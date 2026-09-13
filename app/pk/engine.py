"""One-compartment PK engine.

Units are fixed and never negotiated inside this module:
    dose        mg
    time        hours (float, relative to a reference instant)
    volume      L
    concentration  mg/L

Everything downstream (API, client) uses mg/L. Do not introduce ug/mL.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np

LN2 = math.log(2.0)

# Doses older than this many half-lives contribute <1% and are dropped.
SUPERPOSITION_WINDOW_HALF_LIVES = 7.0

# Default grid resolution, in hours. 15 minutes.
DEFAULT_RESOLUTION_H = 0.25


class UnsupportedKinetics(Exception):
    """Raised for substances this engine cannot honestly model."""


@dataclass(frozen=True)
class PKParams:
    """Per-substance parameters, population values, before individualization."""

    substance_id: str
    half_life_hours: float
    vd_l_per_kg: float
    bioavailability: float  # 0..1
    ka_per_hour: float
    clearance_route: str  # renal | hepatic | mixed
    kinetics: str = "first_order"  # first_order | zero_order | nonlinear

    def ke(self) -> float:
        return LN2 / self.half_life_hours

    def check_modelable(self) -> None:
        if self.kinetics != "first_order":
            raise UnsupportedKinetics(
                f"{self.substance_id} follows {self.kinetics} kinetics; the "
                "one-compartment first-order model does not apply. Surface this "
                "to the user rather than returning a wrong curve."
            )


@dataclass(frozen=True)
class Dose:
    """A single administration, in hours relative to the grid origin."""

    t_hours: float
    amount_mg: float


def volume_of_distribution_l(params: PKParams, weight_kg: float) -> float:
    return params.vd_l_per_kg * weight_kg


def clearance_l_per_hour(
    params: PKParams, weight_kg: float, ke: float | None = None
) -> float:
    """CL = ke * Vd. Not used by the curve itself; used in validation tests."""
    ke = params.ke() if ke is None else ke
    return ke * volume_of_distribution_l(params, weight_kg)


def bateman(
    t_hours: Sequence[float] | np.ndarray,
    dose_mg: float,
    params: PKParams,
    weight_kg: float,
    ke: float | None = None,
) -> np.ndarray:
    """Concentration after a single extravascular dose.

        C(t) = (F * D * ka) / (Vd * (ka - ke)) * (exp(-ke*t) - exp(-ka*t))

    t is elapsed time since the dose. Negative t returns 0 (the dose has not
    been taken yet). Handles the ka == ke degenerate case with the limit form
        C(t) = (F * D / Vd) * ka * t * exp(-ka * t)
    """
    params.check_modelable()

    ke = params.ke() if ke is None else ke
    ka = params.ka_per_hour
    vd = volume_of_distribution_l(params, weight_kg)

    t = np.asarray(t_hours, dtype=float)
    out = np.zeros_like(t)
    live = t >= 0.0
    tl = t[live]

    if abs(ka - ke) < 1e-9:
        out[live] = (params.bioavailability * dose_mg / vd) * ka * tl * np.exp(-ka * tl)
    else:
        coeff = (params.bioavailability * dose_mg * ka) / (vd * (ka - ke))
        out[live] = coeff * (np.exp(-ke * tl) - np.exp(-ka * tl))

    # Floating point can produce tiny negatives at t≈0.
    return np.clip(out, 0.0, None)


def tmax_hours(params: PKParams, ke: float | None = None) -> float:
    """Analytic time of peak after a single dose: ln(ka/ke) / (ka - ke)."""
    ke = params.ke() if ke is None else ke
    ka = params.ka_per_hour
    if abs(ka - ke) < 1e-9:
        return 1.0 / ka
    return math.log(ka / ke) / (ka - ke)


def time_grid(
    t_start: float, t_end: float, resolution_h: float = DEFAULT_RESOLUTION_H
) -> np.ndarray:
    if t_end <= t_start:
        raise ValueError("t_end must be after t_start")
    n = int(round((t_end - t_start) / resolution_h)) + 1
    return t_start + np.arange(n) * resolution_h


def superpose(
    doses: Iterable[Dose],
    params: PKParams,
    weight_kg: float,
    grid: np.ndarray,
    ke: float | None = None,
) -> np.ndarray:
    """Total concentration on `grid` from all prior doses.

    This is the function that produces accumulation, which is the phenomenon
    the whole product exists to surface. Doses older than
    SUPERPOSITION_WINDOW_HALF_LIVES relative to the grid start are dropped.
    """
    params.check_modelable()
    ke = params.ke() if ke is None else ke

    cutoff = SUPERPOSITION_WINDOW_HALF_LIVES * params.half_life_hours
    total = np.zeros_like(grid, dtype=float)

    for d in doses:
        if grid[0] - d.t_hours > cutoff:
            continue
        if d.t_hours > grid[-1]:
            continue
        total += bateman(grid - d.t_hours, d.amount_mg, params, weight_kg, ke=ke)

    return total


def troughs_per_day(
    grid: np.ndarray, conc: np.ndarray, day_length_h: float = 24.0
) -> list[float]:
    """Minimum concentration within each successive 24h block of the grid.

    Rising troughs across days is the accumulation signal. Peaks are noisy
    because they depend on when the last dose landed relative to the grid.
    """
    if len(grid) != len(conc):
        raise ValueError("grid and conc must be the same length")
    troughs: list[float] = []
    start = grid[0]
    while start < grid[-1]:
        mask = (grid >= start) & (grid < start + day_length_h)
        if mask.any():
            troughs.append(float(conc[mask].min()))
        start += day_length_h
    return troughs
