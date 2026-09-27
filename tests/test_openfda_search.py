"""app.api.openfda, and GET /products/search's use of it.

openFDA is never actually hit: every test in this file installs its own
httpx.MockTransport (tests/conftest.py's autouse fixture supplies a "no
matches" stub for every other test in the suite).
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import main, openfda
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
    """Serves canned label results keyed by a substring of the `search` param.

    `routes["brand_name"] = [...]` answers any query whose `search` contains
    "brand_name" (e.g. the brand/generic-name query). Unmatched -> 404, like
    the real API's "no matches" response.
    """

    def __init__(self, routes: dict[str, list[dict]] | None = None, down: bool = False):
        self.routes = routes or {}
        self.down = down
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        search = request.url.params["search"]
        self.calls.append(search)
        if self.down:
            raise httpx.ConnectError("unreachable", request=request)
        for needle, results in self.routes.items():
            if needle in search:
                return httpx.Response(200, json={"results": results})
        return httpx.Response(
            404, json={"error": {"code": "NOT_FOUND", "message": "No matches"}}
        )


@pytest.fixture
def fake_openfda(monkeypatch):
    fake = FakeOpenFDA()
    monkeypatch.setattr(
        openfda, "_http", httpx.Client(transport=httpx.MockTransport(fake))
    )
    openfda.clear_discovered()
    yield fake
    openfda.clear_discovered()


def label(
    brand_name: str,
    substances: list[str],
    route: str | None = None,
    active_ingredient: list[str] | None = None,
    inactive_ingredient: list[str] | None = None,
) -> dict:
    meta = {"brand_name": [brand_name], "substance_name": substances}
    if route:
        meta["route"] = [route]
    result = {"openfda": meta}
    if active_ingredient:
        result["active_ingredient"] = active_ingredient
    if inactive_ingredient:
        result["inactive_ingredient"] = inactive_ingredient
    return result


# --- app.api.openfda: name search --------------------------------------------


def test_search_by_name_parses_brand_and_substances(fake_openfda):
    fake_openfda.routes["brand_name"] = [
        label("Cortizone-10", ["HYDROCORTISONE"], "TOPICAL")
    ]

    (product,) = openfda.search_by_name("cortizone")

    assert product.brand_name == "Cortizone-10"
    assert [i.name for i in product.ingredients] == ["HYDROCORTISONE"]
    assert all(i.strength_mg is None for i in product.ingredients)
    assert product.route == "TOPICAL"
    assert product.matched_via == "name"
    assert openfda.get_discovered(product.id) is product


def test_search_by_name_empty_query_makes_no_call(fake_openfda):
    assert openfda.search_by_name("   ") == []
    assert fake_openfda.calls == []


def test_search_by_name_dedupes_by_brand_name(fake_openfda):
    fake_openfda.routes["brand_name"] = [
        label("NoDoz", ["CAFFEINE"]),
        label("NoDoz", ["CAFFEINE"]),
    ]

    assert len(openfda.search_by_name("caffeine")) == 1


def test_search_by_name_down_raises(fake_openfda):
    fake_openfda.down = True
    with pytest.raises(openfda.OpenFDAUnavailable):
        openfda.search_by_name("anything")


def test_search_by_name_falls_back_to_substance_and_manufacturer(fake_openfda):
    # Nothing on brand_name/generic_name; a hit on substance_name/manufacturer.
    fake_openfda.routes["substance_name"] = [label("Amoxil", ["AMOXICILLIN"])]

    (product,) = openfda.search_by_name("amoxicillin")

    assert product.brand_name == "Amoxil"
    assert [c for c in fake_openfda.calls if "brand_name" in c]
    assert [c for c in fake_openfda.calls if "substance_name" in c]


def test_search_by_name_falls_back_to_drugsfda(fake_openfda, monkeypatch):
    drugsfda_calls: list[str] = []

    def fake_drugsfda(request: httpx.Request) -> httpx.Response:
        drugsfda_calls.append(request.url.params["search"])
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "products": [
                            {
                                "brand_name": "Old Rx Drug",
                                "active_ingredients": [{"name": "SOME SUBSTANCE"}],
                            }
                        ]
                    }
                ]
            },
        )

    # brand/generic and substance/manufacturer both come back empty; only
    # drugsfda.json (a separate mock transport) has anything.
    combined_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda req: (
                fake_drugsfda(req)
                if "drugsfda" in str(req.url)
                else httpx.Response(404, json={})
            )
        )
    )
    monkeypatch.setattr(openfda, "_http", combined_client)

    (product,) = openfda.search_by_name("old rx drug")

    assert product.brand_name == "Old Rx Drug"
    assert [i.name for i in product.ingredients] == ["SOME SUBSTANCE"]
    assert drugsfda_calls  # the drugsfda endpoint was actually queried


def test_topical_product_carries_full_ingredient_list_for_skincare_check(
    fake_openfda,
):
    fake_openfda.routes["brand_name"] = [
        label(
            "Cortizone-10",
            ["HYDROCORTISONE"],
            route="TOPICAL",
            active_ingredient=["Hydrocortisone 1%"],
            inactive_ingredient=["Water, Fragrance, Glycerin."],
        )
    ]

    (product,) = openfda.search_by_name("cortizone")

    assert product.topical_ingredients == (
        "Hydrocortisone 1%",
        "Water",
        "Fragrance",
        "Glycerin",
    )


def test_non_topical_product_has_no_topical_ingredients(fake_openfda):
    fake_openfda.routes["brand_name"] = [
        label(
            "Amoxil",
            ["AMOXICILLIN"],
            route="ORAL",
            inactive_ingredient=["Starch, Talc."],
        )
    ]

    (product,) = openfda.search_by_name("amoxil")

    assert product.topical_ingredients is None


def test_topical_product_with_no_ingredient_text_has_none(fake_openfda):
    fake_openfda.routes["brand_name"] = [label("Plain", ["SOMETHING"], route="TOPICAL")]

    (product,) = openfda.search_by_name("plain")

    assert product.topical_ingredients is None


def test_result_limit_is_ten(fake_openfda):
    fake_openfda.routes["brand_name"] = [
        label(f"Brand {i}", ["SOMETHING"]) for i in range(15)
    ]

    assert len(openfda.search_by_name("something")) == 10


# --- app.api.openfda: condition search ---------------------------------------


def test_search_by_condition_fields_checks_indications_and_purpose(fake_openfda):
    fake_openfda.routes["indications_and_usage"] = [
        label("Cortizone-10", ["HYDROCORTISONE"], "TOPICAL")
    ]
    fake_openfda.routes["purpose"] = [label("Generic Pain Relief", ["IBUPROFEN"])]

    products = openfda.search_by_condition_fields("eczema")

    names = {p.brand_name for p in products}
    assert names == {"Cortizone-10", "Generic Pain Relief"}
    assert {p.matched_via for p in products} == {
        "indications_and_usage",
        "purpose",
    }


def test_search_by_condition_fields_empty_query_makes_no_call(fake_openfda):
    assert openfda.search_by_condition_fields("") == []
    assert fake_openfda.calls == []


# --- GET /products/search: catalog + openFDA merge ---------------------------


def test_condition_search_augments_thin_catalog_results(client, fake_openfda):
    # "eczema" matches nothing in the synthetic catalog.
    fake_openfda.routes["indications_and_usage"] = [
        label("Cortizone-10", ["HYDROCORTISONE"], "TOPICAL")
    ]

    r = client.get("/products/search", params={"q": "eczema"})

    assert r.status_code == 200
    body = r.json()["results"]
    (product,) = [p for p in body if p["brand_name"] == "Cortizone-10"]
    assert product["source"] == "openfda"
    assert product["route"] == "TOPICAL"
    (ing,) = product["ingredients"]
    # Not in the registry: topical-only substances are out of scope
    # (CLAUDE.md), so this is refused, not guessed.
    assert ing["substance_id"] is None
    assert ing["strength_mg"] is None
    assert ing["unmodeled_reason"] == "unmatched"


def test_condition_fallback_is_skipped_once_catalog_has_enough_hits(
    client, fake_openfda
):
    # "acetaminophen" already matches 4 catalog products by ingredient name.
    r = client.get("/products/search", params={"q": "acetaminophen"})

    assert r.status_code == 200
    assert not any(
        "indications_and_usage" in c or "purpose" in c for c in fake_openfda.calls
    )


def test_openfda_down_degrades_to_catalog_only(client, fake_openfda):
    fake_openfda.down = True

    r = client.get("/products/search", params={"q": "nyquil"})

    assert r.status_code == 200
    # "nyquil" also matches the catalog's "NyQuil Severe" entry.
    (product,) = [p for p in r.json()["results"] if p["brand_name"] == "NyQuil"]
    assert product["source"] == "catalog"


def test_openfda_and_catalog_results_are_deduped_by_brand_name(client, fake_openfda):
    # Same brand name as the catalog's NyQuil entry; the catalog wins because
    # it's added first, so no duplicate NyQuil appears.
    fake_openfda.routes["brand_name"] = [label("NyQuil", ["ACETAMINOPHEN"])]

    r = client.get("/products/search", params={"q": "nyquil"})

    matches = [p for p in r.json()["results"] if p["brand_name"] == "NyQuil"]
    assert len(matches) == 1
    assert matches[0]["source"] == "catalog"


def test_openfda_discovered_product_is_loggable(client, fake_openfda):
    fake_openfda.routes["indications_and_usage"] = [
        label("Cortizone-10", ["HYDROCORTISONE"], "TOPICAL")
    ]
    found = client.get("/products/search", params={"q": "eczema"}).json()["results"]
    (product,) = [p for p in found if p["brand_name"] == "Cortizone-10"]

    r = client.post(
        "/users/default/doses",
        json={
            "product_id": product["id"],
            "taken_at": NOW.isoformat(),
            "quantity": 1,
        },
    )

    assert r.status_code == 201, r.text
    (dose,) = client.get("/users/default/doses").json()
    assert dose["product_name"] == "Cortizone-10"
    (ing,) = dose["ingredients"]
    assert ing["amount_mg"] is None
    assert ing["unmodeled_reason"] == "unmatched"


def test_unknown_product_id_is_still_404(client, fake_openfda):
    r = client.post(
        "/users/default/doses",
        json={
            "product_id": "openfda_doesnotexist",
            "taken_at": NOW.isoformat(),
            "quantity": 1,
        },
    )
    assert r.status_code == 404
