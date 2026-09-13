"""Patient-specific adjustment of population PK parameters.

Two mechanisms, deliberately different in kind:

  Renal   -> a quantitative scaling of ke, via Cockcroft-Gault.
  Hepatic -> a warning modifier, NOT a silent numeric change.

The asymmetry is intentional. There is a validated equation for estimating
creatinine clearance. There is no equivalent one-line correction for hepatic
impairment, and inventing a factor would make the model look more precise than
it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .engine import PKParams

Sex = Literal["male", "female"]
HepaticStatus = Literal["none", "mild", "moderate", "severe"]

# Population reference creatinine clearance, mL/min. The registry's half-life
# values are assumed to describe a subject at roughly this value.
REFERENCE_CRCL_ML_MIN = 100.0

# Fraction of total clearance attributable to the renal route. Coarse, and
# labelled as such: a real per-substance fe (fraction excreted unchanged)
# belongs in the registry. This is a Phase 2 placeholder.
RENAL_FRACTION = {"renal": 1.0, "mixed": 0.5, "hepatic": 0.0}

# Floor on the scaling factor so a bad creatinine value cannot produce an
# absurd curve.
MIN_RENAL_SCALE = 0.1
MAX_RENAL_SCALE = 1.5


@dataclass(frozen=True)
class Patient:
    age_years: float
    weight_kg: float
    sex: Sex
    serum_creatinine_mg_dl: float | None = None
    hepatic_impairment: HepaticStatus = "none"


def cockcroft_gault(patient: Patient) -> float | None:
    """Estimated creatinine clearance in mL/min.

        CrCl = ((140 - age) * weight_kg) / (72 * Scr)      * 0.85 if female

    Returns None when serum creatinine is unknown, which is the common case:
    most users will never have entered a lab value.
    """
    if patient.serum_creatinine_mg_dl is None:
        return None
    if patient.serum_creatinine_mg_dl <= 0:
        raise ValueError("serum creatinine must be positive")

    crcl = ((140.0 - patient.age_years) * patient.weight_kg) / (
        72.0 * patient.serum_creatinine_mg_dl
    )
    if patient.sex == "female":
        crcl *= 0.85
    return max(crcl, 0.0)


def adjusted_ke(params: PKParams, patient: Patient) -> tuple[float, list[str]]:
    """Return (ke, notes). Notes are shown to the user, not swallowed."""
    ke = params.ke()
    notes: list[str] = []

    fraction_renal = RENAL_FRACTION.get(params.clearance_route, 0.0)
    crcl = cockcroft_gault(patient)

    if fraction_renal > 0.0:
        if crcl is None:
            notes.append(
                "Renally cleared. No serum creatinine on file, so population "
                "clearance was used. Entering a recent creatinine value will "
                "sharpen this curve."
            )
        else:
            ratio = crcl / REFERENCE_CRCL_ML_MIN
            scale = fraction_renal * ratio + (1.0 - fraction_renal)
            scale = min(max(scale, MIN_RENAL_SCALE), MAX_RENAL_SCALE)
            ke *= scale
            notes.append(
                f"Renal adjustment applied: estimated CrCl {crcl:.0f} mL/min "
                f"vs reference {REFERENCE_CRCL_ML_MIN:.0f}; elimination scaled "
                f"by {scale:.2f}."
            )

    if patient.hepatic_impairment != "none" and params.clearance_route in (
        "hepatic",
        "mixed",
    ):
        notes.append(
            f"This substance is cleared {params.clearance_route}ly and you have "
            f"recorded {patient.hepatic_impairment} hepatic impairment. The curve "
            "below is NOT adjusted for that — no validated correction exists — so "
            "treat it as an underestimate of true exposure."
        )

    return ke, notes
