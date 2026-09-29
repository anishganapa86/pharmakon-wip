"""app.api.alternatives, POST /skincare-alternatives, and
GET /users/{id}/skincare-alternatives.

Curated data only: app/data/skincare_products.json is hand-sourced, never
fetched live. Tests build their own small curated set via load_products(path=)
(same pattern as app.api.skincare's load_rules(path=)) rather than depending
on the real file's contents.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import alternatives, main, skincare
from app.api.db import Base
from app.registry.loader import load_registry

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


def write_curated(tmp_path, products):
    import json

    path = tmp_path / "skincare_products.json"
    path.write_text(json.dumps({"products": products, "_README": "test fixture"}))
    return path


# Retinol alone in a product isn't flagged for anything -- an incompatibility
# needs a partner ingredient somewhere in the regimen. This companion product
# (glycolic acid, an AHA) is what makes "Retinol" in the target product an
# actual finding to look for alternatives to.
FLAGGING_COMPANION = [skincare.Product("Morning Toner", ("Glycolic Acid",))]


def curated_entry(
    brand="CleanBrand",
    name="Gentle Serum",
    category="serum",
    ingredients=("Water", "Glycerin"),
    source_url="https://example.com/product",
    date_checked="2026-09-01",
    verified=False,
):
    return {
        "brand": brand,
        "name": name,
        "category": category,
        "ingredients": list(ingredients),
        "source_url": source_url,
        "date_checked": date_checked,
        "verified": verified,
    }


# --- Loader --------------------------------------------------------------------


def test_placeholder_with_empty_ingredients_is_rejected_at_load(tmp_path):
    path = write_curated(
        tmp_path,
        [
            curated_entry(name="Real Product"),
            curated_entry(name="Placeholder", ingredients=(), source_url="TODO"),
        ],
    )

    products = alternatives.load_products(path=path)

    names = {p.name for p in products}
    assert names == {"Real Product"}


def test_entry_missing_source_url_is_rejected_at_load(tmp_path):
    entry = curated_entry(name="No Source")
    del entry["source_url"]
    path = write_curated(tmp_path, [entry])

    assert alternatives.load_products(path=path) == ()


# --- Ingredient / alias exclusion -----------------------------------------------


def test_candidate_with_the_flagged_ingredient_is_excluded(tmp_path):
    path = write_curated(
        tmp_path,
        [
            curated_entry(name="Still Has Retinol", ingredients=("Retinol", "Water")),
            curated_entry(name="Clean One", ingredients=("Water", "Glycerin")),
        ],
    )
    curated = alternatives.load_products(path=path)

    target = skincare.Product("Night Serum", ("Retinol",))
    result = alternatives.check_alternatives_for_product(
        target, FLAGGING_COMPANION, curated=curated
    )

    (group,) = result["results"]
    names = {a["name"] for a in group["alternatives"]}
    assert names == {"Clean One"}


def test_candidate_with_an_alias_of_the_flagged_ingredient_is_excluded(tmp_path):
    # "Tretinoin" is a cited alias of retinol in ingredient_interactions.json's
    # retinol/AHA incompatibility rule.
    path = write_curated(
        tmp_path,
        [
            curated_entry(name="Has Tretinoin", ingredients=("Tretinoin", "Water")),
            curated_entry(name="Clean One", ingredients=("Water", "Glycerin")),
        ],
    )
    curated = alternatives.load_products(path=path)

    target = skincare.Product("Night Serum", ("Retinol",))
    result = alternatives.check_alternatives_for_product(
        target, FLAGGING_COMPANION, curated=curated
    )

    (group,) = result["results"]
    names = {a["name"] for a in group["alternatives"]}
    assert names == {"Clean One"}


def test_candidate_with_empty_ingredient_list_is_excluded(tmp_path):
    # An entry that somehow has an ingredients key but an empty list is
    # dropped at load time (see loader tests); this proves it never reaches
    # the candidate pool at all, so it can't be excluded by content review
    # either -- absence, not a false "clean" pass.
    path = write_curated(tmp_path, [curated_entry(name="Empty", ingredients=())])
    curated = alternatives.load_products(path=path)

    assert curated == ()


def test_candidate_introducing_a_new_incompatibility_is_excluded(tmp_path):
    # Vitamin C (ascorbic acid) + AHA (glycolic acid) is a cited
    # incompatibility. The user's other product has glycolic acid; a
    # candidate containing ascorbic acid must be excluded even though it
    # doesn't contain the originally flagged ingredient (retinol) at all.
    path = write_curated(
        tmp_path,
        [
            curated_entry(
                name="Vitamin C Serum", ingredients=("Ascorbic Acid", "Water")
            ),
            curated_entry(name="Clean One", ingredients=("Water", "Glycerin")),
        ],
    )
    curated = alternatives.load_products(path=path)

    target = skincare.Product("Night Serum", ("Retinol",))
    other = [skincare.Product("Morning Toner", ("Glycolic Acid",))]
    result = alternatives.check_alternatives_for_product(target, other, curated=curated)

    (group,) = result["results"]
    names = {a["name"] for a in group["alternatives"]}
    assert names == {"Clean One"}


def test_empty_result_when_nothing_qualifies(tmp_path):
    path = write_curated(
        tmp_path, [curated_entry(name="Still Has Retinol", ingredients=("Retinol",))]
    )
    curated = alternatives.load_products(path=path)

    target = skincare.Product("Night Serum", ("Retinol",))
    result = alternatives.check_alternatives_for_product(
        target, FLAGGING_COMPANION, curated=curated
    )

    (group,) = result["results"]
    assert group["alternatives"] == []


# --- Wording ---------------------------------------------------------------------


def test_wording_never_claims_better_safer_or_recommended(tmp_path):
    path = write_curated(tmp_path, [curated_entry(name="Clean One")])
    curated = alternatives.load_products(path=path)

    target = skincare.Product("Night Serum", ("Retinol",))
    result = alternatives.check_alternatives_for_product(
        target, FLAGGING_COMPANION, curated=curated
    )

    (group,) = result["results"]
    (candidate,) = group["alternatives"]
    forbidden = ("recommended", "better", "safer", "will help", "fixes")
    haystack = (candidate["checked_note"] + " " + result["disclaimer"]).lower()
    assert not any(word in haystack for word in forbidden)
    assert "does not contain retinol" in candidate["checked_note"].lower()
    assert "no documented interactions found with your other products" in (
        candidate["checked_note"].lower()
    )
    assert result["disclaimer"] == (
        "Ingredient lists were copied from manufacturer pages and may be out "
        "of date. Check the product packaging."
    )


# --- search_url --------------------------------------------------------------------


def test_search_url_is_encoded_with_no_extra_parameters(tmp_path):
    path = write_curated(
        tmp_path, [curated_entry(brand="CeraVe", name="Foaming Cleanser & More")]
    )
    curated = alternatives.load_products(path=path)

    target = skincare.Product("Night Serum", ("Retinol",))
    result = alternatives.check_alternatives_for_product(
        target, FLAGGING_COMPANION, curated=curated
    )

    (candidate,) = result["results"][0]["alternatives"]
    assert candidate["search_url"] == (
        "https://www.google.com/search?tbm=shop&q=CeraVe+Foaming+Cleanser+%26+More"
    )


# --- Medications rejected -----------------------------------------------------


def test_medication_ingredient_name_is_rejected(client):
    r = client.post(
        "/skincare-alternatives",
        json={"products": [{"name": "Mystery Pill", "ingredients": ["Ibuprofen"]}]},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
    assert "medication" in r.json()["error"]["message"].lower()


def test_medication_ingredient_names_pure_function():
    registry = load_registry()
    bad = alternatives.medication_ingredient_names(
        ["Retinol", "Ibuprofen", "Water"], registry
    )
    assert bad == ["Ibuprofen"]


def test_skincare_ingredients_are_never_flagged_as_medications():
    registry = load_registry()
    bad = alternatives.medication_ingredient_names(
        ["Retinol", "Niacinamide", "Glycolic Acid", "Fragrance", "Water"], registry
    )
    assert bad == []


# --- API endpoints -------------------------------------------------------------


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


def test_post_skincare_alternatives_endpoint(client, monkeypatch, tmp_path):
    path = write_curated(tmp_path, [curated_entry(name="Clean One")])
    curated = alternatives.load_products(path=path)
    monkeypatch.setattr(alternatives, "load_products", lambda path=None: curated)

    r = client.post(
        "/skincare-alternatives",
        json={
            "products": [
                {"name": "Night Serum", "ingredients": ["Retinol"]},
                {"name": "Morning Toner", "ingredients": ["Glycolic Acid"]},
            ]
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["disclaimer"]
    # Both products come back flagged (retinol + glycolic acid interact both
    # ways) -- one alternatives group per flagged ingredient.
    assert len(body["results"]) == 2
    group = next(g for g in body["results"] if g["flagged_ingredient"] == "Retinol")
    assert len(group["alternatives"]) == 1


def test_get_skincare_alternatives_endpoint(client, monkeypatch, tmp_path):
    path = write_curated(tmp_path, [curated_entry(name="Clean One")])
    curated = alternatives.load_products(path=path)
    monkeypatch.setattr(alternatives, "load_products", lambda path=None: curated)

    product_id = register_and_log(client, "Night Serum", ["Retinol"])
    register_and_log(client, "Morning Toner", ["Glycolic Acid"])

    r = client.get(
        "/users/default/skincare-alternatives", params={"product_id": product_id}
    )
    assert r.status_code == 200, r.text
    # Scoped to the target product only, even though Morning Toner is also
    # flagged in the regimen.
    (group,) = r.json()["results"]
    assert group["flagged_ingredient"] == "Retinol"
    assert len(group["alternatives"]) == 1


def test_get_skincare_alternatives_rejects_non_topical_product(client):
    r = client.post(
        "/users/default/doses",
        json={
            "product_id": "prod_tylenol_es_500",
            "taken_at": NOW.isoformat(),
            "quantity": 1,
        },
    )
    assert r.status_code == 201, r.text

    r = client.get(
        "/users/default/skincare-alternatives",
        params={"product_id": "prod_tylenol_es_500"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_get_skincare_alternatives_unknown_product_is_404(client):
    r = client.get(
        "/users/default/skincare-alternatives", params={"product_id": "nope"}
    )
    assert r.status_code == 404
