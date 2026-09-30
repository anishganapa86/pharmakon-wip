"""Tests for scripts/_skincare_common.py extraction and
scripts/import_skincare_product.py. All HTTP is mocked -- no live fetches.
"""

from __future__ import annotations

import json

from scripts import import_skincare_product as importer
from scripts._skincare_common import (
    FetchResult,
    HostRateLimiter,
    extract_ingredients,
)

# --- Extraction ------------------------------------------------------------


def test_extraction_pulls_verbatim_ingredient_text():
    html = """
    <html><body>
    <h3>Ingredients</h3>
    <p>Water, Glycerin, Niacinamide, Butylene Glycol, Phenoxyethanol, Fragrance</p>
    </body></html>
    """
    result = extract_ingredients(html)

    assert result.reason is None
    assert result.ingredients == [
        "Water",
        "Glycerin",
        "Niacinamide",
        "Butylene Glycol",
        "Phenoxyethanol",
        "Fragrance",
    ]
    assert "Water, Glycerin" in result.raw_text


def test_extraction_same_line_label_and_list():
    html = "<p>Full Ingredients: Aqua, Glycerin, Niacinamide, Tocopherol, Silica</p>"
    result = extract_ingredients(html)

    assert result.ingredients == [
        "Aqua",
        "Glycerin",
        "Niacinamide",
        "Tocopherol",
        "Silica",
    ]


def test_page_with_no_ingredient_section_is_skipped_with_reason():
    html = "<html><body><h1>Our Story</h1><p>We make great skincare.</p></body></html>"
    result = extract_ingredients(html)

    assert result.raw_text is None
    assert result.ingredients == []
    assert "no ingredient section found" in result.reason


def test_page_with_two_differing_ingredient_sections_is_ambiguous():
    html = """
    <html><body>
    <h3>Ingredients</h3>
    <p>Water, Glycerin, Niacinamide, Tocopherol, Silica</p>
    <h3>Ingredients</h3>
    <p>Aqua, Dimethicone, Phenoxyethanol, Fragrance, Citric Acid</p>
    </body></html>
    """
    result = extract_ingredients(html)

    assert result.raw_text is None
    assert "ambiguous" in result.reason
    assert "2" in result.reason


def test_identical_duplicate_ingredient_sections_are_not_ambiguous():
    # Two views of the *same* list (e.g. a desktop and mobile copy) should
    # not be flagged as ambiguous -- ambiguity means differing content.
    ingredients_html = (
        "<h3>Ingredients</h3><p>Water, Glycerin, Niacinamide, Tocopherol, Silica</p>"
    )
    html = f"""
    <html><body>
    <div class="desktop">{ingredients_html}</div>
    <div class="mobile">{ingredients_html}</div>
    </body></html>
    """
    result = extract_ingredients(html)

    assert result.reason is None
    assert result.ingredients == [
        "Water",
        "Glycerin",
        "Niacinamide",
        "Tocopherol",
        "Silica",
    ]


def test_partial_capture_is_skipped():
    html = "<html><body><h3>Ingredients</h3><p>Water, Glycerin</p></body></html>"
    result = extract_ingredients(html)

    assert result.raw_text is None
    assert "partial capture" in result.reason
    assert "2" in result.reason


# --- Importer: batch/fetch mode ---------------------------------------------


def make_data(products=None):
    return {"products": products or [], "_README": "test fixture"}


def fake_fetch(mapping):
    def _fetch(url, limiter, user_agent=None):
        return mapping[url]

    return _fetch


def test_new_product_is_added_with_expected_fields(monkeypatch):
    html = "<h3>Ingredients</h3><p>Water, Glycerin, Niacinamide, Tocopherol, Silica</p>"
    monkeypatch.setattr(
        importer,
        "fetch_page",
        fake_fetch({"https://example.com/p": FetchResult(html, None)}),
    )
    data = make_data()

    outcome = importer.import_one(
        {
            "brand": "CleanBrand",
            "name": "Gentle Serum",
            "category": "serum",
            "url": "https://example.com/p",
        },
        data,
        HostRateLimiter(),
    )

    assert outcome.status == "added"
    (entry,) = data["products"]
    assert entry["brand"] == "CleanBrand"
    assert entry["ingredients"] == [
        "Water",
        "Glycerin",
        "Niacinamide",
        "Tocopherol",
        "Silica",
    ]
    assert entry["entry_method"] == "fetched"
    assert entry["verified"] is False
    assert entry["source_url"] == "https://example.com/p"
    assert entry["page_text_hash"]
    assert "date_checked" in entry


def test_importer_never_sets_verified_true(monkeypatch):
    html = "<h3>Ingredients</h3><p>Water, Glycerin, Niacinamide, Tocopherol, Silica</p>"
    monkeypatch.setattr(
        importer,
        "fetch_page",
        fake_fetch({"https://example.com/p": FetchResult(html, None)}),
    )
    data = make_data()

    importer.import_one(
        {
            "brand": "B",
            "name": "N",
            "category": "moisturizer",
            "url": "https://example.com/p",
        },
        data,
        HostRateLimiter(),
    )

    assert all(e["verified"] is False for e in data["products"])


def test_unfetchable_page_is_skipped(monkeypatch):
    monkeypatch.setattr(
        importer,
        "fetch_page",
        fake_fetch({"https://example.com/p": FetchResult(None, "HTTP 404")}),
    )
    data = make_data()

    outcome = importer.import_one(
        {
            "brand": "B",
            "name": "N",
            "category": "cleanser",
            "url": "https://example.com/p",
        },
        data,
        HostRateLimiter(),
    )

    assert outcome.status == "skipped"
    assert "unfetchable" in outcome.reason
    assert data["products"] == []


def test_unrecognized_category_is_rejected_without_fetching(monkeypatch):
    def _boom(url, limiter, user_agent=None):
        raise AssertionError("should not fetch when category is unrecognized")

    monkeypatch.setattr(importer, "fetch_page", _boom)
    data = make_data()

    outcome = importer.import_one(
        {
            "brand": "B",
            "name": "N",
            "category": "toner",
            "url": "https://example.com/p",
        },
        data,
        HostRateLimiter(),
    )

    assert outcome.status == "skipped"
    assert "unrecognized category" in outcome.reason
    assert data["products"] == []


def test_existing_entry_is_never_overwritten(monkeypatch):
    html = (
        "<h3>Ingredients</h3>"
        "<p>Aqua, Dimethicone, Phenoxyethanol, Fragrance, Citric Acid</p>"
    )
    monkeypatch.setattr(
        importer,
        "fetch_page",
        fake_fetch({"https://example.com/p": FetchResult(html, None)}),
    )
    original_entry = {
        "brand": "CleanBrand",
        "name": "Gentle Serum",
        "category": "serum",
        "ingredients": ["Water", "Glycerin"],
        "source_url": "https://example.com/p",
        "date_checked": "2026-01-01",
        "verified": False,
    }
    data = make_data([dict(original_entry)])

    outcome = importer.import_one(
        {
            "brand": "CleanBrand",
            "name": "Gentle Serum",
            "category": "serum",
            "url": "https://example.com/p",
        },
        data,
        HostRateLimiter(),
    )

    assert outcome.status == "exists"
    assert "DIFFERS" in outcome.reason
    assert data["products"] == [original_entry]  # untouched


def test_existing_entry_matching_fetch_is_reported_as_match(monkeypatch):
    html = "<h3>Ingredients</h3><p>Water, Glycerin</p>"
    # 2-item list would normally be a partial capture; use a real one instead.
    html = "<h3>Ingredients</h3><p>Water, Glycerin, Niacinamide, Tocopherol, Silica</p>"
    monkeypatch.setattr(
        importer,
        "fetch_page",
        fake_fetch({"https://example.com/p": FetchResult(html, None)}),
    )
    original_entry = {
        "brand": "CleanBrand",
        "name": "Gentle Serum",
        "category": "serum",
        "ingredients": ["Water", "Glycerin", "Niacinamide", "Tocopherol", "Silica"],
        "source_url": "https://example.com/p",
        "date_checked": "2026-01-01",
        "verified": False,
    }
    data = make_data([dict(original_entry)])

    outcome = importer.import_one(
        {
            "brand": "CleanBrand",
            "name": "Gentle Serum",
            "category": "serum",
            "url": "https://example.com/p",
        },
        data,
        HostRateLimiter(),
    )

    assert outcome.status == "exists"
    assert "matches stored entry" in outcome.reason
    assert data["products"] == [original_entry]


# --- Importer: manual mode ---------------------------------------------------


def test_manual_mode_stores_entry_method_manual_and_verified_false(tmp_path):
    text_path = tmp_path / "list.txt"
    text_path.write_text(
        "Water, Glycerin, Niacinamide, Tocopherol, Silica", encoding="utf-8"
    )
    data = make_data()

    outcome = importer.import_from_text(
        text_path, "BrandM", "ProductM", "moisturizer", "https://example.com/m", data
    )

    assert outcome.status == "added"
    (entry,) = data["products"]
    assert entry["entry_method"] == "manual"
    assert entry["verified"] is False
    assert entry["ingredients"] == [
        "Water",
        "Glycerin",
        "Niacinamide",
        "Tocopherol",
        "Silica",
    ]


def test_manual_mode_normalizes_crlf_and_strips_cr(tmp_path):
    text_path = tmp_path / "list.txt"
    raw = "Water,\r\nGlycerin,\r\nNiacinamide,\r\nTocopherol,\r\nSilica\r\n"
    text_path.write_bytes(raw.encode("utf-8"))
    data = make_data()

    outcome = importer.import_from_text(
        text_path, "BrandM", "ProductM", "serum", "https://example.com/m", data
    )

    assert outcome.status == "added"
    (entry,) = data["products"]
    assert "\r" not in entry["raw_ingredient_text"]
    assert all("\r" not in i for i in entry["ingredients"])
    assert entry["ingredients"] == [
        "Water",
        "Glycerin",
        "Niacinamide",
        "Tocopherol",
        "Silica",
    ]


def test_manual_mode_partial_capture_is_skipped(tmp_path):
    text_path = tmp_path / "list.txt"
    text_path.write_text("Water, Glycerin", encoding="utf-8")
    data = make_data()

    outcome = importer.import_from_text(
        text_path, "BrandM", "ProductM", "serum", "https://example.com/m", data
    )

    assert outcome.status == "skipped"
    assert "partial capture" in outcome.reason
    assert data["products"] == []


def test_manual_mode_unrecognized_category_is_rejected(tmp_path):
    text_path = tmp_path / "list.txt"
    text_path.write_text(
        "Water, Glycerin, Niacinamide, Tocopherol, Silica", encoding="utf-8"
    )
    data = make_data()

    outcome = importer.import_from_text(
        text_path, "BrandM", "ProductM", "toner", "https://example.com/m", data
    )

    assert outcome.status == "skipped"
    assert "unrecognized category" in outcome.reason


def test_manual_mode_never_overwrites_existing_entry(tmp_path):
    text_path = tmp_path / "list.txt"
    text_path.write_text(
        "Water, Glycerin, Niacinamide, Tocopherol, Silica", encoding="utf-8"
    )
    original_entry = {
        "brand": "BrandM",
        "name": "ProductM",
        "category": "serum",
        "ingredients": ["Something", "Else"],
        "source_url": "https://old.example.com",
        "date_checked": "2026-01-01",
        "verified": False,
    }
    data = make_data([dict(original_entry)])

    outcome = importer.import_from_text(
        text_path, "BrandM", "ProductM", "serum", "https://example.com/m", data
    )

    assert outcome.status == "exists"
    assert data["products"] == [original_entry]


# --- save_data round trip ----------------------------------------------------


def test_save_data_writes_utf8_lf_json(tmp_path):
    path = tmp_path / "skincare_products.json"
    path.write_text(json.dumps({"products": []}), encoding="utf-8")
    data = {"products": [{"brand": "B", "name": "N"}]}

    importer.save_data(path, data)

    raw = path.read_bytes()
    assert b"\r" not in raw
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    assert reloaded == data
