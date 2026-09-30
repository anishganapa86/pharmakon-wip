"""app.api.skincare, POST /skincare-check, and GET /users/{id}/skincare-check.

Not PK modeling: no openFDA calls happen for the ingredient checker itself.
tests/conftest.py's autouse fixture already stubs app.api.openfda to "no
matches" for every test, so any accidental network dependency would surface
as an empty result rather than a real request.
"""

from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import main, openfda, skincare
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
    skincare.clear_user_products()
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()
    skincare.clear_user_products()


# --- app.api.skincare.check_ingredients (pure function) -----------------------


def test_retinol_and_glycolic_acid_incompatibility_is_flagged():
    products = [
        skincare.Product("Night Serum", ("Retinol",)),
        skincare.Product("Morning Toner", ("Glycolic Acid",)),
    ]

    result = skincare.check_ingredients(products)

    (found,) = result["incompatibilities"]
    assert {found["ingredient_a"], found["ingredient_b"]} == {
        "Retinol",
        "Glycolic Acid",
    }
    assert "strips the skin barrier" in found["effect"]
    assert found["source"] == "American Academy of Dermatology guidelines"
    assert result["irritants"] == []


def test_fragrance_is_flagged_as_an_irritant():
    products = [skincare.Product("Body Lotion", ("Water", "Fragrance", "Glycerin"))]

    result = skincare.check_ingredients(products)

    (found,) = result["irritants"]
    assert found["ingredient"] == "Fragrance"
    assert found["product"] == "Body Lotion"
    assert found["source"] == "Contact Dermatitis, de Groot 2009"
    clear_names = {c["ingredient"] for c in result["clear"]}
    assert clear_names == {"Water", "Glycerin"}


def test_result_lists_each_products_ingredients():
    products = [
        skincare.Product("Serum", ("Niacinamide", "Water")),
        skincare.Product("Moisturizer", ("Hyaluronic Acid",)),
    ]

    result = skincare.check_ingredients(products)

    assert result["products"] == [
        {"name": "Serum", "ingredients": ["Niacinamide", "Water"]},
        {"name": "Moisturizer", "ingredients": ["Hyaluronic Acid"]},
    ]


def test_compatible_products_have_no_flags_and_show_clear():
    products = [
        skincare.Product("Serum", ("Niacinamide",)),
        skincare.Product("Moisturizer", ("Hyaluronic Acid",)),
    ]

    result = skincare.check_ingredients(products)

    assert result["incompatibilities"] == []
    assert result["irritants"] == []
    clear_names = {c["ingredient"] for c in result["clear"]}
    assert clear_names == {"Niacinamide", "Hyaluronic Acid"}


def test_aliases_are_matched():
    products = [
        skincare.Product("Cream", ("Tretinoin",)),
        skincare.Product("Wash", ("Lactic Acid",)),
    ]

    result = skincare.check_ingredients(products)

    assert len(result["incompatibilities"]) == 1


def test_messy_openfda_ingredient_string_matches_alias_by_substring():
    # Real openFDA label text: dosage form, purpose, and product wording
    # bracketing the actual ingredient name.
    products = [
        skincare.Product("ZO Retinol", ("Retinol",)),
        skincare.Product(
            "ZO Exfoliating Cleanser",
            ("Purpose Salicylic Acid 2.0% Acne Medication",),
        ),
    ]

    result = skincare.check_ingredients(products)

    (found,) = result["incompatibilities"]
    assert {found["ingredient_a"], found["ingredient_b"]} == {
        "Retinol",
        "Purpose Salicylic Acid 2.0% Acne Medication",
    }
    assert found["source"] == "American Academy of Dermatology"


def test_messy_ingredient_string_does_not_match_unrelated_rule():
    # "Isostearyl Alcohol" is a fatty alcohol; the irritant rule is for
    # "denatured alcohol" specifically (see its own effect text), and
    # substring matching must not fire on the word "alcohol" alone.
    products = [skincare.Product("Moisturizer", ("Isostearyl Alcohol",))]

    result = skincare.check_ingredients(products)

    assert result["irritants"] == []
    clear_names = {c["ingredient"] for c in result["clear"]}
    assert clear_names == {"Isostearyl Alcohol"}


def test_filler_words_alone_do_not_match_a_rule():
    # "Active", "Inactive", "Purpose", "Medication" are stripped as filler --
    # they must not, on their own, satisfy a rule that expects real content.
    products = [skincare.Product("Product", ("Active Ingredients", "Inactive"))]

    result = skincare.check_ingredients(products)

    assert result["incompatibilities"] == []
    assert result["irritants"] == []


def test_no_source_no_entry_every_rule_is_cited():
    rules = skincare.load_rules()
    assert all(r.source for r in rules.incompatibilities)
    assert all(r.source for r in rules.known_irritants)


# --- matched_ingredient / matched_alias ----------------------------------------


def test_incompatibility_reports_matched_ingredient_and_alias_per_side():
    # "Tretinoin" is a cited alias of retinol; "Lactic Acid" is a cited alias
    # of alpha hydroxy acid -- neither side matched via its canonical name.
    products = [
        skincare.Product("Cream", ("Tretinoin",)),
        skincare.Product("Wash", ("Lactic Acid",)),
    ]

    result = skincare.check_ingredients(products)

    (found,) = result["incompatibilities"]
    sides = {
        (found["matched_ingredient_a"], found["matched_alias_a"]),
        (found["matched_ingredient_b"], found["matched_alias_b"]),
    }
    assert sides == {("Tretinoin", "tretinoin"), ("Lactic Acid", "lactic acid")}


def test_incompatibility_matched_alias_is_the_messy_string_that_hit():
    products = [
        skincare.Product("ZO Retinol", ("Retinol",)),
        skincare.Product(
            "ZO Exfoliating Cleanser",
            ("Purpose Salicylic Acid 2.0% Acne Medication",),
        ),
    ]

    result = skincare.check_ingredients(products)

    (found,) = result["incompatibilities"]
    aliases = {found["matched_alias_a"], found["matched_alias_b"]}
    assert aliases == {"retinol", "salicylic acid"}
    matched_ingredients = {
        found["matched_ingredient_a"],
        found["matched_ingredient_b"],
    }
    assert matched_ingredients == {
        "Retinol",
        "Purpose Salicylic Acid 2.0% Acne Medication",
    }


def test_irritant_reports_matched_ingredient_and_alias():
    products = [skincare.Product("Body Lotion", ("Parfum",))]

    result = skincare.check_ingredients(products)

    (found,) = result["irritants"]
    assert found["matched_ingredient"] == "Parfum"
    assert found["matched_alias"] == "parfum"


# --- same-product vs. two-product recommendation --------------------------------


def test_same_product_incompatibility_gets_pharmacist_recommendation():
    products = [
        skincare.Product("Eva Serum", ("Retinol", "Salicylic Acid")),
    ]

    result = skincare.check_ingredients(products)

    (found,) = result["incompatibilities"]
    assert found["product_a"] == found["product_b"] == "Eva Serum"
    assert (
        "Ask a pharmacist or dermatologist about this product."
        in (found["recommendation"])
    )
    assert "The source says: " in found["recommendation"]
    assert "Use on alternate nights." in found["recommendation"]
    # Effect text is untouched -- only recommendation is rewritten.
    assert found["effect"] == (
        "Both cause dryness and exfoliation. Combined use increases risk of "
        "irritation, peeling, and barrier damage."
    )


def test_two_product_incompatibility_keeps_original_recommendation():
    products = [
        skincare.Product("Night Serum", ("Retinol",)),
        skincare.Product("Morning Wash", ("Salicylic Acid",)),
    ]

    result = skincare.check_ingredients(products)

    (found,) = result["incompatibilities"]
    assert found["recommendation"] == "Use on alternate nights."


# --- rules_checked ---------------------------------------------------------------


def test_rules_checked_counts_incompatibilities_and_irritants():
    rules = skincare.load_rules()
    expected = len(rules.incompatibilities) + len(rules.known_irritants)

    result = skincare.check_ingredients([skincare.Product("P", ("Water",))])

    assert result["rules_checked"] == expected


# --- POST /skincare-check -------------------------------------------------------


def test_post_skincare_check_endpoint(client):
    r = client.post(
        "/skincare-check",
        json={
            "products": [
                {"name": "A", "ingredients": ["Retinol"]},
                {"name": "B", "ingredients": ["Glycolic Acid"]},
            ]
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["incompatibilities"]) == 1


# --- GET /users/{id}/skincare-check ----------------------------------------------


def register_and_log(client, name, ingredients, hours_ago=1):
    created = client.post(
        "/products/user-entered", json={"name": name, "ingredients": ingredients}
    )
    assert created.status_code == 201, created.text
    product_id = created.json()["id"]
    r = client.post(
        "/users/default/doses",
        json={
            "product_id": product_id,
            "taken_at": (NOW - timedelta(hours=hours_ago)).isoformat(),
            "quantity": 1,
        },
    )
    assert r.status_code == 201, r.text
    return product_id


def test_get_skincare_check_finds_products_logged_in_last_24h(client):
    register_and_log(client, "Night Serum", ["Retinol"], hours_ago=2)
    register_and_log(client, "Morning Toner", ["Glycolic Acid"], hours_ago=1)

    r = client.get("/users/default/skincare-check")

    assert r.status_code == 200, r.text
    assert len(r.json()["incompatibilities"]) == 1


def test_get_skincare_check_ignores_doses_older_than_24h(client):
    register_and_log(client, "Night Serum", ["Retinol"], hours_ago=30)
    register_and_log(client, "Morning Toner", ["Glycolic Acid"], hours_ago=1)

    r = client.get("/users/default/skincare-check")

    assert r.json()["incompatibilities"] == []


def test_sls_irritant_flagged_and_annotated_when_eczema_logged_recently(client):
    register_and_log(client, "Face Wash", ["Sodium Lauryl Sulfate"])
    log_r = client.post(
        "/users/default/log",
        json={
            "category": "skin",
            "value": "Noticed my eczema flaring up today",
        },
    )
    assert log_r.status_code == 201, log_r.text

    r = client.get("/users/default/skincare-check")

    (found,) = r.json()["irritants"]
    assert found["ingredient"] == "Sodium Lauryl Sulfate"
    assert "eczema" in found["note"].lower()


def test_sls_irritant_flagged_without_note_when_no_eczema_mention(client):
    register_and_log(client, "Face Wash", ["Sodium Lauryl Sulfate"])

    r = client.get("/users/default/skincare-check")

    (found,) = r.json()["irritants"]
    assert "note" not in found


def test_openfda_discovered_topical_product_is_checked_automatically(
    client, monkeypatch
):
    def fake_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "openfda": {
                            "brand_name": ["Cortizone-10"],
                            "substance_name": ["HYDROCORTISONE"],
                            "route": ["TOPICAL"],
                        },
                        "inactive_ingredient": ["Fragrance, Water."],
                    }
                ]
            },
        )

    monkeypatch.setattr(
        openfda, "_http", httpx.Client(transport=httpx.MockTransport(fake_transport))
    )
    openfda.clear_discovered()

    found = client.get("/products/search", params={"q": "cortizone"}).json()["results"]
    (product,) = [p for p in found if p["brand_name"] == "Cortizone-10"]

    r = client.post(
        "/users/default/doses",
        json={"product_id": product["id"], "taken_at": NOW.isoformat(), "quantity": 1},
    )
    assert r.status_code == 201, r.text

    check = client.get("/users/default/skincare-check").json()
    (found_irritant,) = check["irritants"]
    assert found_irritant["ingredient"] == "Fragrance"

    openfda.clear_discovered()


def test_products_without_stored_ingredients_are_skipped(client):
    # A regular (non-topical, non-user-entered) catalog dose has no
    # topical_ingredients -- it must not show up in the skincare check at all.
    r = client.post(
        "/users/default/doses",
        json={
            "product_id": "prod_tylenol_es_500",
            "taken_at": NOW.isoformat(),
            "quantity": 1,
        },
    )
    assert r.status_code == 201, r.text

    r = client.get("/users/default/skincare-check")
    assert r.json() == {
        "products": [],
        "incompatibilities": [],
        "irritants": [],
        "clear": [],
        "rules_checked": len(skincare.load_rules().incompatibilities)
        + len(skincare.load_rules().known_irritants),
    }
