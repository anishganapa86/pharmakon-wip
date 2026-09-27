"""Glue between persisted doses and the engine / detectors.

This is the API boundary the engine docstrings refer to: ISO timestamps are
converted to float hours here and nowhere else, and back again on the way out.
No PK or detection logic lives here — it calls app.pk and app.detect as-is.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..detect.alerts import Alert, LoggedDose, detect_accumulation, detect_duplication
from ..pk.engine import (
    DEFAULT_RESOLUTION_H,
    Dose,
    UnsupportedKinetics,
    superpose,
    time_grid,
)
from ..pk.individualize import Patient, adjusted_ke
from ..registry.loader import Registry
from .db import AlertRecord, DoseEvent, DoseIngredient

# Population reference subject, used until PUT /profile exists. No serum
# creatinine, so renally cleared substances get the population-clearance note.
DEFAULT_PATIENT = Patient(age_years=40, weight_kg=70.0, sex="male")

# `1h` is decimation of the fixed 15-minute grid, never interpolation.
DECIMATION = {"15m": 1, "1h": int(round(1.0 / DEFAULT_RESOLUTION_H))}


def hours_since(t: datetime, origin: datetime) -> float:
    return (t - origin).total_seconds() / 3600.0


def load_doses(session: Session, user_id: str) -> list[DoseEvent]:
    return list(session.scalars(select(DoseEvent).where(DoseEvent.user_id == user_id)))


def ingredient_json(ing: DoseIngredient, registry: Registry) -> dict:
    """One logged ingredient as shown to the user.

    `display_name` and `description` come from the registry and are null
    when `substance_id` is null (unmatched) -- there's no entry to pull them
    from. The client falls back to title-casing the raw label in that case.
    """
    display_name = None
    description = None
    if ing.substance_id is not None:
        entry = registry.get(ing.substance_id)
        display_name = entry["name"]
        description = entry.get("description")
    return {
        "name": ing.ingredient_name,
        "substance_id": ing.substance_id,
        "display_name": display_name,
        "description": description,
        "amount_mg": ing.amount_mg,
        "unmodeled_reason": ing.unmodeled_reason,
    }


def logged_doses(events: list[DoseEvent], origin: datetime) -> list[LoggedDose]:
    """Modeled ingredients only, one LoggedDose per (dose event, substance).

    Unmodeled ingredients (unmatched or unknown strength) are excluded from the
    PK model and the 24h sum, per the contract. They stay in the database.
    """
    out = []
    for ev in events:
        per_substance: dict[str, float] = defaultdict(float)
        for ing in ev.ingredients:
            if ing.unmodeled_reason is None:
                per_substance[ing.substance_id] += ing.amount_mg
        t = hours_since(datetime.fromisoformat(ev.taken_at), origin)
        for sid, mg in per_substance.items():
            out.append(LoggedDose(ev.id, sid, t, mg, ev.product_name))
    return out


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


def _contributing(alert: Alert, events: dict[str, DoseEvent]) -> list[dict]:
    out = []
    for dose_id in dict.fromkeys(alert.contributing_dose_ids):
        ev = events[dose_id]
        amount = sum(
            ing.amount_mg
            for ing in ev.ingredients
            if ing.substance_id == alert.substance_id and ing.unmodeled_reason is None
        )
        out.append(
            {
                "dose_event_id": ev.id,
                "product_id": ev.product_id,
                "product_name": ev.product_name,
                "taken_at": ev.taken_at,
                "amount_mg": amount,
            }
        )
    return out


def recompute_alerts(
    session: Session,
    user_id: str,
    registry: Registry,
    now: datetime,
    patient: Patient = DEFAULT_PATIENT,
) -> list[AlertRecord]:
    """Run both detectors and reconcile with stored alerts.

    Identity is (kind, substance, severity). A produced alert that matches an
    active one updates it in place; one that doesn't is new and returned as
    triggered. An active alert no longer produced is resolved. This is the only
    path that makes an alert inactive — no timers, no dismissal in v1.
    """
    events = load_doses(session, user_id)
    by_id = {ev.id: ev for ev in events}
    doses = logged_doses(events, origin=now)
    now_iso = now.isoformat()

    produced: list[Alert] = []
    for sid in sorted({d.substance_id for d in doses}):
        entry = registry.get(sid)
        params = registry.pk_params(sid)
        dup = detect_duplication(
            doses, params, entry["name"], entry["threshold_24h_mg"], 0.0, patient
        )
        acc = detect_accumulation(doses, params, entry["name"], patient, 0.0)
        produced += [a for a in (dup, acc) if a is not None]

    active = {
        (r.kind, r.substance_id, r.severity): r
        for r in session.scalars(
            select(AlertRecord).where(
                AlertRecord.user_id == user_id,
                AlertRecord.dismissed_at.is_(None),
                AlertRecord.resolved_at.is_(None),
            )
        )
    }

    triggered = []
    seen = set()
    for alert in produced:
        contributing = _contributing(alert, by_id)
        # Detectors don't produce uncounted doses yet (incomplete_data follow-up).
        uncounted: list[dict] = []
        if not contributing and not uncounted:
            # Contract: the server must never emit an untraceable alert.
            raise RuntimeError(
                f"untraceable {alert.kind} alert for {alert.substance_id}"
            )
        key = (alert.kind, alert.substance_id, alert.severity)
        seen.add(key)
        record = active.get(key)
        if record is None:
            record = AlertRecord(
                id=f"alert_{uuid.uuid4().hex[:12]}",
                user_id=user_id,
                kind=alert.kind,
                severity=alert.severity,
                substance_id=alert.substance_id,
            )
            session.add(record)
            triggered.append(record)
        record.message = alert.message
        record.notes = list(alert.notes)
        record.contributing_doses = contributing
        record.uncounted_doses = uncounted
        record.computed_at = now_iso

    for key, record in active.items():
        if key not in seen:
            record.resolved_at = now_iso

    session.flush()
    return triggered


def list_alerts(session: Session, user_id: str, active: str) -> list[AlertRecord]:
    q = select(AlertRecord).where(AlertRecord.user_id == user_id)
    is_active = AlertRecord.dismissed_at.is_(None) & AlertRecord.resolved_at.is_(None)
    if active == "true":
        q = q.where(is_active)
    elif active == "false":
        q = q.where(~is_active)
    return list(session.scalars(q))


def alert_json(record: AlertRecord, registry: Registry) -> dict:
    substance = None
    if record.substance_id is not None:
        entry = registry.get(record.substance_id)
        substance = {
            "id": record.substance_id,
            "display_name": entry["name"],
            # Why the threshold matters, shown as "Why this matters" next to
            # the alert. Null unless the registry documents one; never
            # invented here (CLAUDE.md: never a plausible-looking coefficient
            # or claim without a source).
            "threshold_explanation": entry.get("threshold_explanation"),
            # Plain-language "what this is / does", from the registry field
            # of the same name. Null unless the registry documents one.
            "description": entry.get("description"),
        }
    return {
        "id": record.id,
        "kind": record.kind,
        "severity": record.severity,
        "substance": substance,
        "message": record.message,
        "notes": record.notes,
        "computed_at": record.computed_at,
        "contributing_doses": record.contributing_doses,
        "uncounted_doses": record.uncounted_doses,
        "dismissed_at": record.dismissed_at,
        "resolved_at": record.resolved_at,
    }


# ---------------------------------------------------------------------------
# Curve
# ---------------------------------------------------------------------------


@dataclass
class CurveResult:
    modelable: bool
    reason: str | None
    points: list[dict]
    peak: dict | None
    current: dict | None
    notes: list[str]  # causes behind the curve, from adjusted_ke


def _point(origin: datetime, t_hours: float, conc: float) -> dict:
    return {
        "t": (origin + timedelta(hours=float(t_hours))).isoformat(),
        "concentration": float(conc),
    }


def compute_curve(
    events: list[DoseEvent],
    substance_id: str,
    registry: Registry,
    start: datetime,
    end: datetime,
    resolution: str,
    now: datetime,
    patient: Patient = DEFAULT_PATIENT,
) -> CurveResult:
    """Concentration on the fixed 15-minute grid from `start`, in mg/L.

    Timestamps come back in `start`'s offset. Peak is taken over the full
    15-minute grid so a coarser resolution can't hide it.
    """
    params = registry.pk_params(substance_id)
    try:
        params.check_modelable()
    except UnsupportedKinetics as exc:
        return CurveResult(False, str(exc), [], None, None, [])

    ke, notes = adjusted_ke(params, patient)
    doses = [
        Dose(d.t_hours, d.amount_mg)
        for d in logged_doses(events, origin=start)
        if d.substance_id == substance_id
    ]

    end_h = hours_since(end, start)
    grid = time_grid(0.0, end_h)
    grid = grid[grid <= end_h + 1e-9]  # `to` is inclusive; never overshoot it
    conc = superpose(doses, params, patient.weight_kg, grid, ke=ke)

    step = DECIMATION[resolution]
    points = [
        _point(start, t, c) for t, c in zip(grid[::step], conc[::step], strict=True)
    ]

    peak = None
    if conc.max() > 0:
        i = int(np.argmax(conc))
        peak = _point(start, grid[i], conc[i])

    current = None
    if start <= now <= end:
        now_h = hours_since(now, start)
        c_now = superpose(doses, params, patient.weight_kg, np.array([now_h]), ke=ke)
        current = _point(start, now_h, c_now[0])

    return CurveResult(True, None, points, peak, current, notes)
