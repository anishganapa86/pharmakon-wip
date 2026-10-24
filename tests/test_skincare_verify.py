"""Tests for scripts/verify_skincare_products.py. All HTTP is mocked -- no
live fetches.
"""

from __future__ import annotations

from scripts import verify_skincare_products as verifier
from scripts._skincare_common import FetchResult, HostRateLimiter


def fake_fetch(mapping):
    def _fetch(url, limiter, user_agent=None):
        return mapping[url]

    return _fetch


def entry(**overrides):
    base = {
        "brand": "CleanBrand",
        "name": "Gentle Serum",
        "category": "serum",
        "ingredients": ["Water", "Glycerin", "Niacinamide", "Tocopherol", "Silica"],
        "source_url": "https://example.com/p",
        "date_checked": "2026-01-01",
        "verified": False,
        "entry_method": "fetched",
    }
    base.update(overrides)
    return base


def test_identical_list_reports_match(monkeypatch):
    html = "<h3>Ingredients</h3><p>Water, Glycerin, Niacinamide, Tocopherol, Silica</p>"
    monkeypatch.setattr(
        verifier,
        "fetch_page",
        fake_fetch({"https://example.com/p": FetchResult(html, None)}),
    )

    result = verifier.verify_entry(entry(), HostRateLimiter())

    assert result.status == "MATCH"


def test_changed_list_reports_mismatch_with_diff(monkeypatch):
    html = "<h3>Ingredients</h3><p>Water, Glycerin, Retinol, Tocopherol, Silica</p>"
    monkeypatch.setattr(
        verifier,
        "fetch_page",
        fake_fetch({"https://example.com/p": FetchResult(html, None)}),
    )

    result = verifier.verify_entry(entry(), HostRateLimiter())

    assert result.status == "MISMATCH"
    assert "Retinol" in result.detail  # added
    assert "Niacinamide" in result.detail  # removed


def test_reordered_list_reports_mismatch_as_reordered(monkeypatch):
    html = "<h3>Ingredients</h3><p>Silica, Tocopherol, Niacinamide, Glycerin, Water</p>"
    monkeypatch.setattr(
        verifier,
        "fetch_page",
        fake_fetch({"https://example.com/p": FetchResult(html, None)}),
    )

    result = verifier.verify_entry(entry(), HostRateLimiter())

    assert result.status == "MISMATCH"
    assert "reordered" in result.detail


def test_fetch_error_reports_unfetchable(monkeypatch):
    monkeypatch.setattr(
        verifier,
        "fetch_page",
        fake_fetch({"https://example.com/p": FetchResult(None, "HTTP 500")}),
    )

    result = verifier.verify_entry(entry(), HostRateLimiter())

    assert result.status == "UNFETCHABLE"
    assert "HTTP 500" in result.detail


def test_manual_entry_unfetchable_is_skipped_not_unfetchable(monkeypatch):
    monkeypatch.setattr(
        verifier,
        "fetch_page",
        fake_fetch({"https://example.com/p": FetchResult(None, "requires JavaScript")}),
    )

    result = verifier.verify_entry(entry(entry_method="manual"), HostRateLimiter())

    assert result.status == "SKIPPED"


def test_manual_entry_that_is_fetchable_is_still_compared(monkeypatch):
    html = "<h3>Ingredients</h3><p>Water, Glycerin, Retinol, Tocopherol, Silica</p>"
    monkeypatch.setattr(
        verifier,
        "fetch_page",
        fake_fetch({"https://example.com/p": FetchResult(html, None)}),
    )

    result = verifier.verify_entry(entry(entry_method="manual"), HostRateLimiter())

    assert result.status == "MISMATCH"


def test_main_exits_nonzero_on_mismatch(monkeypatch, tmp_path):
    import json

    data_path = tmp_path / "skincare_products.json"
    data_path.write_text(json.dumps({"products": [entry()]}), encoding="utf-8")
    out_path = tmp_path / "reports" / "out.txt"

    html = "<h3>Ingredients</h3><p>Water, Glycerin, Retinol, Tocopherol, Silica</p>"
    monkeypatch.setattr(
        verifier,
        "fetch_page",
        fake_fetch({"https://example.com/p": FetchResult(html, None)}),
    )

    exit_code = verifier.main(["--data", str(data_path), "--out", str(out_path)])

    assert exit_code == 1
    report = out_path.read_text(encoding="utf-8")
    assert "MISMATCH" in report
    assert report.startswith(verifier.REPORT_HEADER)


def test_main_exits_zero_when_everything_matches(monkeypatch, tmp_path):
    import json

    data_path = tmp_path / "skincare_products.json"
    data_path.write_text(json.dumps({"products": [entry()]}), encoding="utf-8")
    out_path = tmp_path / "reports" / "out.txt"

    html = "<h3>Ingredients</h3><p>Water, Glycerin, Niacinamide, Tocopherol, Silica</p>"
    monkeypatch.setattr(
        verifier,
        "fetch_page",
        fake_fetch({"https://example.com/p": FetchResult(html, None)}),
    )

    exit_code = verifier.main(["--data", str(data_path), "--out", str(out_path)])

    assert exit_code == 0


def test_placeholder_entries_are_skipped_entirely(monkeypatch, tmp_path):
    import json

    placeholder = {
        "brand": "TODO",
        "name": "TODO placeholder",
        "category": "TODO",
        "ingredients": [],
        "source_url": "TODO",
        "date_checked": "TODO",
        "verified": False,
    }
    data_path = tmp_path / "skincare_products.json"
    data_path.write_text(json.dumps({"products": [placeholder]}), encoding="utf-8")
    out_path = tmp_path / "reports" / "out.txt"

    def _boom(url, limiter, user_agent=None):
        raise AssertionError("should never fetch a placeholder's source_url")

    monkeypatch.setattr(verifier, "fetch_page", _boom)

    exit_code = verifier.main(["--data", str(data_path), "--out", str(out_path)])

    assert exit_code == 0
