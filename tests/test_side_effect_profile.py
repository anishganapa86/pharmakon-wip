"""GET /users/{id}/side-effect-profile and app.api.side_effects.side_effect_profile.

openFDA is never hit: every test swaps the module's HTTP client for one backed
by httpx.MockTransport, same pattern as test_symptom_check.py.
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
                return httpx.Response(
                    200, json={"results": [{"adverse_reactions": [text]}]}
                )
        return httpx.Response(
            404, json={"error": {"code": "NOT_FOUND", "message": "No matches"}}
        )


@pytest.fixture
def openfda(monkeypatch):
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


def profile(client):
    r = client.get("/users/default/side-effect-profile")
    assert r.status_code == 200, r.text
    return r.json()


def test_no_doses_returns_empty_profile(client, openfda):
    assert profile(client) == {"substances": [], "shared_effects": []}
    assert openfda.calls == []


def test_two_substances_sharing_a_side_effect_are_both_returned_and_flagged(
    client, openfda
):
    # Ibuprofen's own local list already includes "nausea"; caffeine's does
    # not, but its (mocked) FDA label does -- this is the case the reversed
    # vocabulary search exists to catch.
    openfda.labels["CAFFEINE"] = "Nausea, headache, and jitteriness were reported."
    log(client, "prod_nodoz_200", 2)  # caffeine
    log(client, "prod_advil_200", 2)  # ibuprofen

    body = profile(client)

    ids = sorted(s["substance"]["id"] for s in body["substances"])
    assert ids == ["caffeine", "ibuprofen"]

    caffeine = next(s for s in body["substances"] if s["substance"]["id"] == "caffeine")
    caffeine_effects = {e["effect"]: e["source"] for e in caffeine["side_effects"]}
    assert caffeine_effects["nausea"] == "openfda_label"

    ibuprofen = next(
        s for s in body["substances"] if s["substance"]["id"] == "ibuprofen"
    )
    ibuprofen_effects = {e["effect"]: e["source"] for e in ibuprofen["side_effects"]}
    assert ibuprofen_effects["nausea"] == "local_reference"

    assert "nausea" in body["shared_effects"]
    # Not shared: headache is only documented for caffeine in this scenario's
    # matched sources (ibuprofen's local list does list headache too, so this
    # asserts the shared set is exactly what's actually shared).
    assert set(body["shared_effects"]) == {"nausea", "headache"}


def test_contributing_products_and_level_are_named_per_substance(client, openfda):
    log(client, "prod_nodoz_200", 2, quantity=2)  # 400 mg caffeine

    (caffeine,) = profile(client)["substances"]
    assert caffeine["total_24h_mg"] == 400
    assert caffeine["threshold_24h_mg"] == 400
    assert caffeine["current_level_mg_l"] > 0
    assert caffeine["contributing_products"] == [
        {"product_id": "prod_nodoz_200", "product_name": "NoDoz"}
    ]


def test_two_products_of_the_same_substance_both_named(client, openfda):
    log(client, "prod_tylenol_es_500", 3)
    log(client, "prod_tylenol_rs_325", 1)

    (acetaminophen,) = profile(client)["substances"]
    names = {p["product_name"] for p in acetaminophen["contributing_products"]}
    assert names == {"Tylenol Extra Strength", "Tylenol Regular Strength"}


def test_doses_older_than_24h_are_excluded(client, openfda):
    log(client, "prod_nodoz_200", 30)

    assert profile(client) == {"substances": [], "shared_effects": []}


def test_substance_with_no_documented_side_effects_still_appears(client, openfda):
    # Naproxen's registry entry has an empty common_side_effects list and no
    # mocked FDA label; it should still appear, just with no side effects.
    log(client, "prod_aleve_220", 2)

    (naproxen,) = profile(client)["substances"]
    assert naproxen["substance"]["id"] == "naproxen"
    assert naproxen["side_effects"] == []


def test_no_side_effect_entry_is_verified(client, openfda):
    openfda.labels["CAFFEINE"] = "Nausea, headache, and jitteriness were reported."
    log(client, "prod_nodoz_200", 2)

    (caffeine,) = profile(client)["substances"]

    sources = {e["source"] for e in caffeine["side_effects"]}
    assert "openfda_label" in sources
    assert all(e["verified"] is False for e in caffeine["side_effects"])


def test_each_substances_label_is_requested_once_not_once_per_effect(client, openfda):
    log(client, "prod_nodoz_200", 2)
    log(client, "prod_advil_200", 2)
    log(client, "prod_tylenol_es_500", 2)

    profile(client)

    assert len(openfda.calls) == 3


def test_labels_for_different_substances_are_fetched_concurrently(
    client, openfda, monkeypatch
):
    import threading

    barrier = threading.Barrier(2, timeout=5)

    def fake(generic_name):
        barrier.wait()
        return "Nausea and headache were reported."

    monkeypatch.setattr(side_effects, "fetch_adverse_reactions", fake)
    log(client, "prod_nodoz_200", 2)
    log(client, "prod_advil_200", 2)

    body = profile(client)

    assert len(body["substances"]) == 2


def test_unreachable_openfda_is_reported_and_local_list_still_used(client, openfda):
    openfda.down = True
    log(client, "prod_nodoz_200", 2)

    body = profile(client)

    (reason,) = body["unchecked_reason"]
    assert "openFDA was unreachable" in reason
    assert "Caffeine" in reason
    (caffeine,) = body["substances"]
    assert all(e["source"] == "local_reference" for e in caffeine["side_effects"])


def test_reachable_openfda_adds_no_unchecked_reason(client, openfda):
    log(client, "prod_nodoz_200", 2)

    assert "unchecked_reason" not in profile(client)
