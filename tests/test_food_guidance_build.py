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
