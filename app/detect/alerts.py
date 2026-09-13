"""Alert generation: duplication and accumulation.

Built on app.pk.engine / app.pk.individualize. Times are float hours on the
same origin the engine's grid uses; the API layer converts to and from ISO
timestamps at the boundary and nowhere else.

Structural rule: Alert requires contributing_dose_ids. An alert the user cannot
trace back to specific doses is not actionable, so the dataclass makes it
impossible to construct one without them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..pk.engine import (
    Dose,
    PKParams,
    UnsupportedKinetics,
    superpose,
    time_grid,
    troughs_per_day,
)
from ..pk.individualize import Patient, adjusted_ke

# Threshold multipliers under recorded hepatic impairment. Explicit and
# documented, applied to the *threshold* rather than hidden inside the model.
HEPATIC_THRESHOLD_FACTOR = {"none": 1.0, "mild": 0.75, "moderate": 0.5, "severe": 0.5}

# Fraction of threshold at which we start warning rather than waiting for a
# breach. Warning only at 100% is warning too late to act on.
APPROACH_FRACTION = 0.75


@dataclass(frozen=True)
class LoggedDose:
    """A dose plus the identity needed for traceability."""

    dose_id: str
    substance_id: str
    t_hours: float
    amount_mg: float
    product_name: str

    def as_dose(self) -> Dose:
        return Dose(t_hours=self.t_hours, amount_mg=self.amount_mg)


@dataclass
class Alert:
    kind: str  # duplication | threshold | accumulation | unmodelable
    severity: str  # info | caution | urgent
    substance_id: str
    message: str
    contributing_dose_ids: list[str]
    notes: list[str] = field(default_factory=list)
    payload: dict = field(default_factory=dict)


def _window(
    doses: list[LoggedDose], substance_id: str, lo: float, hi: float
) -> list[LoggedDose]:
    return [
        d for d in doses if d.substance_id == substance_id and lo <= d.t_hours <= hi
    ]


# ---------------------------------------------------------------------------
# Duplication / threshold
# ---------------------------------------------------------------------------


def detect_duplication(
    doses: list[LoggedDose],
    params: PKParams,
    display_name: str,
    threshold_24h_mg: float | None,
    now_hours: float,
    patient: Patient,
) -> Alert | None:
    """Sum one substance across every product in the trailing 24 hours.

    The canonical case: acetaminophen in a cold medicine plus a headache
    tablet. Neither product looks dangerous on its own.
    """
    if threshold_24h_mg is None:
        return None

    recent = _window(doses, params.substance_id, now_hours - 24.0, now_hours)
    if not recent:
        return None

    total = sum(d.amount_mg for d in recent)
    products = sorted({d.product_name for d in recent})
    notes: list[str] = []

    effective = threshold_24h_mg
    if (
        params.clearance_route in ("hepatic", "mixed")
        and patient.hepatic_impairment != "none"
    ):
        effective *= HEPATIC_THRESHOLD_FACTOR[patient.hepatic_impairment]
        notes.append(
            f"Threshold reduced from {threshold_24h_mg:.0f} to {effective:.0f} mg "
            f"because of recorded {patient.hepatic_impairment} hepatic impairment."
        )

    if total < APPROACH_FRACTION * effective:
        return None

    breached = total >= effective
    multi = len(products) > 1

    lead = (
        f"{display_name} is in {len(products)} of the products you logged "
        f"({', '.join(products)})."
        if multi
        else f"{display_name} intake over 24 hours."
    )

    return Alert(
        kind="duplication" if multi else "threshold",
        severity="urgent" if breached else "caution",
        substance_id=params.substance_id,
        message=(
            f"{lead} Your 24-hour total is {total:.0f} mg, which "
            f"{'exceeds' if breached else 'is approaching'} the "
            f"{effective:.0f} mg limit. Talk to a pharmacist or your prescriber."
        ),
        contributing_dose_ids=[d.dose_id for d in recent],
        notes=notes,
        payload={
            "total_mg": total,
            "threshold_mg": effective,
            "base_threshold_mg": threshold_24h_mg,
            "products": products,
        },
    )


# ---------------------------------------------------------------------------
# Accumulation
# ---------------------------------------------------------------------------


def detect_accumulation(
    doses: list[LoggedDose],
    params: PKParams,
    display_name: str,
    patient: Patient,
    now_hours: float,
    lookback_days: int = 5,
    min_troughs: int = 3,
    rel_slope_threshold: float = 0.10,
) -> Alert | None:
    """Fit a line to daily trough concentrations.

    A rising floor means each dose is landing before the previous one cleared.
    Peaks can look flat while the trough climbs, which is exactly the failure
    mode a snapshot interaction checker misses.
    """
    relevant = [d for d in doses if d.substance_id == params.substance_id]
    if not relevant:
        return None

    try:
        params.check_modelable()
    except UnsupportedKinetics as exc:
        return Alert(
            kind="unmodelable",
            severity="info",
            substance_id=params.substance_id,
            message=str(exc),
            contributing_dose_ids=[d.dose_id for d in relevant],
        )

    ke, notes = adjusted_ke(params, patient)
    first_dose = min(d.t_hours for d in relevant)
    start = max(now_hours - lookback_days * 24.0, first_dose)
    if now_hours - start < 24.0 * min_troughs:
        return None

    grid = time_grid(start, now_hours)
    conc = superpose(
        [d.as_dose() for d in relevant], params, patient.weight_kg, grid, ke=ke
    )
    all_troughs = troughs_per_day(grid, conc)

    # Drop the first bucket as warm-up. The window can open before the first
    # dose has been absorbed, leaving a floor near zero that is an artifact of
    # where we started looking. Without this, everything accumulates on day one.
    y = np.array(all_troughs[1:], dtype=float)
    if len(y) < min_troughs or y.mean() <= 0:
        return None

    slope = float(np.polyfit(np.arange(len(y), dtype=float), y, 1)[0])
    rel = slope / float(y.mean())
    if rel < rel_slope_threshold:
        return None

    return Alert(
        kind="accumulation",
        severity="caution",
        substance_id=params.substance_id,
        message=(
            f"{display_name} is accumulating. Your daily low point has risen from "
            f"{y[0]:.3f} to {y[-1]:.3f} mg/L across {len(y)} days, meaning each dose "
            f"is landing before the previous one has cleared. This is an estimate "
            f"from a population-average model — worth raising with your prescriber, "
            f"not a reason to change your dose on your own."
        ),
        contributing_dose_ids=[d.dose_id for d in relevant],
        notes=notes,
        payload={
            "troughs_mg_per_l": [round(v, 5) for v in y.tolist()],
            "slope_per_day": slope,
            "relative_slope_per_day": rel,
        },
    )
