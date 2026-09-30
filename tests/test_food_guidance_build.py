"""Tests for scripts/build_food_guidance_curated.py. openFDA is mocked via
monkeypatching app.api.food_guidance's fetch function directly -- no live
fetches in the suite.
"""

from __future__ import annotations

import json

from app.api import food_guidance
from scripts import build_food_guidance_curated as builder


def oral_label(**fields) -> dict:
    label = {"openfda": {"route": ["ORAL"]}, "set_id": "abc-123"}
    label.update(fields)
    return label


def write_data(tmp_path, entries):
    path = tmp_path / "food_drug_guidance.json"
    path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    return path


def test_build_script_creates_no_entry_when_no_quote_found(tmp_path, monkeypatch):
    label = oral_label(warnings=["This product contains inactive ingredients."])
    monkeypatch.setattr(food_guidance, "fetch_label_for_substance", lambda name: label)
    path = write_data(tmp_path, [])

    exit_code = builder.main(["--data", str(path)])

    assert exit_code == 0
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["entries"] == []


def test_build_script_adds_entry_when_quote_qualifies(tmp_path, monkeypatch):
    label = oral_label(
        dosage_and_administration=["Take with food or milk if stomach upset occurs."]
    )
    monkeypatch.setattr(food_guidance, "fetch_label_for_substance", lambda name: label)
    path = write_data(tmp_path, [])

    exit_code = builder.main(["--data", str(path)])

    assert exit_code == 0
    data = json.loads(path.read_text(encoding="utf-8"))
    food_entries = [e for e in data["entries"] if e["item"] == "food"]
    assert food_entries
    assert food_entries[0]["verified"] is False
    assert "food" in food_entries[0]["quote"].lower()


def test_build_script_never_overwrites_existing_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "PAIRS", [("ibuprofen", "food")])
    label = oral_label(
        dosage_and_administration=["Take with food, completely different text now."]
    )
    monkeypatch.setattr(food_guidance, "fetch_label_for_substance", lambda name: label)
    original = {
        "substance": "ibuprofen",
        "item": "food",
        "quote": "Original quote, never touched.",
        "label_field": "directions",
        "set_id": "old-id",
        "date_fetched": "2026-01-01",
        "verified": False,
    }
    path = write_data(tmp_path, [original])

    builder.main(["--data", str(path)])

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["entries"] == [original]


def test_build_script_reports_unreachable_openfda(tmp_path, monkeypatch):
    def _boom(name):
        raise food_guidance.OpenFDAUnavailable("down")

    monkeypatch.setattr(food_guidance, "fetch_label_for_substance", _boom)
    path = write_data(tmp_path, [])

    exit_code = builder.main(["--data", str(path)])

    assert exit_code == 0
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["entries"] == []


def test_build_script_skips_unrecognized_substance(tmp_path, monkeypatch):
    monkeypatch.setattr(
        builder,
        "PAIRS",
        [("not-a-real-substance", "food")],
    )
    path = write_data(tmp_path, [])

    exit_code = builder.main(["--data", str(path)])

    assert exit_code == 0
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["entries"] == []


def test_build_script_entry_includes_product_name_and_dosage_form(
    tmp_path, monkeypatch
):
    label = oral_label(
        openfda={"route": ["ORAL"], "brand_name": ["Ibuprofen"]},
        dosage_forms_and_strengths=["Extended-Release Tablets: 800 mg."],
        dosage_and_administration=["Take with food or milk if stomach upset occurs."],
    )
    monkeypatch.setattr(food_guidance, "fetch_label_for_substance", lambda name: label)
    path = write_data(tmp_path, [])

    builder.main(["--data", str(path)])

    data = json.loads(path.read_text(encoding="utf-8"))
    entry = next(e for e in data["entries"] if e["item"] == "food")
    assert entry["product_name"] == "Ibuprofen"
    assert entry["dosage_form"] == "extended-release tablet"


# --- --audit mode ------------------------------------------------------------


def test_audit_removes_entry_whose_quote_no_longer_passes(tmp_path, monkeypatch):
    failing_label = oral_label(
        dosage_and_administration=[
            "2 DOSAGE AND ADMINISTRATION Take with food once daily."
        ]
    )
    monkeypatch.setattr(
        food_guidance, "fetch_label_by_set_id", lambda set_id: failing_label
    )
    entry = {
        "substance": "metformin",
        "item": "food",
        "quote": "2 DOSAGE AND ADMINISTRATION Take with food once daily.",
        "label_field": "dosage_and_administration",
        "set_id": "old-id",
        "date_fetched": "2026-01-01",
        "verified": False,
    }
    path = write_data(tmp_path, [entry])

    exit_code = builder.main(["--data", str(path), "--audit"])

    assert exit_code == 0
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["entries"] == []


def test_audit_keeps_passing_entry_and_backfills_product_and_form(
    tmp_path, monkeypatch
):
    passing_label = oral_label(
        openfda={"route": ["ORAL"], "brand_name": ["Ibuprofen"]},
        dosage_forms_and_strengths=["Extended-Release Tablets: 800 mg."],
        when_using=["Take with food or milk if stomach upset occurs."],
    )
    monkeypatch.setattr(
        food_guidance, "fetch_label_by_set_id", lambda set_id: passing_label
    )
    entry = {
        "substance": "ibuprofen",
        "item": "food",
        "quote": "Take with food or milk if stomach upset occurs.",
        "label_field": "when_using",
        "set_id": "real-id",
        "date_fetched": "2026-01-01",
        "verified": False,
    }
    path = write_data(tmp_path, [entry])

    builder.main(["--data", str(path), "--audit"])

    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["entries"]) == 1
    kept = data["entries"][0]
    assert kept["quote"] == entry["quote"]
    assert kept["product_name"] == "Ibuprofen"
    assert kept["dosage_form"] == "extended-release tablet"


def test_audit_removes_entry_with_no_set_id(tmp_path):
    entry = {
        "substance": "ibuprofen",
        "item": "food",
        "quote": "Take with food or milk if stomach upset occurs.",
        "label_field": "when_using",
        "set_id": None,
        "date_fetched": "2026-01-01",
        "verified": False,
    }
    path = write_data(tmp_path, [entry])

    builder.main(["--data", str(path), "--audit"])

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["entries"] == []


def test_audit_keeps_entry_unvalidated_when_openfda_unreachable(tmp_path, monkeypatch):
    def _boom(set_id):
        raise food_guidance.OpenFDAUnavailable("down")

    monkeypatch.setattr(food_guidance, "fetch_label_by_set_id", _boom)
    entry = {
        "substance": "ibuprofen",
        "item": "food",
        "quote": "Take with food or milk if stomach upset occurs.",
        "label_field": "when_using",
        "set_id": "real-id",
        "date_fetched": "2026-01-01",
        "verified": False,
    }
    path = write_data(tmp_path, [entry])

    builder.main(["--data", str(path), "--audit"])

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["entries"] == [entry]
