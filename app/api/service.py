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
    PKParams,
    UnsupportedKinetics,
    superpose,
    time_grid,
)
from ..pk.individualize import Patient, adjusted_ke
from ..registry.loader import Registry
from .db import AlertRecord, DoseEvent, DoseIngredient
from .products import DecomposedIngredient

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


def record_dose(
    session: Session,
    registry: Registry,
    user_id: str,
    *,
    product_id: str,
    product_name: str,
    taken_at: datetime,
    quantity: float,
    ingredients: list[DecomposedIngredient],
    topical_ingredients: list[str] | None,
    now: datetime,
) -> tuple[DoseEvent, list[AlertRecord]]:
    """Persist one dose and recompute alerts. The only way a DoseEvent is made.

    Manual logging (POST /doses) and "Take now" from a schedule both come
    through here, so a scheduled dose is alerted on and charted exactly like a
    hand-logged one. `ingredients` carry per-unit strengths; the logged amount
    is strength * quantity, and unmodeled ingredients are stored with none.
    """
    event = DoseEvent(
        id=f"dose_{uuid.uuid4().hex[:12]}",
        user_id=user_id,
        product_id=product_id,
        product_name=product_name,
        taken_at=taken_at.isoformat(),
        quantity=quantity,
        topical_ingredients=topical_ingredients,
    )
    for ing in ingredients:
        event.ingredients.append(
            DoseIngredient(
                ingredient_name=ing.name,
                substance_id=ing.substance_id,
                amount_mg=(
                    ing.strength_mg * quantity if ing.unmodeled_reason is None else None
                ),
                unmodeled_reason=ing.unmodeled_reason,
            )
        )
    session.add(event)
    session.flush()
    triggered = recompute_alerts(session, user_id, registry, now)
    return event, triggered


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
        # The 24h sum needs no PK parameters, so it runs whatever the tier.
        dup = detect_duplication(
            doses,
            params,
            entry["name"],
            entry.get("threshold_24h_mg"),
            0.0,
            patient,
        )
        # Accumulation needs a real curve. Without Vd and ka there isn't one,
        # and staying quiet beats alerting on an approximation.
        acc = (
            detect_accumulation(doses, params, entry["name"], patient, 0.0)
            if registry.modelability(sid) in ("full", "unsupported")
            else None
        )
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
            # invented here (project notes: never a plausible-looking coefficient
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
    # "mg/L" for a full curve. An approximate curve (see registry.Modelability)
    # is the amount remaining in the body, in mg: no Vd, so no concentration.
    unit: str = "mg/L"
    fidelity: str = "full"  # "full" | "approximate"
    # The same curve with future scheduled doses added, from `now` on; empty
    # when no schedule contributes. A projection, never merged into `points`.
    projected_points: list[dict] | None = None


APPROXIMATE_NOTE = "Approximate — missing distribution parameters"


def _missing_parameter_notes(entry: dict) -> list[str]:
    notes = [
        "This is the estimated amount of the substance left in the body (mg), "
        "not a blood concentration, and it assumes each dose is absorbed at "
        "once. Only its half-life and bioavailability are on file."
    ]
    if entry.get("vd_l_per_kg") is None:
        notes.append("No volume of distribution on file, so mg/L can't be computed.")
    if entry.get("ka_per_hour") is None:
        notes.append("No absorption rate on file, so the rise after a dose is skipped.")
    return notes


def _decay_amount(
    doses: list[Dose], params: PKParams, ke: float, grid: np.ndarray
) -> np.ndarray:
    """mg remaining: F * dose * exp(-ke * elapsed), summed over past doses.

    Deliberately not app.pk: that engine needs Vd and ka, which this tier
    lacks. Instant absorption is a stated simplification, not a parameter.
    """
    total = np.zeros_like(grid, dtype=float)
    for d in doses:
        elapsed = grid - d.t_hours
        live = elapsed >= 0.0
        total[live] += (
            params.bioavailability * d.amount_mg * np.exp(-ke * elapsed[live])
        )
    return total


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
    scheduled: list[tuple[datetime, float]] | None = None,
) -> CurveResult:
    """Concentration on the fixed 15-minute grid from `start`, in mg/L.

    Timestamps come back in `start`'s offset. Peak is taken over the full
    15-minute grid so a coarser resolution can't hide it.

    `scheduled` is (instant, mg) for doses planned but not yet taken. They are
    kept out of `points`, `peak` and `current`, which describe what was
    actually logged; `projected_points` adds them on top from `now` onward.
    """
    params = registry.pk_params(substance_id)
    try:
        params.check_modelable()
    except UnsupportedKinetics as exc:
        return CurveResult(False, str(exc), [], None, None, [])

    entry = registry.get(substance_id)
    tier = registry.modelability(substance_id)
    if tier == "none":
        return CurveResult(
            False,
            f"No pharmacokinetic parameters on file for {entry['name']}: "
            "its doses are logged, but no curve is drawn.",
            [],
            None,
            None,
            [],
        )

    ke, notes = adjusted_ke(params, patient)
    if entry.get("kinetics_assumed"):
        notes.append(
            "Imported from DrugBank, which doesn't state the elimination "
            "order: first-order elimination is assumed and unchecked."
        )
    approximate = tier == "approximate"
    if approximate:
        notes = [APPROXIMATE_NOTE, *_missing_parameter_notes(entry), *notes]

    logged = [
        Dose(d.t_hours, d.amount_mg)
        for d in logged_doses(events, origin=start)
        if d.substance_id == substance_id
    ]

    def level(at_hours: np.ndarray, extra: list[Dose] = []) -> np.ndarray:  # noqa: B006
        if approximate:
            return _decay_amount([*logged, *extra], params, ke, at_hours)
        return superpose([*logged, *extra], params, patient.weight_kg, at_hours, ke=ke)

    end_h = hours_since(end, start)
    grid = time_grid(0.0, end_h)
    grid = grid[grid <= end_h + 1e-9]  # `to` is inclusive; never overshoot it
    conc = level(grid)

    step = DECIMATION[resolution]
    points = [
        _point(start, t, c) for t, c in zip(grid[::step], conc[::step], strict=True)
    ]

    peak = None
    if conc.max() > 0:
        i = int(np.argmax(conc))
        peak = _point(start, grid[i], conc[i])

    now_h = hours_since(now, start)
    current = None
    if start <= now <= end:
        current = _point(start, now_h, level(np.array([now_h]))[0])

    projected = None
    if scheduled:
        planned = [Dose(hours_since(t, start), mg) for t, mg in scheduled]
        both = level(grid, planned)
        projected = [
            _point(start, t, c)
            for t, c in zip(grid[::step], both[::step], strict=True)
            if t >= now_h
        ]

    return CurveResult(
        True,
        None,
        points,
        peak,
        current,
        notes,
        "mg" if approximate else "mg/L",
        "approximate" if approximate else "full",
        projected,
    )
