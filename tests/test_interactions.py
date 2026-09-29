"""app.api.interactions, POST /interaction-check, and
GET /users/{id}/interaction-check.

openFDA is never actually reached: every test installs its own
httpx.MockTransport (tests/conftest.py's autouse fixture already stubs
app.api.openfda, but app.api.interactions keeps its own _http client and
cache, same pattern as app/api/side_effects.py).
"""

from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import interactions, main
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
    """Serves drug_interactions text per generic name; 404 for anything else."""

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
                return httpx.Response(
                    200, json={"results": [{"drug_interactions": [text]}]}
                )
        return httpx.Response(
            404, json={"error": {"code": "NOT_FOUND", "message": "No matches"}}
        )


@pytest.fixture
def openfda(monkeypatch):
    fake = FakeOpenFDA()
    monkeypatch.setattr(
        interactions, "_http", httpx.Client(transport=httpx.MockTransport(fake))
    )
    interactions.clear_cache()
    yield fake
    interactions.clear_cache()


def manual_check(client, names):
    r = client.post("/interaction-check", json={"names": names})
    assert r.status_code == 200, r.text
    return r.json()


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


# --- Curated rules -----------------------------------------------------------


def test_ibuprofen_and_naproxen_flagged_as_duplicate_nsaid(client, openfda):
    body = manual_check(client, ["ibuprofen", "naproxen"])

    curated = [f for f in body["interactions"] if f["source_type"] == "curated"]
    assert len(curated) == 1
    finding = curated[0]
    ids = {finding["substance_a"]["id"], finding["substance_b"]["id"]}
    assert ids == {"ibuprofen", "naproxen"}
    assert "NSAID" in finding["effect"] or "GI bleeding" in finding["effect"]
    assert finding["source"]
    assert finding["verified"] is False
    assert finding["severity"] is None
    assert body["checked_pairs"] == 1


def test_sertraline_and_dextromethorphan_flagged(client, openfda):
    body = manual_check(client, ["sertraline", "dextromethorphan"])

    curated = [f for f in body["interactions"] if f["source_type"] == "curated"]
    assert len(curated) == 1
    ids = {curated[0]["substance_a"]["id"], curated[0]["substance_b"]["id"]}
    assert ids == {"sertraline", "dextromethorphan"}
    assert "serotonin" in curated[0]["effect"].lower()


def test_no_source_no_entry_every_rule_is_cited():
    rules = interactions.load_rules()
    assert all(r.source for r in rules)
    assert all(r.verified is False for r in rules)


def test_rule_with_no_source_is_rejected_at_load_time(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(
        '{"rules": [{"substance_a": "ibuprofen", "substance_b": "warfarin", '
        '"effect": "x", "recommendation": "y", "source": "", "verified": false}]}'
    )
    with pytest.raises(interactions.InvalidRule):
        interactions.load_rules(path=bad)


# --- FDA label text ------------------------------------------------------------


def test_label_text_mentioning_the_other_drug_is_flagged_with_quote(client, openfda):
    openfda.labels["LISINOPRIL"] = (
        "Diuretics: excessive reduction of blood pressure may occur. "
        "NSAIDs: may reduce the antihypertensive effect of lisinopril."
    )

    body = manual_check(client, ["lisinopril", "ibuprofen"])

    label_findings = [
        f for f in body["interactions"] if f["source_type"] == "fda_label"
    ]
    assert len(label_findings) == 1
    finding = label_findings[0]
    assert finding["substance_a"]["id"] == "lisinopril"
    assert finding["substance_b"]["id"] == "ibuprofen"
    assert "NSAIDs" in finding["quoted_text"]
    assert len(finding["quoted_text"]) <= 300
    assert finding["verified"] is True
    assert "The FDA label for Lisinopril mentions Ibuprofen" in finding["message"]


def test_label_text_not_mentioning_the_other_drug_is_not_flagged(client, openfda):
    openfda.labels["LORATADINE"] = "CYP3A4 and CYP2D6 inhibitors may increase levels."
    openfda.labels["FAMOTIDINE"] = "Antacids may decrease absorption of famotidine."

    body = manual_check(client, ["loratadine", "famotidine"])

    assert body["interactions"] == []


# --- Empty result --------------------------------------------------------------


def test_two_unrelated_drugs_returns_exact_empty_message(client, openfda):
    body = manual_check(client, ["loratadine", "famotidine"])

    assert body["interactions"] == []
    assert body["message"] == (
        "No documented interactions found in our sources. This does not mean "
        "none exist. Ask a pharmacist to review your full medication list."
    )


def test_nonempty_result_has_no_top_level_message(client, openfda):
    body = manual_check(client, ["ibuprofen", "naproxen"])

    assert body["interactions"] != []
    assert body["message"] is None


# --- Unresolved ingredients ------------------------------------------------------


def test_unrecognized_ingredient_appears_under_unchecked_reason(client, openfda):
    body = manual_check(client, ["ibuprofen", "Unobtainium"])

    assert any("Unobtainium" in reason for reason in body["unchecked_reason"])


# --- openFDA unreachable ---------------------------------------------------------


def test_openfda_unreachable_curated_rules_still_work(client, openfda):
    openfda.down = True

    body = manual_check(client, ["ibuprofen", "naproxen"])

    curated = [f for f in body["interactions"] if f["source_type"] == "curated"]
    assert len(curated) == 1
    assert any(
        "openFDA was unreachable" in reason for reason in body["unchecked_reason"]
    )


# --- Concentration overlap -------------------------------------------------------


def test_overlap_computed_for_two_modeled_drugs(client, openfda):
    log(client, "prod_advil_200", hours_ago=2)  # ibuprofen, full modelability
    log(client, "prod_aleve_220", hours_ago=2)  # naproxen, full modelability

    body = client.get("/users/default/interaction-check").json()

    (finding,) = [f for f in body["interactions"] if f["source_type"] == "curated"]
    assert finding["overlap"] is not None
    (window,) = finding["overlap"]
    assert window["estimate"] is True
    assert "from" in window and "to" in window


def test_overlap_absent_when_one_substance_is_unmodeled(client, openfda):
    # ethanol is zero-order kinetics (UnsupportedKinetics), so its
    # modelability is never "full" -- compute_overlap must refuse to guess a
    # window for it no matter what the other substance in the pair looks like.
    log(client, "prod_example_beer", hours_ago=1)  # ethanol
    r = client.post(
        "/products/user-entered",
        json={"name": "Metformin 500mg", "ingredients": ["Metformin"]},
    )
    assert r.status_code == 201, r.text
    log(client, r.json()["id"], hours_ago=1)

    body = client.get("/users/default/interaction-check").json()

    curated = [
        f
        for f in body["interactions"]
        if {f["substance_a"]["id"], f["substance_b"]["id"]} == {"metformin", "ethanol"}
    ]
    assert curated
    assert curated[0]["overlap"] is None


# --- GET /users/{id}/interaction-check -------------------------------------------


def test_get_interaction_check_uses_regimen_doses_and_schedule(client, openfda):
    log(client, "prod_advil_200", hours_ago=1)
    r = client.post(
        "/users/default/schedule",
        json={
            "product_id": "prod_aleve_220",
            "frequency": "daily",
            "times": ["09:00"],
        },
    )
    assert r.status_code == 201, r.text

    body = client.get("/users/default/interaction-check").json()

    ids = {
        sid
        for f in body["interactions"]
        for sid in (f["substance_a"]["id"], f["substance_b"]["id"])
    }
    assert {"ibuprofen", "naproxen"} <= ids


def test_get_interaction_check_ignores_doses_older_than_24h(client, openfda):
    log(client, "prod_advil_200", hours_ago=30)
    log(client, "prod_aleve_220", hours_ago=1)

    body = client.get("/users/default/interaction-check").json()

    assert body["interactions"] == []


def test_manual_check_requires_at_least_one_name(client, openfda):
    r = client.post("/interaction-check", json={"names": []})
    assert r.status_code == 422
