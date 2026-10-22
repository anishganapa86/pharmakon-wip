from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import food_guidance, interactions, main, skincare, verification
from app.api.db import Base

REPO = Path(__file__).resolve().parent.parent
NOW = datetime.fromisoformat("2026-09-13T18:00:00-07:00")
GATE = "SHOW_UNVERIFIED_RULES"

VERIFIED_FIELDS = {
    "verified": True,
    "verified_by": "A. Reviewer",
    "verified_on": "2026-10-01",
    "source_url": "https://example.org/source",
    "source_quote": "Quoted from the opened source.",
}
REQUIRED = ("verified_by", "verified_on", "source_url", "source_quote")


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


@pytest.fixture
def gate_off(monkeypatch):
    monkeypatch.delenv(GATE, raising=False)


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv(GATE, "1")


@pytest.fixture
def label_text(monkeypatch):
    texts: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        search = request.url.params["search"]
        for name, text in texts.items():
            if f'"{name}"' in search:
                return httpx.Response(
                    200, json={"results": [{"drug_interactions": [text]}]}
                )
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})

    monkeypatch.setattr(
        interactions, "_http", httpx.Client(transport=httpx.MockTransport(handler))
    )
    interactions.clear_cache()
    yield texts
    interactions.clear_cache()


def awaiting(count: int) -> dict:
    word = "rule is" if count == 1 else "rules are"
    verb = "is" if count == 1 else "are"
    message = f"{count} {word} awaiting verification and {verb} not being applied."
    return {"count": count, "message": message}


def interaction_check(client, names):
    r = client.post("/interaction-check", json={"names": names})
    assert r.status_code == 200, r.text
    return r.json()


def skincare_check(client, products):
    r = client.post("/skincare-check", json={"products": products})
    assert r.status_code == 200, r.text
    return r.json()


def food_check(client, names):
    r = client.post("/food-guidance", json={"names": names})
    assert r.status_code == 200, r.text
    return r.json()


def oral_label(**fields) -> dict:
    label = {"openfda": {"route": ["ORAL"]}}
    label.update(fields)
    return label


RETINOL_SALICYLIC = [
    {"name": "Retinol serum", "ingredients": ["Retinol"]},
    {"name": "BHA toner", "ingredients": ["Salicylic Acid"]},
]


def test_the_gate_defaults_to_off(gate_off):
    assert verification.show_unverified_rules() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " 1 "])
def test_truthy_values_turn_the_gate_on(monkeypatch, value):
    monkeypatch.setenv(GATE, value)
    assert verification.show_unverified_rules() is True


@pytest.mark.parametrize("value", ["0", "false", "", "no", "off", "maybe"])
def test_anything_else_leaves_the_gate_off(monkeypatch, value):
    monkeypatch.setenv(GATE, value)
    assert verification.show_unverified_rules() is False


def test_notice_wording_for_one_and_many(gate_off):
    assert verification.awaiting_message(1) == (
        "1 rule is awaiting verification and is not being applied."
    )
    assert verification.awaiting_message(8) == (
        "8 rules are awaiting verification and are not being applied."
    )
    assert verification.unapplied_notice(0) is None


def test_no_notice_when_the_gate_is_on(gate_on):
    assert verification.unapplied_notice(8) is None


def test_every_shipped_curated_entry_is_still_unverified():
    assert all(not r.verified for r in interactions.load_rules())
    rules = skincare.load_rules()
    assert all(not r.verified for r in rules.incompatibilities)
    assert all(not r.verified for r in rules.known_irritants)
    assert all(not c.verified for c in food_guidance.load_curated())


def test_ingredient_interactions_entries_all_carry_the_verification_fields():
    data = json.loads(
        (REPO / "app" / "data" / "ingredient_interactions.json").read_text("utf-8")
    )
    entries = data["incompatibilities"] + data["known_irritants"]
    assert len(entries) == 8
    for entry in entries:
        assert entry["verified"] is False
        for field in REQUIRED:
            assert field in entry
            assert entry[field] is None


# --- Interactions ---------------------------------------------------------------


def test_gate_off_applies_no_unverified_interaction_rule_and_counts_them(
    client, gate_off, label_text
):
    body = interaction_check(client, ["ibuprofen", "naproxen"])

    assert [f for f in body["interactions"] if f["source_type"] == "curated"] == []
    assert body["rules_awaiting_verification"] == awaiting(
        len(interactions.load_rules())
    )
    assert body["rules_awaiting_verification"]["count"] > 0


def test_gate_on_applies_unverified_interaction_rules_without_a_notice(
    client, gate_on, label_text
):
    body = interaction_check(client, ["ibuprofen", "naproxen"])

    (curated,) = [f for f in body["interactions"] if f["source_type"] == "curated"]
    assert curated["verified"] is False
    assert "rules_awaiting_verification" not in body


def test_gate_off_leaves_live_label_findings_alone(client, gate_off, label_text):
    label_text["IBUPROFEN"] = "Do not use naproxen together with ibuprofen."

    body = interaction_check(client, ["ibuprofen", "naproxen"])

    sources = {f["source_type"] for f in body["interactions"]}
    assert sources == {"fda_label"}
    assert "rules_awaiting_verification" in body


def test_a_verified_interaction_rule_applies_with_the_gate_off(
    client, gate_off, label_text, monkeypatch, tmp_path
):
    path = tmp_path / "rules.json"
    path.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "substance_a": "NSAID",
                        "substance_b": "NSAID",
                        "effect": "e",
                        "recommendation": "r",
                        "source": "s",
                        **VERIFIED_FIELDS,
                    },
                    {
                        "substance_a": "sertraline",
                        "substance_b": "NSAID",
                        "effect": "e2",
                        "recommendation": "r2",
                        "source": "s2",
                        "verified": False,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(interactions, "DATA_PATH", path)
    interactions.load_rules.cache_clear()

    both = interaction_check(client, ["ibuprofen", "naproxen"])
    unverified_only = interaction_check(client, ["sertraline", "ibuprofen"])
    interactions.load_rules.cache_clear()

    (finding,) = both["interactions"]
    assert finding["verified"] is True
    assert both["rules_awaiting_verification"] == awaiting(1)
    assert unverified_only["interactions"] == []


# --- Skincare ---------------------------------------------------------------------


def test_gate_off_applies_no_unverified_skincare_rule_and_counts_them(client, gate_off):
    body = skincare_check(client, RETINOL_SALICYLIC)

    assert body["incompatibilities"] == []
    assert body["irritants"] == []
    assert body["rules_checked"] == 0
    total = len(skincare.load_rules().incompatibilities) + len(
        skincare.load_rules().known_irritants
    )
    assert body["rules_awaiting_verification"] == awaiting(total)
    assert total == 8


def test_gate_on_applies_unverified_skincare_rules_without_a_notice(client, gate_on):
    body = skincare_check(client, RETINOL_SALICYLIC)

    assert len(body["incompatibilities"]) == 1
    assert body["rules_checked"] == 8
    assert "rules_awaiting_verification" not in body


def test_gate_off_notice_also_reaches_the_regimen_check(client, gate_off):
    r = client.get("/users/default/skincare-check")

    assert r.status_code == 200
    assert r.json()["rules_awaiting_verification"] == awaiting(8)


def test_a_verified_skincare_rule_applies_with_the_gate_off(gate_off, tmp_path):
    path = tmp_path / "ingredients.json"
    path.write_text(
        json.dumps(
            {
                "incompatibilities": [
                    {
                        "ingredient_a": "retinol",
                        "aliases_a": [],
                        "ingredient_b": "salicylic acid",
                        "aliases_b": [],
                        "effect": "e",
                        "recommendation": "r",
                        "source": "s",
                        **VERIFIED_FIELDS,
                    }
                ],
                "known_irritants": [
                    {
                        "ingredient": "fragrance",
                        "aliases": [],
                        "effect": "e",
                        "source": "s",
                        "verified": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    rules = skincare.load_rules(path=path)
    products = [
        skincare.Product("A", ("Retinol", "Fragrance")),
        skincare.Product("B", ("Salicylic Acid",)),
    ]

    result = skincare.check_ingredients(products, rules)

    assert len(result["incompatibilities"]) == 1
    assert result["irritants"] == []
    assert result["rules_checked"] == 1
    assert result["rules_awaiting_verification"] == awaiting(1)


# --- Food guidance ------------------------------------------------------------------


def stub_labels(monkeypatch, label):
    monkeypatch.setattr(
        food_guidance, "fetch_labels_for_substance", lambda name, **kw: [label]
    )


def test_gate_off_drops_unverified_curated_food_entries_and_counts_them(
    client, gate_off, monkeypatch
):
    monkeypatch.setattr(
        food_guidance, "fetch_labels_for_substance", lambda name, **kw: []
    )

    body = food_check(client, ["ibuprofen"])

    assert [f for f in body["guidance"] if f["source_type"] == "curated"] == []
    assert body["rules_awaiting_verification"] == awaiting(
        len(food_guidance.load_curated())
    )


def test_gate_on_keeps_unverified_curated_food_entries_without_a_notice(
    client, gate_on, monkeypatch
):
    monkeypatch.setattr(
        food_guidance, "fetch_labels_for_substance", lambda name, **kw: []
    )

    body = food_check(client, ["ibuprofen"])

    curated = [f for f in body["guidance"] if f["source_type"] == "curated"]
    assert len(curated) == 1
    assert curated[0]["verified"] is False
    assert "rules_awaiting_verification" not in body


def test_gate_off_leaves_live_food_findings_alone(client, gate_off, monkeypatch):
    stub_labels(
        monkeypatch,
        oral_label(directions=["Take with food to reduce stomach upset."]),
    )

    body = food_check(client, ["ibuprofen"])

    assert body["guidance"]
    assert {f["source_type"] for f in body["guidance"]} == {"fda_label"}
    assert "rules_awaiting_verification" in body


def test_a_verified_curated_food_entry_applies_with_the_gate_off(
    client, gate_off, monkeypatch, tmp_path
):
    path = tmp_path / "food.json"
    path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "substance": "warfarin",
                        "item": "vitamin k",
                        "quote": "Maintain a consistent vitamin K intake.",
                        "label_field": "information_for_patients",
                        "date_fetched": "2026-01-01",
                        **VERIFIED_FIELDS,
                    },
                    {
                        "substance": "sertraline",
                        "item": "alcohol",
                        "quote": "Do not drink alcohol while you take sertraline.",
                        "label_field": "information_for_patients",
                        "date_fetched": "2026-01-01",
                        "verified": False,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(food_guidance, "DATA_PATH", path)
    food_guidance.clear_curated_cache()
    monkeypatch.setattr(
        food_guidance, "fetch_labels_for_substance", lambda name, **kw: []
    )

    body = food_check(client, ["warfarin", "sertraline"])
    food_guidance.clear_curated_cache()

    (finding,) = body["guidance"]
    assert finding["name"] == "Warfarin"
    assert finding["verified"] is True
    assert body["rules_awaiting_verification"] == awaiting(1)


# --- Loaders reject a "verified" entry that does not say who verified what ---------


def write_json(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def interaction_rule(**overrides):
    rule = {
        "substance_a": "NSAID",
        "substance_b": "NSAID",
        "effect": "e",
        "recommendation": "r",
        "source": "s",
        **VERIFIED_FIELDS,
    }
    rule.update(overrides)
    return {"rules": [rule]}


def incompatibility(**overrides):
    entry = {
        "ingredient_a": "retinol",
        "ingredient_b": "salicylic acid",
        "effect": "e",
        "recommendation": "r",
        "source": "s",
        **VERIFIED_FIELDS,
    }
    entry.update(overrides)
    return {"incompatibilities": [entry], "known_irritants": []}


def irritant(**overrides):
    entry = {
        "ingredient": "fragrance",
        "effect": "e",
        "source": "s",
        **VERIFIED_FIELDS,
    }
    entry.update(overrides)
    return {"incompatibilities": [], "known_irritants": [entry]}


def food_entry(**overrides):
    entry = {
        "substance": "warfarin",
        "item": "vitamin k",
        "quote": "Maintain a consistent vitamin K intake.",
        "label_field": "information_for_patients",
        "date_fetched": "2026-01-01",
        **VERIFIED_FIELDS,
    }
    entry.update(overrides)
    return {"entries": [entry]}


LOADERS = {
    "interactions": (
        interaction_rule,
        interactions.load_rules,
        interactions.InvalidRule,
    ),
    "skincare incompatibility": (
        incompatibility,
        skincare.load_rules,
        skincare.InvalidRule,
    ),
    "skincare irritant": (irritant, skincare.load_rules, skincare.InvalidRule),
    "food guidance": (
        food_entry,
        food_guidance.load_curated,
        food_guidance.InvalidCuratedEntry,
    ),
}


@pytest.mark.parametrize("kind", list(LOADERS))
@pytest.mark.parametrize("field", REQUIRED)
@pytest.mark.parametrize("value", [None, "", "   "])
def test_verified_without_a_required_field_is_rejected_at_load(
    tmp_path, kind, field, value
):
    build, load, error = LOADERS[kind]
    path = write_json(tmp_path, "data.json", build(**{field: value}))

    with pytest.raises(error, match=field):
        load(path=path)


@pytest.mark.parametrize("kind", list(LOADERS))
@pytest.mark.parametrize("field", REQUIRED)
def test_verified_with_a_required_field_removed_is_rejected_at_load(
    tmp_path, kind, field
):
    build, load, error = LOADERS[kind]
    payload = build()
    entries = next(v for v in payload.values() if v)
    del entries[0][field]
    path = write_json(tmp_path, "data.json", payload)

    with pytest.raises(error, match=field):
        load(path=path)


@pytest.mark.parametrize("kind", list(LOADERS))
def test_verified_with_every_field_present_loads_as_verified(tmp_path, kind):
    build, load, _ = LOADERS[kind]
    path = write_json(tmp_path, "data.json", build())

    loaded = load(path=path)

    if kind == "interactions" or kind == "food guidance":
        assert [r.verified for r in loaded] == [True]
    elif kind == "skincare incompatibility":
        assert [r.verified for r in loaded.incompatibilities] == [True]
    else:
        assert [r.verified for r in loaded.known_irritants] == [True]


@pytest.mark.parametrize("kind", list(LOADERS))
def test_unverified_entries_need_none_of_the_verification_fields(tmp_path, kind):
    build, load, _ = LOADERS[kind]
    path = write_json(
        tmp_path,
        "data.json",
        build(
            verified=False,
            verified_by=None,
            verified_on=None,
            source_url=None,
            source_quote=None,
        ),
    )

    load(path=path)


@pytest.mark.parametrize("kind", list(LOADERS))
def test_a_truthy_non_boolean_is_not_treated_as_verified(tmp_path, kind):
    build, load, _ = LOADERS[kind]
    payload = build(verified="yes", verified_by=None, verified_on=None, source_url=None)
    path = write_json(tmp_path, "data.json", payload)

    loaded = load(path=path)

    if kind == "interactions" or kind == "food guidance":
        assert [r.verified for r in loaded] == [False]


# --- Local development turns the gate on --------------------------------------------


def test_run_scripts_default_the_gate_on_for_local_development():
    sh = (REPO / "run.sh").read_text(encoding="utf-8")
    ps1 = (REPO / "run.ps1").read_text(encoding="utf-8")

    assert f"{GATE}=" in sh
    assert ":-1}" in sh
    assert f'$env:{GATE} = "1"' in ps1


def test_env_example_documents_the_gate_defaulting_to_off():
    lines = (REPO / ".env.example").read_text(encoding="utf-8").splitlines()

    assert f"{GATE}=0" in lines
    assert not (REPO / ".env.example").read_text(encoding="utf-8").startswith("#")
