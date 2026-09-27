"""app.api.skincare and POST /skincare-check.

Not PK modeling: no openFDA calls happen for the ingredient checker itself.
tests/conftest.py's autouse fixture already stubs app.api.openfda to "no
matches" for every test, so any accidental network dependency would surface
as an empty result rather than a real request.
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import main, skincare
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


def test_no_source_no_entry_every_rule_is_cited():
    rules = skincare.load_rules()
    assert all(r.source for r in rules.incompatibilities)
    assert all(r.source for r in rules.known_irritants)


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
