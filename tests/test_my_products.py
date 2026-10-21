"""app.api.my_products, and POST/GET/DELETE /users/{id}/my-products.

v1 has exactly one real user through the API (require_user rejects any id
other than "default"), so "scoped per user" is tested two ways: through the
API for CRUD/search behavior, and directly against app.api.my_products's
module functions (which take a user_id) for cross-user isolation.
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import alternatives, main, my_products, skincare
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


def create_product(
    client,
    brand="TestBrand",
    name="Test Serum",
    category="serum",
    raw_text="Water, Glycerin, Niacinamide",
):
    return client.post(
        "/users/default/my-products",
        json={"brand": brand, "name": name, "category": category, "raw_text": raw_text},
    )


# --- Splitting: the hardened splitter, reused -------------------------------------


def test_paste_with_commas_inside_parentheses_stays_whole(client):
    r = create_product(
        client, raw_text="Water, Glycerin (Kosher, Vegetable), Fragrance"
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["ingredients"] == ["Water", "Glycerin (Kosher, Vegetable)", "Fragrance"]
    # raw_text is stored exactly as pasted, never reworded.
    assert body["raw_text"] == "Water, Glycerin (Kosher, Vegetable), Fragrance"


def test_paste_with_chemical_name_comma_stays_whole(client):
    r = create_product(client, raw_text="Water, 1,2-Hexanediol, Glycerin")
    assert r.status_code == 201, r.text
    assert r.json()["ingredients"] == ["Water", "1,2-Hexanediol", "Glycerin"]


# --- CRUD --------------------------------------------------------------------------


def test_created_product_has_expected_fields(client):
    r = create_product(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["brand"] == "TestBrand"
    assert body["name"] == "Test Serum"
    assert body["category"] == "serum"
    assert body["ingredients"] == ["Water", "Glycerin", "Niacinamide"]
    assert body["id"]
    assert body["created_at"]


def test_empty_ingredient_list_is_rejected(client):
    r = create_product(client, raw_text="   ")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_invalid_category_is_rejected(client):
    r = client.post(
        "/users/default/my-products",
        json={
            "brand": "B",
            "name": "N",
            "category": "not-a-recognized-category",
            "raw_text": "Water",
        },
    )
    assert r.status_code == 422


@pytest.mark.parametrize(
    "category", ["cleanser", "toner", "serum", "moisturizer", "sunscreen", "other"]
)
def test_every_recognized_category_is_accepted(client, category):
    r = create_product(client, category=category)
    assert r.status_code == 201, r.text
    assert r.json()["category"] == category


def test_saved_product_appears_in_list(client):
    create_product(client)
    body = client.get("/users/default/my-products").json()
    assert len(body) == 1
    assert body[0]["brand"] == "TestBrand"


def test_deleted_product_disappears(client):
    created = create_product(client).json()
    r = client.delete(f"/users/default/my-products/{created['id']}")
    assert r.status_code == 204
    assert client.get("/users/default/my-products").json() == []


def test_deleting_unknown_product_is_404(client):
    r = client.delete("/users/default/my-products/myprod_doesnotexist")
    assert r.status_code == 404


# --- Search, scoped per user -------------------------------------------------------


def test_saved_product_appears_in_search(client):
    create_product(client, brand="UniqueBrandXYZ")
    body = client.get("/products/search", params={"q": "UniqueBrandXYZ"}).json()
    result = next(p for p in body["results"] if p["source"] == "my_product")
    assert result["brand_name"] == "UniqueBrandXYZ Test Serum"
    assert result["label"] == "My product (entered by you)"
    assert result["verification_note"] == my_products.UNVERIFIED_NOTE


def test_deleted_product_no_longer_appears_in_search(client):
    created = create_product(client, brand="UniqueBrandXYZ").json()
    client.delete(f"/users/default/my-products/{created['id']}")
    body = client.get("/products/search", params={"q": "UniqueBrandXYZ"}).json()
    assert body["results"] == []


def test_my_products_are_scoped_per_user():
    """Direct module-level test: two different user_ids against the same
    session, since v1's own API only ever serves "default"."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as session:
        my_products.create(session, "alice", "BrandA", "Serum A", "serum", "Water", NOW)
        my_products.create(session, "bob", "BrandB", "Serum B", "serum", "Water", NOW)
        session.commit()

        alice_products = my_products.list_for_user(session, "alice")
        bob_products = my_products.list_for_user(session, "bob")

        assert [p.brand for p in alice_products] == ["BrandA"]
        assert [p.brand for p in bob_products] == ["BrandB"]
        assert my_products.search(alice_products, "BrandB") == []

        alice_id = alice_products[0].id
        assert my_products.get(session, "bob", alice_id) is None
        assert my_products.delete(session, "bob", alice_id) is False
        assert my_products.get(session, "alice", alice_id) is not None


# --- Logging ------------------------------------------------------------------------


def test_logging_a_saved_product_creates_a_dose_with_stored_ingredients(client):
    created = create_product(
        client, raw_text="Water, Niacinamide, Centella Asiatica Extract"
    ).json()

    r = client.post(
        "/users/default/doses",
        json={
            "product_id": created["id"],
            "taken_at": NOW.isoformat(),
            "quantity": 1,
        },
    )
    assert r.status_code == 201, r.text

    (dose,) = client.get("/users/default/doses").json()
    assert dose["product_name"] == "TestBrand Test Serum"
    assert [i["name"] for i in dose["ingredients"]] == [
        "Water",
        "Niacinamide",
        "Centella Asiatica Extract",
    ]

    check = client.get("/users/default/skincare-check").json()
    assert any(p["name"] == "TestBrand Test Serum" for p in check["products"])


# --- Never an alternative candidate --------------------------------------------------


def test_my_product_never_appears_as_an_alternative_candidate(client):
    """A my-product with exactly the shape that WOULD make it an attractive
    "alternative" (doesn't contain the flagged ingredient, introduces no new
    flag) must still never appear -- app/api/alternatives.py's candidate
    pool is built only from app/data/skincare_products.json and never reads
    the user_products table, for anyone, including the user who saved it."""
    create_product(
        client,
        brand="WouldBeGreatAlternative",
        name="Clean Serum",
        category="serum",
        raw_text="Water, Glycerin",
    )

    target = skincare.Product("Night Serum", ("Retinol",))
    other = [skincare.Product("Morning Toner", ("Glycolic Acid",))]
    result = alternatives.check_alternatives_for_product(target, other)

    for group in result["results"]:
        brands = {a["brand"] for a in group["alternatives"]}
        assert "WouldBeGreatAlternative" not in brands


# --- The add-a-product flow end to end ------------------------------------------

FLOW_TEXT = "Water, Glycerin (Kosher, Vegetable), 1,2-Hexanediol, Retinol"


def test_search_finds_a_saved_product_by_brand_and_name_together(client):
    created = create_product(
        client,
        brand="Test",
        name="Test Cream",
        category="moisturizer",
        raw_text=FLOW_TEXT,
    ).json()

    r = client.get("/products/search", params={"q": "Test Test Cream"})

    assert r.status_code == 200, r.text
    assert [p["id"] for p in r.json()["results"]] == [created["id"]]


def test_saved_product_can_be_found_logged_and_checked_end_to_end(client):
    r = create_product(
        client,
        brand="Test",
        name="Test Cream",
        category="moisturizer",
        raw_text=FLOW_TEXT,
    )
    assert r.status_code == 201, r.text
    saved = r.json()
    assert len(saved["ingredients"]) == 4

    found = client.get("/products/search", params={"q": "Test Cream"}).json()["results"]
    (product,) = [p for p in found if p["id"] == saved["id"]]
    assert product["source"] == "my_product"
    assert product["route"] == "TOPICAL"
    assert len(product["ingredients"]) == 4

    dose = client.post(
        "/users/default/doses",
        json={
            "product_id": saved["id"],
            "taken_at": NOW.isoformat(),
            "quantity": 1,
        },
    )
    assert dose.status_code == 201, dose.text

    logged = client.get("/users/default/doses").json()
    assert [i["name"] for i in logged[0]["ingredients"]] == saved["ingredients"]

    check = client.get("/users/default/skincare-check").json()
    assert check["products"] == [
        {"name": "Test Test Cream", "ingredients": saved["ingredients"]}
    ]
