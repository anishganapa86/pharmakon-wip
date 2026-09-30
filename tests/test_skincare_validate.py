"""Tests for scripts/validate_skincare_products.py. Pure data checks, no
network involved."""

from __future__ import annotations

from scripts import validate_skincare_products as validator


def entry(**overrides):
    base = {
        "brand": "CleanBrand",
        "name": "Gentle Serum",
        "category": "serum",
        "ingredients": ["Water", "Glycerin"],
        "source_url": "https://example.com/p",
        "date_checked": "2026-01-01",
        "verified": False,
    }
    base.update(overrides)
    return base


def test_clean_data_has_no_problems():
    problems = validator.validate({"products": [entry()]})
    assert problems == []


def test_placeholder_entries_are_not_flagged():
    placeholder = {
        "brand": "TODO",
        "name": "TODO placeholder",
        "category": "TODO",
        "ingredients": [],
        "source_url": "TODO",
        "date_checked": "TODO",
        "verified": False,
    }
    problems = validator.validate({"products": [placeholder]})
    assert problems == []


def test_duplicate_brand_and_name_is_flagged():
    problems = validator.validate({"products": [entry(), entry()]})
    assert any("duplicate" in p for p in problems)


def test_empty_ingredient_list_is_flagged():
    problems = validator.validate({"products": [entry(ingredients=[])]})
    assert any("empty ingredient list" in p for p in problems)


def test_missing_source_url_is_flagged():
    e = entry()
    del e["source_url"]
    problems = validator.validate({"products": [e]})
    assert any("missing source_url" in p for p in problems)


def test_missing_date_checked_is_flagged():
    e = entry()
    del e["date_checked"]
    problems = validator.validate({"products": [e]})
    assert any("missing date_checked" in p for p in problems)


def test_verified_true_without_date_checked_is_flagged():
    e = entry(verified=True)
    del e["date_checked"]
    problems = validator.validate({"products": [e]})
    assert any("verified true but no date_checked" in p for p in problems)


def test_unrecognized_category_is_flagged():
    problems = validator.validate({"products": [entry(category="toner")]})
    assert any("unrecognized category" in p for p in problems)


def test_trailing_period_is_flagged():
    problems = validator.validate(
        {"products": [entry(ingredients=["Water", "Glycerin."])]}
    )
    assert any("trailing period" in p for p in problems)


def test_carriage_return_is_flagged():
    problems = validator.validate(
        {"products": [entry(ingredients=["Water", "Glycerin\r"])]}
    )
    assert any(r"\r" in p for p in problems)


def test_stray_html_is_flagged():
    problems = validator.validate(
        {"products": [entry(ingredients=["Water", "<b>Glycerin</b>"])]}
    )
    assert any("stray HTML" in p for p in problems)


def test_main_returns_nonzero_on_problems(tmp_path):
    import json

    data_path = tmp_path / "skincare_products.json"
    data_path.write_text(
        json.dumps({"products": [entry(category="toner")]}), encoding="utf-8"
    )

    assert validator.main(["--data", str(data_path)]) == 1


def test_main_returns_zero_when_clean(tmp_path):
    import json

    data_path = tmp_path / "skincare_products.json"
    data_path.write_text(json.dumps({"products": [entry()]}), encoding="utf-8")

    assert validator.main(["--data", str(data_path)]) == 0
