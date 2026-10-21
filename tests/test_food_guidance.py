"""app.api.food_guidance, POST /food-guidance, and
GET /users/{id}/food-guidance.

openFDA is never actually reached: every test installs its own
httpx.MockTransport or monkeypatches the fetch functions directly (same
pattern as app/api/interactions.py's tests).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import food_guidance, main, service
from app.api import openfda as openfda_module
from app.api.db import Base

NOW = datetime.fromisoformat("2026-09-13T18:00:00-07:00")


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    def session_override():
        with SessionLocal() as s:
            yield s

    main.app.dependency_overrides[main.get_session] = session_override
    main.app.dependency_overrides[main.get_now] = lambda: NOW
    food_guidance.clear_cache()
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()
    food_guidance.clear_cache()


def oral_label(**fields) -> dict:
    label = {"openfda": {"route": ["ORAL"]}}
    label.update(fields)
    return label


def topical_label(**fields) -> dict:
    label = {"openfda": {"route": ["TOPICAL"]}}
    label.update(fields)
    return label


def no_route_label(**fields) -> dict:
    label = {"openfda": {}}
    label.update(fields)
    return label


class FakeOpenFDA:
    """Serves a whole label dict per generic/brand name search; 404 for
    anything not registered."""

    def __init__(self, labels: dict[str, dict] | None = None, down: bool = False):
        self.labels = {k.upper(): v for k, v in (labels or {}).items()}
        self.down = down
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        search = request.url.params["search"]
        self.calls.append(search)
        if self.down:
            raise httpx.ConnectError("unreachable", request=request)
        for name, label in self.labels.items():
            if f'"{name}"' in search:
                return httpx.Response(200, json={"results": [label]})
        return httpx.Response(
            404, json={"error": {"code": "NOT_FOUND", "message": "No matches"}}
        )


@pytest.fixture
def openfda(monkeypatch):
    fake = FakeOpenFDA()
    monkeypatch.setattr(
        food_guidance, "_http", httpx.Client(transport=httpx.MockTransport(fake))
    )
    food_guidance.clear_cache()
    yield fake
    food_guidance.clear_cache()


def manual_check(client, names):
    r = client.post("/food-guidance", json={"names": names})
    assert r.status_code == 200, r.text
    return r.json()


def register_and_log(client, product_id, hours_ago=1):
    r = client.post(
        "/users/default/doses",
        json={
            "product_id": product_id,
            "taken_at": (NOW - timedelta(hours=hours_ago)).isoformat(),
            "quantity": 1,
        },
    )
    assert r.status_code == 201, r.text


def add_schedule(client, product_id, frequency="daily", times=("08:00",)):
    r = client.post(
        "/users/default/schedule",
        json={"product_id": product_id, "frequency": frequency, "times": list(times)},
    )
    assert r.status_code == 201, r.text
    return r.json()


# --- Sentence qualification --------------------------------------------------


def test_take_with_food_sentence_is_found_and_classified_pure():
    label = oral_label(
        dosage_and_administration=[
            "Adults: take 1 tablet with food or milk if stomach upset occurs."
        ]
    )
    matches = food_guidance.qualifying_matches(label, "food")
    assert len(matches) == 1
    assert "food" in matches[0].quote.lower()
    assert food_guidance.classify(matches[0].quote, "food") == "take_with_food_if_upset"


def test_item_term_without_instruction_cue_is_excluded():
    label = oral_label(warnings=["This product contains natural grapefruit flavor."])
    matches = food_guidance.qualifying_matches(label, "grapefruit")
    assert matches == []


def test_qualifying_match_item_term_is_literally_in_quote():
    label = oral_label(directions=["Take with food. Do not take on an empty stomach."])
    for match in food_guidance.qualifying_matches(label, "food"):
        assert "food" in match.quote.lower()


def test_alcoholic_drinks_variant_qualifies_for_alcohol():
    label = oral_label(
        warnings=["Avoid alcoholic drinks while taking this medication."]
    )
    matches = food_guidance.qualifying_matches(label, "alcohol")
    assert len(matches) == 1
    assert "alcoholic drinks" in matches[0].quote.lower()


def test_phrase_matching_is_word_boundary_safe_not_substring():
    # "ash" is a real substring of "crash", but not a matched *word* -- the
    # boundary-based matcher must not treat it as a hit (no stemming, no
    # fuzzy/substring matching).
    assert food_guidance._phrase_in("avoid a crash while driving", "ash") is False
    assert food_guidance._phrase_in("some ash on the counter", "ash") is True


def test_item_variant_present_without_instruction_cue_is_excluded():
    label = oral_label(
        warnings=["This product contains alcoholic flavoring for taste."]
    )
    matches = food_guidance.qualifying_matches(label, "alcohol")
    assert matches == []


def test_meal_variant_qualifies_as_food_item():
    label = oral_label(directions=["Take once daily with the evening meal."])
    matches = food_guidance.qualifying_matches(label, "food")
    assert len(matches) == 1
    assert food_guidance.classify(matches[0].quote, "food") == "take_with_food"


def test_dairy_variant_qualifies_as_milk_item():
    label = oral_label(
        warnings=["Avoid dairy products for two hours after taking this medicine."]
    )
    matches = food_guidance.qualifying_matches(label, "milk")
    assert len(matches) == 1
    assert "dairy" in matches[0].quote.lower()


def test_at_most_two_matches_kept_best_scoring_first():
    label = oral_label(
        dosage_and_administration=[
            "Take with food.",
            "Take with food and plenty of water before bed.",
            "This medicine may be taken with or without food.",
        ]
    )
    matches = food_guidance.qualifying_matches(label, "food")
    assert len(matches) <= 2


# --- Quote integrity --------------------------------------------------------------


def test_antabuse_style_list_item_never_quoted_without_its_lead_in():
    # Mirrors a real sertraline label: no "•" bullets at all -- each
    # "do not take if" list item is just period-separated, so once split it
    # reads as its own lowercase-started "sentence" with no governing
    # clause attached.
    label = oral_label(
        spl_medguide=[
            "Do not take this product if you: take the antipsychotic "
            "medicine pimozide. take Antabuse (disulfiram) due to the "
            "alcohol content. are allergic to any of the ingredients."
        ]
    )
    quotes, pointers = food_guidance.label_matches(label, "alcohol")
    assert not any(
        "antabuse" in q.quote.lower() and "do not" not in q.quote.lower()
        for q in quotes
    )
    # Never silently dropped either -- it still surfaces as a pointer.
    assert any(p.field == "spl_medguide" for p in pointers)


def test_truncated_sentence_is_not_quoted():
    tail = " and continuing on with further explanation" * 6
    long_sentence = f"Avoid alcohol while taking this medication{tail} as advised."
    assert len(long_sentence) > food_guidance.MAX_QUOTE_CHARS
    label = oral_label(warnings=[long_sentence])

    assert food_guidance.qualifying_matches(label, "alcohol") == []
    pointers = food_guidance.qualifying_pointers(label, "alcohol")
    assert any(p.field == "warnings" for p in pointers)


def test_heading_noise_sentence_is_not_quoted():
    label = oral_label(
        dosage_and_administration=["2 DOSAGE AND ADMINISTRATION Take with food daily."]
    )

    assert food_guidance.qualifying_matches(label, "food") == []
    pointers = food_guidance.qualifying_pointers(label, "food")
    assert any(p.field == "dosage_and_administration" for p in pointers)


def test_xref_token_sentence_is_not_quoted():
    label = oral_label(
        drug_interactions=["Take with food ( 2.1 ) to reduce stomach upset."]
    )
    assert food_guidance.qualifying_matches(label, "food") == []


def test_bullet_list_item_assembled_quote_keeps_bullet_separator():
    label = oral_label(
        spl_medguide=[
            "This medicine may increase certain risks: • with food "
            "intolerance • with prolonged use."
        ]
    )
    matches = food_guidance.qualifying_matches(label, "food")
    assert len(matches) == 1
    assert " • " in matches[0].quote
    assert matches[0].quote.startswith("This medicine may increase certain risks:")
    assert "with food intolerance" in matches[0].quote


def test_over_300_char_assembled_list_quote_becomes_pointer():
    long_lead = (
        "This medicine carries several important considerations that "
        "patients should be aware of before beginning treatment, including "
        "interactions with other substances and specific timing "
        "instructions that must be followed precisely for the medication "
        "to work as intended and to avoid any adverse effects occurring:"
    )
    label = oral_label(spl_medguide=[f"{long_lead} • take with food."])
    assert len(long_lead) + len(" • take with food.") > food_guidance.MAX_QUOTE_CHARS

    assert food_guidance.qualifying_matches(label, "food") == []
    pointers = food_guidance.qualifying_pointers(label, "food")
    assert any(p.field == "spl_medguide" for p in pointers)


def test_audience_qualifier_leading_colon_is_still_quotable():
    label = oral_label(
        directions=["Adults: take 1 tablet with food or milk if stomach upset occurs."]
    )
    matches = food_guidance.qualifying_matches(label, "food")
    assert len(matches) == 1


def test_self_titled_section_colon_is_not_quotable():
    # "When using this product:" is the field re-announcing its own
    # section name inline, not a real audience qualifier -- structurally
    # the same self-titling "2 DOSAGE AND ADMINISTRATION" does for Rx
    # labels, just not ALL-CAPS.
    label = oral_label(
        when_using=[
            "When using this product: avoid alcohol sedatives and "
            "tranquilizers may increase drowsiness use caution driving"
        ]
    )
    assert food_guidance.qualifying_matches(label, "alcohol") == []
    pointers = food_guidance.qualifying_pointers(label, "alcohol")
    assert any(p.field == "when_using" for p in pointers)


def test_pointer_finding_has_no_quote_and_class_other():
    item = food_guidance.RegimenItem("ibuprofen", "Ibuprofen", False, "Ibuprofen")
    pointer = food_guidance.PointerMatch("warnings", "alcohol")

    finding = food_guidance._pointer_finding(
        item, "alcohol", pointer, "Ibuprofen", "tablet", "abc-123"
    )

    assert finding["quote"] is None
    assert finding["class"] == "other"
    assert "dailymed.nlm.nih.gov" in finding["pointer"]


def test_with_or_without_food_is_other_not_take_with_food():
    quote = "Sertraline hydrochloride tablets may be taken with or without food."
    assert food_guidance.classify(quote, "food") == "other"


def test_findings_include_product_name_and_dosage_form(monkeypatch, tmp_path):
    monkeypatch.setattr(food_guidance, "DATA_PATH", write_curated(tmp_path, []))
    food_guidance.clear_curated_cache()

    label = oral_label(
        openfda={"route": ["ORAL"], "brand_name": ["Metformin"]},
        dosage_forms_and_strengths=["Extended-Release Tablets: 500 mg."],
        directions=["Take with food each evening."],
    )
    monkeypatch.setattr(
        food_guidance, "fetch_labels_for_substance", lambda name, **kw: [label]
    )
    item = food_guidance.RegimenItem("metformin", "Metformin", False, "Metformin")

    findings = food_guidance._findings_for_item(item, [])
    food_guidance.clear_curated_cache()

    food_finding = next(f for f in findings if f["item"] == "food")
    assert food_finding["product_name"] == "Metformin"
    assert food_finding["dosage_form"] == "extended-release tablet"


def test_dailymed_url_only_built_from_real_set_id():
    with_id = food_guidance.pointer_text("Ibuprofen", "alcohol", "warnings", "abc-123")
    assert "dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid=abc-123" in with_id

    without_id = food_guidance.pointer_text("Ibuprofen", "alcohol", "warnings", None)
    assert "dailymed" not in without_id.lower()
    assert "abc-123" not in without_id


# --- Timing windows ------------------------------------------------------------


def test_explicit_two_sided_window_parses():
    window = food_guidance.parse_window(
        "Take at least 2 hours before or 4 hours after taking an antacid."
    )
    assert window == (2.0, 4.0)


def test_reversed_after_before_window_parses():
    window = food_guidance.parse_window(
        "Take 4 hours after or 2 hours before an antacid."
    )
    assert window == (2.0, 4.0)


def test_vague_sentence_yields_no_window():
    window = food_guidance.parse_window("Avoid taking with antacids for best results.")
    assert window is None


def test_one_sided_hour_statement_yields_no_window():
    window = food_guidance.parse_window("Wait at least 2 hours before eating.")
    assert window is None


# --- Classification --------------------------------------------------------------


@pytest.mark.parametrize(
    "quote,item,expected",
    [
        (
            "Take on an empty stomach at least 1 hour before eating.",
            "food",
            "take_on_empty_stomach",
        ),
        ("Avoid alcohol while taking this medication.", "alcohol", "avoid_item"),
        (
            "Take at least 2 hours before or 4 hours after taking an antacid.",
            "antacid",
            "separate_from",
        ),
        ("Take with food to reduce stomach upset.", "food", "take_with_food"),
        ("This medication may interact with grapefruit juice.", "grapefruit", "other"),
    ],
)
def test_classify_rules(quote, item, expected):
    assert food_guidance.classify(quote, item) == expected


def test_consistent_intake_sentence_is_not_avoid_item():
    # "avoid" appears in the sentence, but 25 words from "vitamin k" and
    # about something else entirely (diet changes in general) -- the advice
    # actually attached to vitamin K here is to keep intake *consistent*,
    # not to avoid it.
    quote = (
        "Maintain a consistent vitamin K intake throughout treatment since "
        "large changes in your usual diet from one week to the next can "
        "affect INR results and patients should avoid drastic sudden "
        "dietary changes."
    )
    assert food_guidance.classify(quote, "vitamin k") == "other"


def test_avoid_grapefruit_juice_is_avoid_item():
    quote = "Avoid grapefruit juice while taking this medication."
    assert food_guidance.classify(quote, "grapefruit") == "avoid_item"


def test_take_with_food_or_milk_is_take_with_food():
    quote = "Take with food or milk to reduce stomach upset."
    assert food_guidance.classify(quote, "food") == "take_with_food"


def test_avoid_far_from_item_term_does_not_classify_as_avoid_item():
    quote = (
        "Avoid operating heavy machinery after taking this medication with "
        "a full glass of water and a light meal containing food."
    )
    # "Avoid" is about machinery, more than 8 words from "food".
    assert food_guidance.classify(quote, "food") != "avoid_item"


def test_once_daily_with_the_evening_meal_is_take_with_food():
    quote = "Take one tablet once daily with the evening meal."
    assert food_guidance.classify(quote, "food") == "take_with_food"


def test_avoid_item_matches_via_variant_not_just_base_term():
    # The quote never says "alcohol" -- only its variant "alcoholic" -- so
    # avoid_item classification must check variants, not just the base term.
    quote = "Avoid alcoholic beverages while taking this medication."
    assert food_guidance.classify(quote, "alcohol") == "avoid_item"


def test_water_is_not_a_tracked_item():
    assert "water" not in food_guidance.ITEM_TERMS


def test_take_with_water_then_do_not_crush_is_not_avoid_item():
    quote = (
        "Adults: take 2 tablets every 8 hours with water swallow whole; do not "
        "crush, chew, split or dissolve."
    )
    assert food_guidance.classify(quote, "water") != "avoid_item"


def test_do_not_drink_alcohol_is_avoid_item():
    quote = "Do not drink alcohol while you take sertraline hydrochloride tablets."
    assert food_guidance.classify(quote, "alcohol") == "avoid_item"


def test_avoid_grapefruit_juice_phrase_is_avoid_item():
    assert (
        food_guidance.classify("avoid grapefruit juice", "grapefruit") == "avoid_item"
    )


def test_must_not_cue_counts():
    quote = "You must not take antacid products within two hours."
    assert food_guidance.classify(quote, "antacid") == "avoid_item"


def test_do_not_crush_chew_with_item_earlier_is_not_avoid_item():
    quote = "Take with food and swallow whole. Do not crush, chew, split or dissolve."
    assert food_guidance.classify(quote, "food") != "avoid_item"
    same_sentence = "Take with milk, swallow whole, do not crush or chew."
    assert food_guidance.classify(same_sentence, "milk") != "avoid_item"


def test_cue_governing_another_verb_never_counts_for_a_later_item():
    quote = "Do not crush or chew with milk."
    assert food_guidance.classify(quote, "milk") != "avoid_item"


def test_cue_and_item_in_different_clauses_is_not_avoid_item():
    assert food_guidance.classify("Do not drink alcohol", "alcohol") == "avoid_item"
    for sep in (".", ";", ":"):
        quote = f"Do not drink{sep} alcohol is common in syrups"
        assert food_guidance.classify(quote, "alcohol") != "avoid_item"


def test_more_than_four_words_between_cue_and_item_is_not_avoid_item():
    quote = "Avoid taking this product together with any kind of alcohol."
    assert food_guidance.classify(quote, "alcohol") != "avoid_item"


def test_exactly_four_words_between_cue_and_item_is_avoid_item():
    quote = "Avoid drinking large amounts of alcohol."
    assert food_guidance.classify(quote, "alcohol") == "avoid_item"


def test_generic_take_with_no_food_word_is_not_take_with_food():
    # The old rule matched bare "take with"; the new one requires one of the
    # specific phrases.
    quote = "Take with a full glass of water."
    assert food_guidance.classify(quote, "water") != "take_with_food"


# --- Route gating ----------------------------------------------------------------


def test_topical_label_produces_nothing_and_is_unchecked(monkeypatch):
    monkeypatch.setattr(
        food_guidance,
        "fetch_labels_for_substance",
        lambda name, **kw: [topical_label(warnings=["Avoid contact with eyes."])],
    )
    item = food_guidance.RegimenItem(
        "hydrocortisone", "Hydrocortisone", False, "Hydrocortisone"
    )
    unchecked: list[str] = []

    findings = food_guidance._findings_for_item(item, unchecked)

    assert findings == []
    assert any("Hydrocortisone" in u and "route is Topical" in u for u in unchecked)


def test_unknown_route_is_unchecked_not_guessed(monkeypatch):
    monkeypatch.setattr(
        food_guidance,
        "fetch_labels_for_substance",
        lambda name, **kw: [no_route_label(warnings=["Take with food."])],
    )
    item = food_guidance.RegimenItem("mystery", "Mystery Drug", False, "Mystery Drug")
    unchecked: list[str] = []

    findings = food_guidance._findings_for_item(item, unchecked)

    assert findings == []
    assert any("route could not be determined" in u for u in unchecked)


def test_no_label_found_is_unchecked(monkeypatch):
    monkeypatch.setattr(
        food_guidance, "fetch_labels_for_substance", lambda name, **kw: []
    )
    item = food_guidance.RegimenItem("unknownium", "Unknownium", False, "Unknownium")
    unchecked: list[str] = []

    findings = food_guidance._findings_for_item(item, unchecked)

    assert findings == []
    assert any("No oral label found for Unknownium" in u for u in unchecked)


# --- Label selection (up to LABEL_SELECT_LIMIT labels, single-ingredient first) ----


def test_select_labels_prefers_single_ingredient():
    combo = oral_label(openfda={"route": ["ORAL"], "substance_name": ["A", "B"]})
    single = oral_label(openfda={"route": ["ORAL"], "substance_name": ["A"]})

    selected = food_guidance._select_labels([combo, single], limit=2, prefer_otc=False)

    assert selected == [single, combo]


def test_select_labels_prefers_otc_only_when_requested():
    rx = oral_label(
        openfda={
            "route": ["ORAL"],
            "substance_name": ["A"],
            "product_type": ["HUMAN PRESCRIPTION DRUG"],
        }
    )
    otc = oral_label(
        openfda={
            "route": ["ORAL"],
            "substance_name": ["A"],
            "product_type": ["HUMAN OTC DRUG"],
        }
    )

    assert food_guidance._select_labels([rx, otc], 2, prefer_otc=True) == [otc, rx]
    # Without prefer_otc, both are single-ingredient, so the original (stable)
    # order is kept -- OTC-ness isn't a tie-break unless asked for.
    assert food_guidance._select_labels([rx, otc], 2, prefer_otc=False) == [rx, otc]


def test_findings_for_item_reads_multiple_labels_and_dedupes_shared_quotes(
    monkeypatch, tmp_path
):
    # No curated entries for this substance -- isolates the test from
    # app/data/food_drug_guidance.json's real contents.
    monkeypatch.setattr(food_guidance, "DATA_PATH", write_curated(tmp_path, []))
    food_guidance.clear_curated_cache()

    shared_quote = "Take with food to reduce stomach upset."
    label_a = oral_label(directions=[shared_quote])
    label_b = oral_label(
        directions=[shared_quote, "Avoid alcohol while taking this medication."]
    )
    monkeypatch.setattr(
        food_guidance,
        "fetch_labels_for_substance",
        lambda name, **kw: [label_a, label_b],
    )
    item = food_guidance.RegimenItem("ibuprofen", "Ibuprofen", False, "Ibuprofen")

    findings = food_guidance._findings_for_item(item, [])
    food_guidance.clear_curated_cache()

    food_quotes = [f["quote"] for f in findings if f["item"] == "food"]
    assert food_quotes == [shared_quote]  # not duplicated across the two labels
    assert any(f["item"] == "alcohol" for f in findings)  # read from the 2nd label


# --- Combination products ---------------------------------------------------------


def test_combination_product_guidance_attributed_to_product(monkeypatch):
    combo_label = oral_label(
        openfda={"route": ["ORAL"], "substance_name": ["ACETAMINOPHEN", "DOXYLAMINE"]},
        drug_interactions=["Do not take with alcohol; avoid grapefruit juice."],
    )
    monkeypatch.setattr(
        food_guidance, "fetch_labels_for_product", lambda name, **kw: [combo_label]
    )
    item = food_guidance.RegimenItem("product:nyquil", "NyQuil", True, "NyQuil")

    findings = food_guidance._findings_for_item(item, [])

    assert findings
    assert all(f["name"] == "NyQuil" and f["is_product"] for f in findings)


def test_regimen_item_groups_single_vs_combination(monkeypatch):
    from app.registry.loader import load_registry

    registry = load_registry()
    single = food_guidance._regimen_item_for_names("Advil", ["ibuprofen"], registry)
    assert single is not None
    assert single.is_product is False
    assert single.key == "ibuprofen"

    combo = food_guidance._regimen_item_for_names(
        "NyQuil", ["acetaminophen", "doxylamine"], registry
    )
    assert combo is not None
    assert combo.is_product is True
    assert combo.key == "product:nyquil"
    assert combo.lookup_name == "NyQuil"


# --- Curated data (Source B) -----------------------------------------------------


def write_curated(tmp_path, entries):
    path = tmp_path / "food_drug_guidance.json"
    path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    return path


def test_curated_entry_without_quote_is_rejected_at_load(tmp_path):
    path = write_curated(
        tmp_path,
        [
            {
                "substance": "ibuprofen",
                "item": "food",
                "quote": "",
                "label_field": "directions",
                "date_fetched": "2026-01-01",
                "verified": False,
            },
            {
                "substance": "warfarin",
                "item": "vitamin k",
                "quote": "Maintain a consistent vitamin K intake.",
                "label_field": "information_for_patients",
                "date_fetched": "2026-01-01",
                "verified": False,
            },
        ],
    )

    entries = food_guidance.load_curated(path=path)

    assert {e.substance for e in entries} == {"warfarin"}


def test_unreachable_openfda_still_returns_curated_entries(monkeypatch, tmp_path):
    curated_path = write_curated(
        tmp_path,
        [
            {
                "substance": "warfarin",
                "item": "vitamin k",
                "quote": "Maintain a consistent vitamin K intake while on warfarin.",
                "label_field": "information_for_patients",
                "date_fetched": "2026-01-01",
                "verified": False,
            }
        ],
    )
    monkeypatch.setattr(food_guidance, "DATA_PATH", curated_path)
    food_guidance.clear_curated_cache()

    def _boom(name, **kw):
        raise food_guidance.OpenFDAUnavailable("down")

    monkeypatch.setattr(food_guidance, "fetch_labels_for_substance", _boom)
    item = food_guidance.RegimenItem("warfarin", "Warfarin", False, "Warfarin")
    unchecked: list[str] = []

    findings = food_guidance._findings_for_item(item, unchecked)

    assert any(f["source_type"] == "curated" for f in findings)
    assert any("unreachable" in u and "Warfarin" in u for u in unchecked)
    food_guidance.clear_curated_cache()


# --- Dedupe: live and curated quoting the same sentence -------------------------


def test_live_and_curated_same_quote_dedupe_to_one_fda_label_finding(
    monkeypatch, tmp_path
):
    quote = "When using this product take with food or milk if stomach upset occurs"
    label = oral_label(when_using=[quote])
    monkeypatch.setattr(
        food_guidance, "fetch_labels_for_substance", lambda name, **kw: [label]
    )

    curated_path = write_curated(
        tmp_path,
        [
            {
                "substance": "ibuprofen",
                "item": "food",
                "quote": quote,
                "label_field": "when_using",
                "date_fetched": "2026-01-01",
                "verified": False,
            }
        ],
    )
    monkeypatch.setattr(food_guidance, "DATA_PATH", curated_path)
    food_guidance.clear_curated_cache()

    item = food_guidance.RegimenItem("ibuprofen", "Ibuprofen", False, "Ibuprofen")
    findings = food_guidance._findings_for_item(item, [])

    food_findings = [f for f in findings if f["item"] == "food"]
    assert len(food_findings) == 1
    assert food_findings[0]["source_type"] == "fda_label"
    assert food_findings[0]["also_in_curated"] is True
    food_guidance.clear_curated_cache()


def test_same_quote_after_whitespace_normalization_still_dedupes(monkeypatch, tmp_path):
    live_quote = "Take   with food\nor milk if stomach upset occurs"
    curated_quote = "Take with food or milk if stomach upset occurs"
    label = oral_label(when_using=[live_quote])
    monkeypatch.setattr(
        food_guidance, "fetch_labels_for_substance", lambda name, **kw: [label]
    )

    curated_path = write_curated(
        tmp_path,
        [
            {
                "substance": "ibuprofen",
                "item": "food",
                "quote": curated_quote,
                "label_field": "when_using",
                "date_fetched": "2026-01-01",
                "verified": False,
            }
        ],
    )
    monkeypatch.setattr(food_guidance, "DATA_PATH", curated_path)
    food_guidance.clear_curated_cache()

    item = food_guidance.RegimenItem("ibuprofen", "Ibuprofen", False, "Ibuprofen")
    findings = food_guidance._findings_for_item(item, [])

    food_findings = [f for f in findings if f["item"] == "food"]
    assert len(food_findings) == 1
    assert food_findings[0]["also_in_curated"] is True
    food_guidance.clear_curated_cache()


def test_different_quotes_are_not_deduped(monkeypatch, tmp_path):
    label = oral_label(when_using=["Take with food to reduce stomach upset."])
    monkeypatch.setattr(
        food_guidance, "fetch_labels_for_substance", lambda name, **kw: [label]
    )

    curated_path = write_curated(
        tmp_path,
        [
            {
                "substance": "ibuprofen",
                "item": "food",
                "quote": "A completely different sentence about taking with food.",
                "label_field": "directions",
                "date_fetched": "2026-01-01",
                "verified": False,
            }
        ],
    )
    monkeypatch.setattr(food_guidance, "DATA_PATH", curated_path)
    food_guidance.clear_curated_cache()

    item = food_guidance.RegimenItem("ibuprofen", "Ibuprofen", False, "Ibuprofen")
    findings = food_guidance._findings_for_item(item, [])

    food_findings = [f for f in findings if f["item"] == "food"]
    assert len(food_findings) == 2
    assert {f["source_type"] for f in food_findings} == {"fda_label", "curated"}
    live = next(f for f in food_findings if f["source_type"] == "fda_label")
    assert live["also_in_curated"] is False
    food_guidance.clear_curated_cache()


# --- Wording -----------------------------------------------------------------------


def test_empty_message_matches_exactly():
    assert food_guidance.EMPTY_MESSAGE == (
        "No food, drink or timing instructions found in our sources for these "
        "medications. This does not mean none exist. Check the label and ask "
        "a pharmacist."
    )


def test_no_authored_copy_claims_safety_or_causation():
    haystack = " ".join(
        [food_guidance.EMPTY_MESSAGE, food_guidance.ONGOING_NOTE]
    ).lower()
    assert "safe" not in haystack
    assert "will cause" not in haystack


# --- Endpoints ---------------------------------------------------------------------


def test_post_food_guidance_manual_endpoint(client, openfda):
    openfda.labels["IBUPROFEN"] = oral_label(
        dosage_and_administration=["Take with food or milk if stomach upset occurs."]
    )
    body = manual_check(client, ["ibuprofen"])
    assert body["message"] is None
    assert any(f["item"] == "food" for f in body["guidance"])
    assert body["timeline"] == []


def test_post_food_guidance_unrecognized_name_is_unchecked(client, openfda):
    body = manual_check(client, ["totally not a drug"])
    assert body["guidance"] == []
    assert any("wasn't recognized" in u for u in body["unchecked_reason"])
    assert body["message"] == food_guidance.EMPTY_MESSAGE


def test_get_food_guidance_regimen_endpoint(client, openfda):
    openfda.labels["IBUPROFEN"] = oral_label(
        dosage_and_administration=["Take with food or milk if stomach upset occurs."]
    )
    register_and_log(client, "prod_advil_200", hours_ago=1)

    r = client.get("/users/default/food-guidance")

    assert r.status_code == 200, r.text
    body = r.json()
    assert any(f["item"] == "food" for f in body["guidance"])


# --- Medications only: skincare/cosmetic products are excluded ------------------


def test_skincare_product_logged_today_produces_no_unresolved_entries(client):
    search_body = client.get("/products/search", params={"q": "TIRTIR"}).json()
    product = next(
        p for p in search_body["results"] if p["source"] == "skincare_catalog"
    )
    register_and_log(client, product["id"], hours_ago=1)

    body = client.get("/users/default/food-guidance").json()

    assert not any("wasn't recognized" in u for u in body["unchecked_reason"])
    assert body["guidance"] == []


def test_medication_product_with_unresolved_ingredient_still_unchecked(client):
    openfda_module._discovered["fda_mystery"] = openfda_module.OpenFDAProduct(
        id="fda_mystery",
        brand_name="Mystery Pill",
        ingredients=(openfda_module.OpenFDAIngredient("Xyzalotine"),),
        route="ORAL",
        matched_via="name",
    )
    register_and_log(client, "fda_mystery", hours_ago=1)

    body = client.get("/users/default/food-guidance").json()

    assert any("wasn't recognized" in u for u in body["unchecked_reason"])


def test_food_guidance_never_creates_an_alert(client, openfda):
    openfda.labels["ACETAMINOPHEN"] = oral_label(
        warnings=["Avoid alcohol while taking this medication; do not drink."]
    )
    register_and_log(client, "prod_tylenol_es_500", hours_ago=1)

    r = client.get("/users/default/food-guidance")
    assert r.status_code == 200, r.text

    alerts = client.get("/users/default/alerts").json()
    assert alerts == []


# --- Timeline ----------------------------------------------------------------------


def test_timeline_take_with_food_marker_logged_and_scheduled(client, openfda):
    openfda.labels["IBUPROFEN"] = oral_label(
        dosage_and_administration=["Take with food or milk to reduce stomach upset."]
    )
    register_and_log(client, "prod_advil_200", hours_ago=1)
    add_schedule(client, "prod_advil_200", times=["08:00"])

    body = client.get("/users/default/food-guidance").json()

    markers = [
        e
        for e in body["timeline"]
        if e["type"] == "dose_marker" and e["class"] == "take_with_food"
    ]
    sources = {m["source"] for m in markers}
    assert sources == {"logged", "scheduled"}
    assert all(m["name"] == "Ibuprofen" for m in markers)


@pytest.mark.parametrize(
    "quote",
    [
        "When using this product take with food or milk if stomach upset occurs.",
        "If gastrointestinal complaints occur, administer ibuprofen tablets with "
        "meals or milk.",
        "Take with food or milk should stomach upset occur.",
        "Take with food in case of stomach upset.",
    ],
)
def test_conditional_with_food_sentence_is_take_with_food_if_upset(quote):
    assert food_guidance.classify(quote, "food") == "take_with_food_if_upset"


@pytest.mark.parametrize(
    "quote", ["Take with food.", "Take this medicine with meals or milk."]
)
def test_unconditional_with_food_sentence_stays_take_with_food(quote):
    assert food_guidance.classify(quote, "food") == "take_with_food"


def test_conditional_with_food_gets_no_timeline_marker(client, openfda):
    openfda.labels["IBUPROFEN"] = oral_label(
        dosage_and_administration=["Take with food or milk if stomach upset occurs."]
    )
    register_and_log(client, "prod_advil_200", hours_ago=1)

    body = client.get("/users/default/food-guidance").json()

    assert any(f["class"] == "take_with_food_if_upset" for f in body["guidance"])
    assert [e for e in body["timeline"] if e["type"] == "dose_marker"] == []


def test_timeline_marker_is_one_per_dose_time_substance_and_class(client, openfda):
    openfda.labels["IBUPROFEN"] = oral_label(
        dosage_and_administration=["Take with food."],
        directions=["Take with meals."],
    )
    register_and_log(client, "prod_advil_200", hours_ago=1)

    body = client.get("/users/default/food-guidance").json()

    markers = [
        e
        for e in body["timeline"]
        if e["type"] == "dose_marker" and e["class"] == "take_with_food"
    ]
    assert len(body["guidance"]) >= 2
    assert len(markers) == 1


def test_timeline_separate_from_window_drawn_around_dose(client, openfda):
    openfda.labels["ACETAMINOPHEN"] = oral_label(
        drug_interactions=[
            "Take at least 2 hours before or 4 hours after taking an antacid."
        ]
    )
    taken_at = NOW - timedelta(hours=1)
    register_and_log(client, "prod_tylenol_es_500", hours_ago=1)

    body = client.get("/users/default/food-guidance").json()

    windows = [e for e in body["timeline"] if e["type"] == "window"]
    assert len(windows) == 1
    w = windows[0]
    assert w["class"] == "separate_from"
    assert datetime.fromisoformat(w["from"]) == taken_at - timedelta(hours=2)
    assert datetime.fromisoformat(w["to"]) == taken_at + timedelta(hours=4)


def test_timeline_avoid_item_gives_ongoing_note_and_no_window(client, openfda):
    openfda.labels["DIPHENHYDRAMINE"] = oral_label(
        when_using=["Avoid alcohol while taking this product."]
    )
    register_and_log(client, "prod_benadryl_25", hours_ago=1)

    body = client.get("/users/default/food-guidance").json()

    ongoing = [e for e in body["timeline"] if e["type"] == "ongoing"]
    windows = [e for e in body["timeline"] if e["type"] == "window"]
    assert len(ongoing) == 1
    assert ongoing[0]["class"] == "avoid_item"
    assert ongoing[0]["note"] == food_guidance.ONGOING_NOTE
    assert windows == []


def test_timeline_as_needed_schedule_has_no_scheduled_markers(client, openfda):
    openfda.labels["IBUPROFEN"] = oral_label(
        dosage_and_administration=["Take with food or milk if stomach upset occurs."]
    )
    add_schedule(client, "prod_advil_200", frequency="as_needed", times=[])

    body = client.get("/users/default/food-guidance").json()

    scheduled_markers = [
        e
        for e in body["timeline"]
        if e["type"] == "dose_marker" and e.get("source") == "scheduled"
    ]
    assert scheduled_markers == []


def test_timeline_never_derived_from_concentrations(client, openfda, monkeypatch):
    openfda.labels["IBUPROFEN"] = oral_label(
        dosage_and_administration=["Take with food or milk to reduce stomach upset."]
    )
    # Schedule creation's own response legitimately calls compute_curve (to
    # show current concentration) -- set up the regimen before patching it
    # out, so only the food-guidance call itself is under test.
    register_and_log(client, "prod_advil_200", hours_ago=1)
    add_schedule(client, "prod_advil_200", times=["08:00"])

    def _boom(*args, **kwargs):
        raise AssertionError("food guidance timeline must never call compute_curve")

    monkeypatch.setattr(service, "compute_curve", _boom)

    r = client.get("/users/default/food-guidance")

    assert r.status_code == 200, r.text
    markers = [e for e in r.json()["timeline"] if e["type"] == "dose_marker"]
    assert markers  # the marker was produced without ever touching a curve


def test_timeline_empty_regimen_gives_empty_timeline(client):
    body = client.get("/users/default/food-guidance").json()
    assert body["timeline"] == []


# --- Provenance: live label findings are never "verified" -------------------------


def _ibuprofen_item():
    return food_guidance.RegimenItem("ibuprofen", "Ibuprofen", False, "Ibuprofen")


def test_live_quote_finding_is_unverified_with_quote_source(monkeypatch, tmp_path):
    monkeypatch.setattr(food_guidance, "DATA_PATH", write_curated(tmp_path, []))
    food_guidance.clear_curated_cache()
    label = oral_label(directions=["Take with food to reduce stomach upset."])
    monkeypatch.setattr(
        food_guidance, "fetch_labels_for_substance", lambda name, **kw: [label]
    )

    findings = food_guidance._findings_for_item(_ibuprofen_item(), [])
    food_guidance.clear_curated_cache()

    (finding,) = [f for f in findings if f["quote"] is not None]
    assert finding["source_type"] == "fda_label"
    assert finding["verified"] is False
    assert finding["quote_source"] == "fda_label_verbatim"


def test_live_pointer_finding_is_unverified_and_has_no_quote_source():
    pointer = food_guidance.PointerMatch("warnings", "alcohol")

    finding = food_guidance._pointer_finding(
        _ibuprofen_item(), "alcohol", pointer, "Ibuprofen", "tablet", "abc-123"
    )

    assert finding["quote"] is None
    assert finding["verified"] is False
    assert finding["quote_source"] is None


def test_curated_finding_keeps_its_own_verified_flag_and_has_no_quote_source(
    monkeypatch, tmp_path
):
    path = write_curated(
        tmp_path,
        [
            {
                "substance": "warfarin",
                "item": "vitamin k",
                "quote": "Maintain a consistent vitamin K intake while on warfarin.",
                "label_field": "information_for_patients",
                "date_fetched": "2026-01-01",
                "verified": False,
            }
        ],
    )
    monkeypatch.setattr(food_guidance, "DATA_PATH", path)
    food_guidance.clear_curated_cache()
    monkeypatch.setattr(
        food_guidance, "fetch_labels_for_substance", lambda name, **kw: []
    )
    item = food_guidance.RegimenItem("warfarin", "Warfarin", False, "Warfarin")

    findings = food_guidance._findings_for_item(item, [])
    food_guidance.clear_curated_cache()

    (finding,) = findings
    assert finding["source_type"] == "curated"
    assert finding["verified"] is False
    assert finding["quote_source"] is None


def test_no_live_finding_in_an_http_response_is_verified(client, openfda):
    openfda.labels["IBUPROFEN"] = oral_label(
        directions=["Take with food to reduce stomach upset."],
        warnings=["2 DOSAGE AND ADMINISTRATION avoid alcohol with this product"],
    )

    body = manual_check(client, ["ibuprofen"])

    live = [f for f in body["guidance"] if f["source_type"] == "fda_label"]
    assert live
    assert all(f["verified"] is False for f in live)


def test_bare_with_is_not_an_instruction_cue():
    label = oral_label(
        dosage_and_administration=[
            "A linear blood level doseresponse relationship exists with single "
            "doses up to 800 mg (See CLINICAL PHARMACOLOGY for effects of food "
            "on rate of absorption)."
        ]
    )

    quotes, pointers = food_guidance.label_matches(label, "food")

    assert quotes == []
    assert pointers == []


def test_with_food_phrase_is_still_an_instruction_cue():
    label = oral_label(directions=["Swallow tablets with food."])

    quotes, _pointers = food_guidance.label_matches(label, "food")

    assert [q.quote for q in quotes] == ["Swallow tablets with food."]


@pytest.mark.parametrize(
    "sentence",
    [
        "Take with food (See CLINICAL PHARMACOLOGY).",
        "Take with food ( 2.1 ).",
        "Take with food [see Dosage and Administration].",
    ],
)
def test_sentence_with_a_cross_reference_is_never_quoted(sentence):
    label = oral_label(dosage_and_administration=[sentence])

    quotes, pointers = food_guidance.label_matches(label, "food")

    assert quotes == []
    assert [p.field for p in pointers] == ["dosage_and_administration"]


def test_quote_after_a_section_number_heading_is_only_the_sentence():
    label = oral_label(
        drug_interactions=[
            "7.2 Administration Instructions The tablets should be taken with food."
        ]
    )

    quotes, _pointers = food_guidance.label_matches(label, "food")

    assert [q.quote for q in quotes] == ["The tablets should be taken with food."]


def test_unseparable_section_heading_becomes_a_pointer_not_a_quote():
    label = oral_label(
        drug_interactions=["7.2 Use Take with food to reduce stomach upset."]
    )

    quotes, pointers = food_guidance.label_matches(label, "food")

    assert quotes == []
    assert [p.field for p in pointers] == ["drug_interactions"]


def test_pointers_sharing_a_label_and_field_collapse_into_one_finding(client, openfda):
    openfda.labels["IBUPROFEN"] = oral_label(
        set_id="abc-123",
        openfda={"route": ["ORAL"], "brand_name": ["Advil"]},
        warnings=[
            "Take with food (See Dosage). Take with milk (See Dosage). "
            "Avoid alcohol (See Dosage)."
        ],
    )
    register_and_log(client, "prod_advil_200", hours_ago=1)

    body = client.get("/users/default/food-guidance").json()

    pointers = [f for f in body["guidance"] if f["quote"] is None]
    assert len(pointers) == 1
    assert pointers[0]["items"] == ["food", "milk", "alcohol"]
    assert pointers[0]["pointer"].startswith(
        "The FDA label for Advil mentions food, milk and alcohol in its Warnings "
        "section."
    )
    assert "dailymed.nlm.nih.gov" in pointers[0]["pointer"]


def test_food_finding_says_which_label_was_used(client, openfda):
    openfda.labels["IBUPROFEN"] = oral_label(
        set_id="adv-123",
        openfda={"route": ["ORAL"], "brand_name": ["Advil"]},
        directions=["Take with food."],
    )
    register_and_log(client, "prod_advil_200", hours_ago=1)

    body = client.get("/users/default/food-guidance").json()

    (finding,) = [f for f in body["guidance"] if f["source_type"] == "fda_label"]
    assert finding["label_attribution"] == (
        "From the label for Advil, an ibuprofen product. "
        "Not necessarily the exact product you take."
    )
    assert finding["dailymed_url"].endswith("setid=adv-123")


def test_food_pointer_says_which_label_was_used(client, openfda):
    openfda.labels["IBUPROFEN"] = oral_label(
        set_id="adv-123",
        openfda={"route": ["ORAL"], "brand_name": ["Advil"]},
        warnings=["Take with food (See Dosage)."],
    )
    register_and_log(client, "prod_advil_200", hours_ago=1)

    body = client.get("/users/default/food-guidance").json()

    (finding,) = [f for f in body["guidance"] if f["quote"] is None]
    assert finding["label_attribution"].startswith(
        "From the label for Advil, an ibuprofen product."
    )
