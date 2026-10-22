"""POST /symptom-check and app.api.side_effects.

openFDA is never hit: every test swaps the module's HTTP client for one backed
by httpx.MockTransport, so the request building and response parsing are still
exercised. The clock is pinned as in test_api.
"""

from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import main, side_effects
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
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


class FakeOpenFDA:
    """Serves adverse_reactions text per generic name; 404 for anything else."""

    def __init__(self, labels: dict[str, str] | None = None, down: bool = False):
        self.labels = {k.upper(): v for k, v in (labels or {}).items()}
        self.down = down
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        search = request.url.params["search"]
        self.calls.append(search)
        if self.down:
            raise httpx.ConnectError("unreachable", request=request)
        for name, text in self.labels.items():
            if f'"{name}"' in search:
                label = (
                    text if isinstance(text, dict) else {"adverse_reactions": [text]}
                )
                return httpx.Response(200, json={"results": [label]})
        return httpx.Response(
            404, json={"error": {"code": "NOT_FOUND", "message": "No matches"}}
        )


@pytest.fixture
def openfda(monkeypatch):
    """Install a FakeOpenFDA; tests configure it through the returned object."""
    fake = FakeOpenFDA()
    monkeypatch.setattr(
        side_effects, "_http", httpx.Client(transport=httpx.MockTransport(fake))
    )
    side_effects.clear_cache()
    yield fake
    side_effects.clear_cache()


def log(client, product_id, hours_ago, quantity=1):
    r = client.post(
        "/users/default/doses",
        json={
            "product_id": product_id,
            "taken_at": (NOW - timedelta(hours=hours_ago)).isoformat(),
            "quantity": quantity,
        },
    )
    assert r.status_code == 201, r.text


def check(client, symptom):
    r = client.post("/symptom-check", json={"symptom": symptom})
    assert r.status_code == 200, r.text
    return r.json()


NOTHING = (
    "None of your current substances at elevated levels are associated with "
    "this symptom."
)


# --- The cases from the spec --------------------------------------------------


def test_caffeine_above_threshold_with_label_match_is_flagged(client, openfda):
    openfda.labels["CAFFEINE"] = "Headache, dizziness, and nausea were reported."
    log(client, "prod_nodoz_200", 2, quantity=2)  # 400 mg of a 400 mg limit

    body = check(client, "headache")

    (match,) = body["matches"]
    assert match["substance"] == {
        "id": "caffeine",
        "display_name": "Caffeine",
        "description": "Stimulant found in coffee, tea, and energy drinks",
    }
    assert match["symptom"] == "headache"
    assert match["source"] == "openfda_label"
    assert match["threshold_status"] == "above"
    assert match["total_24h_mg"] == 400
    assert match["current_level_mg_l"] > 0
    assert "Source: FDA drug label" in match["message"]
    assert "probably" not in match["message"].lower()
    # Traceable to the doses behind the level.
    assert [d["product_id"] for d in match["contributing_doses"]] == ["prod_nodoz_200"]


def test_caffeine_below_approach_fraction_stays_quiet(client, openfda):
    openfda.labels["CAFFEINE"] = "Headache, dizziness, and nausea were reported."
    log(client, "prod_nodoz_200", 2)  # 200 mg: 50% of the limit

    body = check(client, "headache")

    assert body == {"matches": [], "message": NOTHING}


def test_symptom_on_neither_label_nor_local_list_stays_quiet(client, openfda):
    openfda.labels["CAFFEINE"] = "Headache, dizziness, and nausea were reported."
    log(client, "prod_nodoz_200", 2, quantity=2)

    assert check(client, "backache") == {"matches": [], "message": NOTHING}


def test_no_doses_stays_quiet_and_never_queries(client, openfda):
    assert check(client, "headache") == {"matches": [], "message": NOTHING}
    assert openfda.calls == []


def test_two_elevated_substances_sharing_a_symptom_are_both_returned(client, openfda):
    openfda.labels["ACETAMINOPHEN"] = "Nausea, vomiting, constipation."
    openfda.labels["IBUPROFEN"] = "Most common: nausea, epigastric pain, heartburn."
    log(client, "prod_tylenol_es_500", 3, quantity=6)  # 3000 of 4000 mg
    log(client, "prod_advil_200", 2, quantity=5)  # 1000 of 1200 mg

    body = check(client, "Nausea")

    ids = sorted(m["substance"]["id"] for m in body["matches"])
    assert ids == ["acetaminophen", "ibuprofen"]
    assert all(m["source"] == "openfda_label" for m in body["matches"])
    assert all(m["threshold_status"] == "approaching" for m in body["matches"])
    assert body["message"] != NOTHING


def test_openfda_unreachable_falls_back_to_local_list(client, openfda):
    openfda.down = True
    log(client, "prod_nodoz_200", 2, quantity=2)

    (match,) = check(client, "headache")["matches"]

    assert match["source"] == "local_reference"
    assert "Source: local reference" in match["message"]


# --- Beyond the spec list -------------------------------------------------------


def test_no_label_on_openfda_falls_back_to_local_list(client, openfda):
    log(client, "prod_nodoz_200", 2, quantity=2)  # fake 404s for caffeine

    (match,) = check(client, "insomnia")["matches"]

    assert match["source"] == "local_reference"


def test_a_label_that_exists_is_authoritative_over_the_local_list(client, openfda):
    # Spec: the local list is a fallback for when openFDA returns nothing.
    openfda.labels["CAFFEINE"] = "Nausea was reported."
    log(client, "prod_nodoz_200", 2, quantity=2)

    assert check(client, "headache")["matches"] == []


def test_doses_older_than_24h_do_not_count(client, openfda):
    openfda.labels["CAFFEINE"] = "Headache."
    log(client, "prod_nodoz_200", 30, quantity=2)

    assert check(client, "headache")["matches"] == []


def test_repeat_checks_are_served_from_cache(client, openfda):
    openfda.labels["CAFFEINE"] = "Headache."
    log(client, "prod_nodoz_200", 2, quantity=2)

    check(client, "headache")
    check(client, "HEADACHE ")

    assert len(openfda.calls) == 1


def test_an_unreachable_result_is_not_cached(client, openfda):
    openfda.down = True
    log(client, "prod_nodoz_200", 2, quantity=2)
    assert check(client, "headache")["matches"][0]["source"] == "local_reference"

    openfda.down = False
    openfda.labels["CAFFEINE"] = "Headache."
    assert check(client, "headache")["matches"][0]["source"] == "openfda_label"


@pytest.mark.parametrize("symptom", ["", "  ", "a"])
def test_blank_or_trivial_symptom_is_422(client, openfda, symptom):
    r = client.post("/symptom-check", json={"symptom": symptom})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


# --- Matching -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "symptom", "expected"),
    [
        ("Headache, dizziness, and nausea", "headache", True),
        ("HEADACHE", "Headache", True),
        ("patients reported headaches", "headache", True),
        ("dry\nmouth", "dry mouth", True),
        # Word boundaries: substring hits inside other words are not findings.
        ("vehicle crash", "rash", False),
        ("paint", "pain", False),
    ],
)
def test_mentions(text, symptom, expected):
    assert side_effects.mentions(text, symptom) is expected


def test_symptom_match_is_never_verified(client, openfda):
    openfda.labels["CAFFEINE"] = "Headache, dizziness, and nausea were reported."
    log(client, "prod_nodoz_200", 2, quantity=2)

    (match,) = check(client, "headache")["matches"]

    assert match["source"] == "openfda_label"
    assert match["verified"] is False


def test_message_names_the_reference_data_limit_and_calls_it_unverified(
    client, openfda
):
    openfda.labels["CAFFEINE"] = "Headache, dizziness, and nausea were reported."
    log(client, "prod_nodoz_200", 2, quantity=2)

    (match,) = check(client, "headache")["matches"]

    assert "the 24-hour limit in our reference data (unverified)" in match["message"]
    assert "recommended" not in match["message"].lower()


def test_unreachable_openfda_is_reported_with_the_symptom_result(client, openfda):
    openfda.down = True
    log(client, "prod_nodoz_200", 2, quantity=2)

    body = check(client, "headache")

    (reason,) = body["unchecked_reason"]
    assert "openFDA was unreachable" in reason
    assert "Caffeine" in reason


def test_reachable_openfda_adds_no_unchecked_reason_to_symptom_result(client, openfda):
    openfda.labels["CAFFEINE"] = "Headache, dizziness, and nausea were reported."
    log(client, "prod_nodoz_200", 2, quantity=2)

    assert "unchecked_reason" not in check(client, "headache")


def test_label_match_says_which_label_was_used(client, openfda):
    openfda.labels["CAFFEINE"] = {
        "adverse_reactions": ["Headache, dizziness, and nausea were reported."],
        "set_id": "caf-123",
        "openfda": {"brand_name": ["NoDoz"]},
    }
    log(client, "prod_nodoz_200", 2, quantity=2)

    (match,) = check(client, "headache")["matches"]

    assert match["label_product_name"] == "NoDoz"
    assert match["label_attribution"] == (
        "From the label for NoDoz, a caffeine product. "
        "Not necessarily the exact product you take."
    )
    assert match["dailymed_url"].endswith("setid=caf-123")


def test_adverse_reactions_for_a_salt_form_generic_name_use_the_fallback_search(
    monkeypatch,
):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        search = request.url.params["search"]
        calls.append(search)
        if ".exact:" in search:
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "openfda": {"generic_name": ["DONEPEZIL HYDROCHLORIDE"]},
                        "adverse_reactions": ["Nausea and diarrhea were reported."],
                    },
                    {
                        "openfda": {
                            "generic_name": ["DONEPEZIL HYDROCHLORIDE", "MEMANTINE"]
                        },
                        "adverse_reactions": ["Combination label text."],
                    },
                ]
            },
        )

    monkeypatch.setattr(
        side_effects, "_http", httpx.Client(transport=httpx.MockTransport(handler))
    )
    side_effects.clear_cache()

    sections = side_effects.fetch_adverse_reaction_sections("donepezil")

    assert len(calls) == 2
    assert [s["text"] for s in sections] == ["Nausea and diarrhea were reported."]
    side_effects.clear_cache()


def test_label_match_shows_the_verbatim_sentence_and_dailymed_link(client, openfda):
    openfda.labels["CAFFEINE"] = {
        "adverse_reactions": [
            "Insomnia was common. Headache, dizziness, and nausea were reported."
        ],
        "set_id": "caf-123",
        "openfda": {"brand_name": ["NoDoz"]},
    }
    log(client, "prod_nodoz_200", 2, quantity=2)

    (match,) = check(client, "headache")["matches"]

    assert match["matched_sentence"] == (
        "Headache, dizziness, and nausea were reported."
    )
    assert match["quote_source"] == "fda_label_verbatim"
    assert match["label_pointer"] is None
    assert match["dailymed_url"].endswith("setid=caf-123")


def test_sentence_with_a_cross_reference_is_not_quoted_but_still_linked(
    client, openfda
):
    openfda.labels["CAFFEINE"] = {
        "adverse_reactions": ["Headache was reported (See WARNINGS)."],
        "set_id": "caf-123",
        "openfda": {"brand_name": ["NoDoz"]},
    }
    log(client, "prod_nodoz_200", 2, quantity=2)

    (match,) = check(client, "headache")["matches"]

    assert match["matched_sentence"] is None
    assert match["quote_source"] is None
    assert match["label_pointer"] == (
        "The FDA label for NoDoz mentions headache in its Adverse Reactions section."
    )
    assert match["dailymed_url"].endswith("setid=caf-123")


def test_local_reference_match_has_no_sentence_or_dailymed_link(client, openfda):
    log(client, "prod_nodoz_200", 2, quantity=2)

    (match,) = check(client, "headache")["matches"]

    assert match["source"] == "local_reference"
    assert match["matched_sentence"] is None
    assert match["label_pointer"] is None
    assert match["dailymed_url"] is None
