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
    source_type=None,
    reference_url=None,
):
    return {
        "brand": brand,
        "name": name,
        "category": category,
        "ingredients": list(ingredients),
        "source_url": source_url,
        "date_checked": date_checked,
        "verified": verified,
        "source_type": source_type,
        "reference_url": reference_url,
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


def test_packaging_entry_with_no_source_url_is_loaded(tmp_path):
    path = write_curated(
        tmp_path,
        [
            curated_entry(
                name="Packaging Product", source_type="packaging", source_url=None
            )
        ],
    )

    products = alternatives.load_products(path=path)

    names = {p.name for p in products}
    assert names == {"Packaging Product"}


# --- source_type: category lookup vs. candidate eligibility --------------------


def test_category_lookup_works_for_a_packaging_product(tmp_path):
    # No source_url at all -- would have been rejected before source_type
    # existed. It's still usable to recognize the flagged product as one of
    # our own and infer its category.
    path = write_curated(
        tmp_path,
        [
            curated_entry(
                brand="TIRTIR",
                name="SOS Serum",
                category="serum",
                ingredients=("Retinol", "Water"),
                source_type="packaging",
                source_url=None,
                verified=False,
                reference_url=None,
            )
        ],
    )
    curated = alternatives.load_products(path=path)

    target = skincare.Product("TIRTIR SOS Serum", ("Retinol",))
    result = alternatives.check_alternatives_for_product(
        target, FLAGGING_COMPANION, curated=curated
    )

    (group,) = result["results"]
    assert group["category"] == "serum"


def test_unverified_packaging_entry_is_excluded_from_candidates(tmp_path):
    path = write_curated(
        tmp_path,
        [
            curated_entry(
                name="Unverified Packaging",
                ingredients=("Water", "Glycerin"),
                source_type="packaging",
                source_url=None,
                verified=False,
                reference_url="https://retailer.example.com/product",
            )
        ],
    )
    curated = alternatives.load_products(path=path)

    target = skincare.Product("Night Serum", ("Retinol",))
    result = alternatives.check_alternatives_for_product(
        target, FLAGGING_COMPANION, curated=curated
    )

    (group,) = result["results"]
    assert group["alternatives"] == []


def test_packaging_entry_with_no_reference_url_is_excluded_even_if_verified(tmp_path):
    path = write_curated(
        tmp_path,
        [
            curated_entry(
                name="Verified But No Link",
                ingredients=("Water", "Glycerin"),
                source_type="packaging",
                source_url=None,
                verified=True,
                reference_url=None,
            )
        ],
    )
    curated = alternatives.load_products(path=path)

    target = skincare.Product("Night Serum", ("Retinol",))
    result = alternatives.check_alternatives_for_product(
        target, FLAGGING_COMPANION, curated=curated
    )

    (group,) = result["results"]
    assert group["alternatives"] == []


def test_verified_packaging_entry_with_reference_url_is_a_candidate(tmp_path):
    path = write_curated(
        tmp_path,
        [
            curated_entry(
                name="Verified Packaging",
                ingredients=("Water", "Glycerin"),
                source_type="packaging",
                source_url=None,
                verified=True,
                reference_url="https://retailer.example.com/product",
            )
        ],
    )
    curated = alternatives.load_products(path=path)

    target = skincare.Product("Night Serum", ("Retinol",))
    result = alternatives.check_alternatives_for_product(
        target, FLAGGING_COMPANION, curated=curated
    )

    (group,) = result["results"]
    names = {a["name"] for a in group["alternatives"]}
    assert names == {"Verified Packaging"}


def test_entry_with_no_source_type_behaves_as_before(tmp_path):
    # Legacy shape: no source_type key at all, source_url present, verified
    # false -- must still be an eligible candidate, same as pre-source_type
    # behavior.
    path = write_curated(
        tmp_path,
        [curated_entry(name="Legacy Product", ingredients=("Water", "Glycerin"))],
    )
    curated = alternatives.load_products(path=path)

    target = skincare.Product("Night Serum", ("Retinol",))
    result = alternatives.check_alternatives_for_product(
        target, FLAGGING_COMPANION, curated=curated
    )

    (group,) = result["results"]
    names = {a["name"] for a in group["alternatives"]}
    assert names == {"Legacy Product"}


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


def test_candidate_with_an_internal_incompatible_pair_is_excluded(tmp_path):
    # Neither retinol nor glycolic acid is the flagged ingredient here -- the
    # candidate must still be excluded, because the re-check (via
    # _introduces_new_flags -> skincare.check_ingredients) checks every pair
    # WITHIN the candidate itself, not just against the user's other
    # products.
    path = write_curated(
        tmp_path,
        [
            curated_entry(
                name="Has Its Own Bad Pair",
                ingredients=("Retinol", "Glycolic Acid", "Water"),
            ),
            curated_entry(name="Clean One", ingredients=("Water", "Glycerin")),
        ],
    )
    curated = alternatives.load_products(path=path)

    target = skincare.Product("Vitamin C Serum", ("Ascorbic Acid",))
    other = [skincare.Product("Niacinamide Cream", ("Niacinamide",))]
    result = alternatives.check_alternatives_for_product(target, other, curated=curated)

    (group,) = result["results"]
    names = {a["name"] for a in group["alternatives"]}
    assert names == {"Clean One"}


# --- Same-product pair: candidate note and banner flag --------------------------


def _eva_style_target() -> skincare.Product:
    # Retinol + Salicylic Acid is a cited same-product-capable incompatibility
    # (American Academy of Dermatology rule); both ingredients live in one
    # product here, same as the real Eva Naturals Vitamin C+ Serum entry.
    return skincare.Product("Eva Serum", ("Retinol", "Salicylic Acid"))


def _group_for(result: dict, flagged_ingredient: str) -> dict:
    return next(
        g for g in result["results"] if g["flagged_ingredient"] == flagged_ingredient
    )


def test_candidate_with_only_the_partner_ingredient_shows_contains_line(tmp_path):
    path = write_curated(
        tmp_path,
        [
            curated_entry(
                name="Has Salicylic Only", ingredients=("Salicylic Acid", "Water")
            )
        ],
    )
    curated = alternatives.load_products(path=path)

    result = alternatives.check_alternatives_for_product(
        _eva_style_target(), [], curated=curated
    )

    retinol_group = _group_for(result, "Retinol")
    assert retinol_group["same_product_partners"] == ["Salicylic Acid"]
    (candidate,) = retinol_group["alternatives"]
    assert candidate["same_product_note"] == (
        "Does not contain: Retinol. Contains: Salicylic Acid"
    )


def test_candidate_with_neither_ingredient_shows_contains_neither(tmp_path):
    path = write_curated(
        tmp_path, [curated_entry(name="Has Neither", ingredients=("Water", "Glycerin"))]
    )
    curated = alternatives.load_products(path=path)

    result = alternatives.check_alternatives_for_product(
        _eva_style_target(), [], curated=curated
    )

    retinol_group = _group_for(result, "Retinol")
    (candidate,) = retinol_group["alternatives"]
    assert candidate["same_product_note"] == (
        "Contains neither ingredient of the flagged pair"
    )


def test_candidate_with_both_pair_ingredients_is_excluded_from_both_groups(tmp_path):
    path = write_curated(
        tmp_path,
        [curated_entry(name="Has Both", ingredients=("Retinol", "Salicylic Acid"))],
    )
    curated = alternatives.load_products(path=path)

    result = alternatives.check_alternatives_for_product(
        _eva_style_target(), [], curated=curated
    )

    retinol_group = _group_for(result, "Retinol")
    salicylic_group = _group_for(result, "Salicylic Acid")
    assert retinol_group["alternatives"] == []
    assert salicylic_group["alternatives"] == []


def test_two_product_finding_has_no_same_product_partners(tmp_path):
    path = write_curated(tmp_path, [curated_entry(name="Clean One")])
    curated = alternatives.load_products(path=path)

    target = skincare.Product("Night Serum", ("Retinol",))
    result = alternatives.check_alternatives_for_product(
        target, FLAGGING_COMPANION, curated=curated
    )

    (group,) = result["results"]
    assert group["same_product_partners"] == []
    (candidate,) = group["alternatives"]
    assert candidate["same_product_note"] is None


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


def test_cosmetic_ingredients_sharing_a_registry_name_are_not_medications():
    registry = load_registry()
    bad = alternatives.medication_ingredient_names(
        ["Alcohol", "Ethyl Alcohol", "Caffeine", "Ibuprofen"], registry
    )
    assert bad == ["Ibuprofen"]


def test_payload_the_client_sends_after_skincare_check_is_accepted(client):
    register_and_log(
        client, "Vitamin C Serum", ["Water", "Alcohol", "Retinol", "Salicylic Acid"]
    )
    register_and_log(client, "Toner", ["Aqua", "Caffeine", "Niacinamide"])

    check = client.get("/users/default/skincare-check")
    assert check.status_code == 200, check.text
    r = client.post(
        "/skincare-alternatives", json={"products": check.json()["products"]}
    )

    assert r.status_code == 200, r.text
    assert r.json()["results"]


def test_get_alternatives_for_a_product_listing_alcohol_is_accepted(client):
    product_id = register_and_log(
        client, "Vitamin C Serum", ["Water", "Alcohol", "Retinol", "Salicylic Acid"]
    )

    r = client.get(
        "/users/default/skincare-alternatives", params={"product_id": product_id}
    )

    assert r.status_code == 200, r.text


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
