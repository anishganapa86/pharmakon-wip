from app.detect.alerts import LoggedDose, detect_accumulation, detect_duplication
from app.pk.engine import PKParams
from app.pk.individualize import Patient

APAP = PKParams("acetaminophen", 2.5, 0.95, 0.88, 2.0, "hepatic")
CAFFEINE = PKParams("caffeine", 5.0, 0.6, 0.99, 1.4, "hepatic")
SERTRALINE = PKParams("sertraline", 26.0, 20.0, 0.44, 0.4, "hepatic")

ADULT = Patient(30, 70, "male")
IMPAIRED_LIVER = Patient(30, 70, "male", hepatic_impairment="moderate")


# --- Duplication -----------------------------------------------------------


def test_the_canonical_hidden_acetaminophen_case():
    """Cold medicine plus headache tablets. No single product looks dangerous."""
    doses = [
        LoggedDose("d1", "acetaminophen", 0.0, 650.0, "NyQuil"),
        LoggedDose("d2", "acetaminophen", 5.0, 1000.0, "Extra Strength Tylenol"),
        LoggedDose("d3", "acetaminophen", 11.0, 1000.0, "Extra Strength Tylenol"),
        LoggedDose("d4", "acetaminophen", 17.0, 650.0, "NyQuil"),
    ]
    alert = detect_duplication(doses, APAP, "Acetaminophen", 4000.0, 20.0, ADULT)
    assert alert is not None
    assert alert.kind == "duplication"
    assert alert.severity == "caution"  # 3300 of 4000
    assert set(alert.contributing_dose_ids) == {"d1", "d2", "d3", "d4"}
    assert "NyQuil" in alert.message


def test_breaching_the_threshold_is_urgent():
    doses = [
        LoggedDose("d1", "acetaminophen", 0.0, 2000.0, "NyQuil"),
        LoggedDose("d2", "acetaminophen", 8.0, 2100.0, "Tylenol"),
    ]
    alert = detect_duplication(doses, APAP, "Acetaminophen", 4000.0, 20.0, ADULT)
    assert alert.severity == "urgent" and "exceeds" in alert.message


def test_single_product_is_a_threshold_alert_not_a_duplication_one():
    doses = [LoggedDose("d1", "acetaminophen", 0.0, 3900.0, "Tylenol")]
    alert = detect_duplication(doses, APAP, "Acetaminophen", 4000.0, 6.0, ADULT)
    assert alert.kind == "threshold"


def test_well_under_the_threshold_stays_quiet():
    doses = [LoggedDose("d1", "acetaminophen", 0.0, 650.0, "Tylenol")]
    assert detect_duplication(doses, APAP, "Acetaminophen", 4000.0, 6.0, ADULT) is None


def test_hepatic_impairment_lowers_the_threshold_and_explains_why():
    doses = [LoggedDose("d1", "acetaminophen", 0.0, 2200.0, "Tylenol")]
    assert detect_duplication(doses, APAP, "Acetaminophen", 4000.0, 6.0, ADULT) is None
    alert = detect_duplication(
        doses, APAP, "Acetaminophen", 4000.0, 6.0, IMPAIRED_LIVER
    )
    assert alert is not None and alert.severity == "urgent"
    assert any("hepatic impairment" in n for n in alert.notes)


def test_doses_outside_the_24h_window_are_excluded():
    doses = [LoggedDose("old", "acetaminophen", 0.0, 4000.0, "Tylenol")]
    assert detect_duplication(doses, APAP, "Acetaminophen", 4000.0, 40.0, ADULT) is None


def test_no_threshold_means_no_alert():
    doses = [LoggedDose("d1", "caffeine", 0.0, 9999.0, "Coffee")]
    assert detect_duplication(doses, CAFFEINE, "Caffeine", None, 1.0, ADULT) is None


def test_other_substances_do_not_contaminate_the_sum():
    doses = [
        LoggedDose("d1", "acetaminophen", 0.0, 500.0, "Tylenol"),
        LoggedDose("d2", "ibuprofen", 1.0, 4000.0, "Advil"),
    ]
    assert detect_duplication(doses, APAP, "Acetaminophen", 4000.0, 6.0, ADULT) is None


# --- Accumulation ----------------------------------------------------------


def test_accumulation_fires_on_a_rising_trough():
    doses = [
        LoggedDose(f"d{i}", "sertraline", 24.0 * i, 50.0, "Zoloft") for i in range(6)
    ]
    alert = detect_accumulation(doses, SERTRALINE, "Sertraline", ADULT, 120.0)
    assert alert is not None and alert.kind == "accumulation"
    assert len(alert.contributing_dose_ids) == 6
    troughs = alert.payload["troughs_mg_per_l"]
    assert troughs[-1] > troughs[0]


def test_accumulation_stays_quiet_for_a_fast_clearing_substance():
    """Caffeine at 24h intervals reaches steady state immediately. Flat floor."""
    doses = [
        LoggedDose(f"d{i}", "caffeine", 24.0 * i, 100.0, "Coffee") for i in range(6)
    ]
    assert detect_accumulation(doses, CAFFEINE, "Caffeine", ADULT, 120.0) is None


def test_not_enough_history_stays_quiet():
    """Silence beats a confident finding from two days of data."""
    doses = [
        LoggedDose(f"d{i}", "sertraline", 24.0 * i, 50.0, "Zoloft") for i in range(2)
    ]
    assert detect_accumulation(doses, SERTRALINE, "Sertraline", ADULT, 48.0) is None


def test_no_doses_of_that_substance_stays_quiet():
    doses = [LoggedDose("d1", "caffeine", 0.0, 100.0, "Coffee")]
    assert detect_accumulation(doses, SERTRALINE, "Sertraline", ADULT, 120.0) is None


def test_zero_order_substance_returns_an_honest_refusal_not_a_curve():
    ethanol = PKParams("ethanol", 1.0, 0.6, 1.0, 3.0, "hepatic", kinetics="zero_order")
    doses = [LoggedDose(f"d{i}", "ethanol", 24.0 * i, 14.0, "Beer") for i in range(6)]
    alert = detect_accumulation(doses, ethanol, "Alcohol", ADULT, 120.0)
    assert alert is not None and alert.kind == "unmodelable"
    assert alert.severity == "info"


def test_renal_notes_are_carried_onto_the_alert():
    metformin = PKParams("metformin", 5.0, 4.0, 0.55, 0.9, "renal")
    elderly = Patient(78, 62, "female", 1.6)
    doses = [
        LoggedDose(f"d{i}", "metformin", 12.0 * i, 850.0, "Glucophage")
        for i in range(12)
    ]
    alert = detect_accumulation(doses, metformin, "Metformin", elderly, 132.0)
    if alert is not None:
        assert any("Renal adjustment" in n for n in alert.notes)


def test_every_alert_is_traceable_to_doses():
    doses = [
        LoggedDose("d1", "acetaminophen", 0.0, 2000.0, "NyQuil"),
        LoggedDose("d2", "acetaminophen", 8.0, 2100.0, "Tylenol"),
    ]
    alert = detect_duplication(doses, APAP, "Acetaminophen", 4000.0, 20.0, ADULT)
    assert alert.contributing_dose_ids, "non-negotiable per the API contract"
