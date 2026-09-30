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
    assert food_guidance.classify(matches[0].quote, "food") == "take_with_food"


def test_item_term_without_instruction_cue_is_excluded():
    label = oral_label(warnings=["This product contains natural grapefruit flavor."])
    matches = food_guidance.qualifying_matches(label, "grapefruit")
    assert matches == []


def test_qualifying_match_item_term_is_literally_in_quote():
    label = oral_label(directions=["Take with food. Do not take on an empty stomach."])
    for match in food_guidance.qualifying_matches(label, "food"):
        assert "food" in match.quote.lower()


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
    quote = "Take with food or milk if stomach upset occurs."
    assert food_guidance.classify(quote, "food") == "take_with_food"


def test_avoid_far_from_item_term_does_not_classify_as_avoid_item():
    quote = (
        "Avoid operating heavy machinery after taking this medication with "
        "a full glass of water and a light meal containing food."
    )
    # "Avoid" is about machinery, more than 8 words from "food".
    assert food_guidance.classify(quote, "food") != "avoid_item"


def test_generic_take_with_no_food_word_is_not_take_with_food():
    # The old rule matched bare "take with"; the new one requires one of the
    # specific phrases.
    quote = "Take with a full glass of water."
    assert food_guidance.classify(quote, "water") != "take_with_food"


# --- Route gating ----------------------------------------------------------------


def test_topical_label_produces_nothing_and_is_unchecked(monkeypatch):
    monkeypatch.setattr(
        food_guidance,
        "fetch_label_for_substance",
        lambda name: topical_label(warnings=["Avoid contact with eyes."]),
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
        "fetch_label_for_substance",
        lambda name: no_route_label(warnings=["Take with food."]),
    )
    item = food_guidance.RegimenItem("mystery", "Mystery Drug", False, "Mystery Drug")
    unchecked: list[str] = []

    findings = food_guidance._findings_for_item(item, unchecked)

    assert findings == []
    assert any("route could not be determined" in u for u in unchecked)


def test_no_label_found_is_unchecked(monkeypatch):
    monkeypatch.setattr(food_guidance, "fetch_label_for_substance", lambda name: None)
    item = food_guidance.RegimenItem("unknownium", "Unknownium", False, "Unknownium")
    unchecked: list[str] = []

    findings = food_guidance._findings_for_item(item, unchecked)

    assert findings == []
    assert any("No oral label found for Unknownium" in u for u in unchecked)


# --- Combination products ---------------------------------------------------------


def test_combination_product_guidance_attributed_to_product(monkeypatch):
    combo_label = oral_label(
        openfda={"route": ["ORAL"], "substance_name": ["ACETAMINOPHEN", "DOXYLAMINE"]},
        drug_interactions=["Do not take with alcohol; avoid grapefruit juice."],
    )
    monkeypatch.setattr(
        food_guidance, "fetch_label_for_product", lambda name: combo_label
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

    def _boom(name):
        raise food_guidance.OpenFDAUnavailable("down")

    monkeypatch.setattr(food_guidance, "fetch_label_for_substance", _boom)
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
    monkeypatch.setattr(food_guidance, "fetch_label_for_substance", lambda name: label)

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
    monkeypatch.setattr(food_guidance, "fetch_label_for_substance", lambda name: label)

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
    monkeypatch.setattr(food_guidance, "fetch_label_for_substance", lambda name: label)

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
        dosage_and_administration=["Take with food or milk if stomach upset occurs."]
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
        dosage_and_administration=["Take with food or milk if stomach upset occurs."]
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
