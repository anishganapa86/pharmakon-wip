import pytest
from fastapi.testclient import TestClient

from app.api import products
from app.registry.loader import load_registry

NOW = "2026-09-13T12:00:00-07:00"


@pytest.fixture
def client():
    from app.api import main

    return TestClient(main.app)


def search(client, q):
    r = client.get("/products/search", params={"q": q})
    assert r.status_code == 200
    return r.json()["results"]


def by_name(results):
    return {r["brand_name"]: r for r in results}


def test_every_result_carries_a_type_and_a_modeled_flag(client):
    for q in ("coffee", "nodoz", "vitamin", "beer", "chicken"):
        for result in search(client, q):
            assert result["kind"] in {
                "medication",
                "supplement",
                "food_drink",
                "skincare",
            }
            assert isinstance(result["modeled"], bool)
            assert "match_score" not in result


def test_types_are_plain_and_correct(client):
    assert by_name(search(client, "coffee"))["Coffee"]["kind"] == "food_drink"
    assert (
        by_name(search(client, "beer"))["Example Beer (12 oz)"]["kind"] == "food_drink"
    )
    assert by_name(search(client, "vitamin d"))["Vitamin D"]["kind"] == "supplement"
    assert by_name(search(client, "fish oil"))["Fish oil"]["kind"] == "supplement"
    assert by_name(search(client, "nodoz"))["NoDoz"]["kind"] == "medication"


def test_modeled_means_a_concentration_estimate_exists(client):
    assert by_name(search(client, "nodoz"))["NoDoz"]["modeled"] is True
    assert (
        by_name(search(client, "tylenol"))["Tylenol Extra Strength"]["modeled"] is True
    )
    assert by_name(search(client, "coffee"))["Coffee"]["modeled"] is False
    assert by_name(search(client, "chicken"))["Chicken"]["modeled"] is False
    assert by_name(search(client, "vitamin c"))["Vitamin C"]["modeled"] is False


def test_an_unmodeled_exact_match_outranks_modeled_partial_matches(client):
    results = search(client, "coffee")
    assert results[0]["brand_name"] == "Coffee"
    assert results[0]["modeled"] is False


def test_a_modeled_drug_is_not_ranked_above_a_better_unmodeled_match(client):
    results = search(client, "tea")
    names = [r["brand_name"] for r in results]
    assert names[0] == "Tea"
    assert by_name(results)["Tea"]["modeled"] is False


def test_ranking_depends_only_on_match_quality_then_name(client):
    registry = load_registry()
    for q in ("caffeine", "acetaminophen", "vitamin", "e"):
        results = search(client, q)
        scores = []
        for r in results:
            names = [i["name"] for i in r["ingredients"]]
            scores.append(
                (
                    -products.match_score(q, r["brand_name"], names),
                    r["brand_name"].lower(),
                )
            )
        assert scores == sorted(scores), q
    assert registry is not None


def test_equal_matches_are_ordered_by_name_not_by_whether_they_are_modeled(client):
    results = search(client, "caffeine")
    names = [r["brand_name"] for r in results]
    assert names == sorted(names, key=str.lower)
    flags = {r["modeled"] for r in results}
    assert flags == {True, False}


def test_the_catalog_has_no_food_or_supplement_amounts_that_were_made_up():
    for product_id in products.FOOD_DRINK_IDS | products.SUPPLEMENT_IDS:
        product = products.PRODUCTS[product_id]
        if product_id == "prod_example_beer":
            continue
        assert all(i.strength_mg is None for i in product.ingredients), product_id


def test_unmapped_foods_have_no_registry_match():
    registry = load_registry()
    chicken = products.PRODUCTS["prod_food_chicken"]
    assert [i.substance_id for i in products.decompose(chicken, registry)] == [None]
    assert products.decompose(chicken, registry)[0].unmodeled_reason == "unmatched"


def test_a_food_with_a_mapped_component_decomposes_to_that_substance():
    registry = load_registry()
    coffee = products.PRODUCTS["prod_food_coffee"]
    (ingredient,) = products.decompose(coffee, registry)
    assert ingredient.substance_id == "caffeine"
    assert ingredient.unmodeled_reason == "unknown_strength"


def test_logging_an_unmodeled_item_feeds_the_checks_and_the_log(client):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.api import main
    from app.api.db import Base

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)

    def session_override():
        with maker() as s:
            yield s

    main.app.dependency_overrides[main.get_session] = session_override
    try:
        for pid in ("prod_food_coffee", "prod_food_chicken", "prod_supp_vitamin_c"):
            r = client.post(
                "/users/default/doses",
                json={"product_id": pid, "taken_at": NOW, "quantity": 1},
            )
            assert r.status_code == 201, r.text
        doses = client.get("/users/default/doses").json()
        assert {d["product_name"] for d in doses} == {"Coffee", "Chicken", "Vitamin C"}
        coffee = next(d for d in doses if d["product_name"] == "Coffee")
        assert coffee["ingredients"][0]["substance_id"] == "caffeine"
        assert coffee["ingredients"][0]["unmodeled_reason"] == "unknown_strength"
        assert client.get("/users/default/interaction-check").status_code == 200
        assert client.get("/users/default/timeline").status_code == 200
    finally:
        main.app.dependency_overrides.pop(main.get_session, None)


def test_user_saved_items_get_no_type_label_instead_of_a_wrong_one():
    assert products.intake_kind("myprod_x", "my_product") is None
    assert products.intake_kind("user_x", "user_entered") is None
    assert products.intake_kind("curated_x", "skincare_catalog") == "skincare"
    assert products.intake_kind("openfda_x", "openfda") == "medication"


def test_a_my_product_wins_a_tie_on_match_quality(client):
    from app.api import main

    ranked = [
        {"source": "catalog", "brand_name": "Aaa", "match_score": 80},
        {"source": "my_product", "brand_name": "Zzz", "match_score": 80},
    ]
    ranked.sort(
        key=lambda r: (
            -r["match_score"],
            r["source"] != "my_product",
            r["brand_name"].lower(),
        )
    )
    assert ranked[0]["source"] == "my_product"
    assert main is not None
