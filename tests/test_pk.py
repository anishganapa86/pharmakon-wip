"""Engine and individualization tests.

The spec's framing: this suite is the difference between "we implemented some
equations" and "we implemented equations and verified them."
"""

import numpy as np
import pytest

from app.pk.engine import (
    Dose,
    PKParams,
    UnsupportedKinetics,
    bateman,
    clearance_l_per_hour,
    superpose,
    time_grid,
    tmax_hours,
    troughs_per_day,
)
from app.pk.individualize import Patient, adjusted_ke, cockcroft_gault
from app.registry.loader import load_registry, normalize, strip_salt_tokens

CAFFEINE = PKParams("caffeine", 5.0, 0.6, 0.99, 1.4, "hepatic")
METFORMIN = PKParams("metformin", 5.0, 4.0, 0.55, 0.9, "renal")
SERTRALINE = PKParams("sertraline", 26.0, 20.0, 0.44, 0.4, "hepatic")

ADULT = Patient(30, 70, "male")


# --- The gate test: elimination must be first-order ------------------------


def test_caffeine_halves_every_half_life_after_peak():
    """100 mg caffeine, t1/2 = 5 h. Past absorption, the level halves every 5 h."""
    peak = tmax_hours(CAFFEINE)
    t = np.array([peak + 20, peak + 25, peak + 30])
    c = bateman(t, 100.0, CAFFEINE, 70.0)
    assert c[1] / c[0] == pytest.approx(0.5, rel=1e-3)
    assert c[2] / c[1] == pytest.approx(0.5, rel=1e-3)


def test_numeric_peak_matches_analytic_tmax():
    t = np.linspace(0, 24, 24 * 240 + 1)
    c = bateman(t, 100.0, CAFFEINE, 70.0)
    assert t[int(np.argmax(c))] == pytest.approx(tmax_hours(CAFFEINE), abs=0.02)


def test_zero_before_dose():
    c = bateman(np.array([-5.0, -0.1, 1.0]), 100.0, CAFFEINE, 70.0)
    assert c[0] == 0.0 and c[1] == 0.0 and c[2] > 0


def test_concentration_scales_linearly_with_dose():
    t = np.array([1.0, 4.0, 12.0])
    assert np.allclose(
        bateman(t, 200.0, CAFFEINE, 70.0), 2 * bateman(t, 100.0, CAFFEINE, 70.0)
    )


def test_heavier_person_reaches_lower_peak():
    t = np.array([tmax_hours(CAFFEINE)])
    assert bateman(t, 100.0, CAFFEINE, 110.0)[0] < bateman(t, 100.0, CAFFEINE, 70.0)[0]


def test_clearance_is_ke_times_vd():
    assert clearance_l_per_hour(CAFFEINE, 70.0) == pytest.approx(
        CAFFEINE.ke() * 0.6 * 70.0
    )


def test_zero_order_substance_is_refused_not_guessed():
    """Alcohol is zero-order. A wrong curve is worse than an honest refusal."""
    ethanol = PKParams("ethanol", 1.0, 0.6, 1.0, 3.0, "hepatic", kinetics="zero_order")
    with pytest.raises(UnsupportedKinetics):
        ethanol.check_modelable()


# --- Superposition ---------------------------------------------------------


def test_superposition_equals_sum_of_singles():
    grid = time_grid(0.0, 24.0)
    doses = [Dose(0.0, 100.0), Dose(6.0, 100.0)]
    manual = sum(bateman(grid - d.t_hours, d.amount_mg, CAFFEINE, 70.0) for d in doses)
    assert np.allclose(superpose(doses, CAFFEINE, 70.0, grid), manual)


def test_repeat_dosing_climbs_toward_steady_state():
    grid = time_grid(0.0, 96.0)
    doses = [Dose(24.0 * i, 50.0) for i in range(5)]
    troughs = troughs_per_day(grid, superpose(doses, SERTRALINE, 70.0, grid))
    assert troughs[-1] > troughs[1], "long half-life should accumulate"


def test_doses_beyond_the_window_are_dropped():
    grid = time_grid(500.0, 504.0)
    assert np.allclose(superpose([Dose(0.0, 1000.0)], CAFFEINE, 70.0, grid), 0.0)


def test_future_doses_do_not_contribute():
    grid = time_grid(0.0, 10.0)
    assert np.allclose(superpose([Dose(50.0, 500.0)], CAFFEINE, 70.0, grid), 0.0)


# --- Cockcroft-Gault -------------------------------------------------------


def test_cockcroft_gault_worked_example():
    """40 y, 72 kg male, Scr 1.0 -> ((140-40)*72)/(72*1.0) = 100 mL/min."""
    assert cockcroft_gault(Patient(40, 72, "male", 1.0)) == pytest.approx(100.0)


def test_female_correction_factor():
    m = cockcroft_gault(Patient(40, 72, "male", 1.0))
    f = cockcroft_gault(Patient(40, 72, "female", 1.0))
    assert f == pytest.approx(0.85 * m)


def test_unknown_creatinine_means_unadjusted_and_says_so():
    ke, notes = adjusted_ke(METFORMIN, ADULT)
    assert ke == pytest.approx(METFORMIN.ke())
    assert any("population clearance" in n for n in notes), "silence is not acceptable"


def test_impaired_kidneys_slow_renal_clearance():
    ke, notes = adjusted_ke(METFORMIN, Patient(75, 60, "female", 1.8))
    assert ke < METFORMIN.ke()
    assert notes


def test_hepatic_route_is_not_renally_scaled():
    ke, _ = adjusted_ke(CAFFEINE, Patient(75, 60, "female", 1.8))
    assert ke == pytest.approx(CAFFEINE.ke())


def test_hepatic_impairment_warns_without_changing_the_number():
    ke, notes = adjusted_ke(
        CAFFEINE, Patient(40, 70, "male", hepatic_impairment="severe")
    )
    assert ke == pytest.approx(CAFFEINE.ke()), "no invented correction factor"
    assert any("NOT adjusted" in n for n in notes)


def test_absurd_creatinine_cannot_produce_an_absurd_curve():
    ke, _ = adjusted_ke(METFORMIN, Patient(90, 40, "female", 12.0))
    assert ke > 0 and ke >= 0.05 * METFORMIN.ke()


# --- Registry and normalization -------------------------------------------


def test_registry_loads_and_every_entry_resolves_to_itself():
    reg = load_registry()
    assert len(reg.all()) >= 15
    for entry in reg.all():
        assert reg.resolve(entry["name"]) == entry["id"]


def test_every_entry_is_flagged_unverified_until_a_human_checks_it():
    unverified = load_registry().unverified()
    assert unverified, "if this fails, confirm citations were actually checked"


def test_salt_and_brand_forms_normalize_to_the_same_substance():
    reg = load_registry()
    for variant in ["ACETAMINOPHEN", "Acetaminophen", "acetaminophen ", "paracetamol"]:
        assert reg.resolve(variant) == "acetaminophen", variant


def test_salt_suffix_stripping():
    assert strip_salt_tokens(normalize("Diphenhydramine HCl")) == "diphenhydramine"
    assert strip_salt_tokens(normalize("Sertraline Hydrochloride")) == "sertraline"


def test_unknown_ingredient_returns_none_rather_than_guessing():
    """None routes to manual entry, which works. A wrong guess mis-attributes a dose."""
    assert load_registry().resolve("zzqx anti-inflammatory complex") is None
