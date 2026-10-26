import secrets
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import demo_seed, food_rules, interactions, verification
from app.api.db import Base
from app.registry.loader import load_registry

NOW = datetime(2026, 9, 13, 18, 0, tzinfo=UTC)


@pytest.fixture
def seeded():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    registry = load_registry()
    with sessionmaker(bind=engine)() as session:
        result = demo_seed.seed(
            session,
            registry,
            "demo@example.com",
            secrets.token_urlsafe(16),
            NOW,
            food_scenario=True,
        )
        yield session, registry, result.user_id


def check(seeded):
    session, registry, user_id = seeded
    interactions.clear_cache()
    return interactions.check_regimen(session, user_id, registry, NOW)


def food(result):
    return [f for f in result["interactions"] if f.get("finding_kind") == "food"]


def test_with_the_gate_on_the_demo_scenario_shows_exactly_one_food_finding(
    seeded, monkeypatch
):
    monkeypatch.setenv("SHOW_UNVERIFIED_RULES", "1")
    result = check(seeded)
    findings = food(result)
    assert len(findings) == 1
    assert findings[0]["substance_a"]["id"] == "acetaminophen"
    assert findings[0]["substance_b"]["display_name"] == "Alcohol"
    assert findings[0]["verified"] is False
    assert findings[0]["quoted_text"].endswith(
        "3 or more alcoholic drinks every day while using this product"
    )
    assert findings[0]["applies_to"].startswith("The label's alcohol warning refers to")
    assert "rules_awaiting_verification" not in result
    assert [d["ingredient_name"] for d in result["duplicate_ingredients"]] == [
        "Acetaminophen"
    ]


def test_coffee_and_chicken_add_nothing_in_the_demo_scenario(seeded, monkeypatch):
    monkeypatch.setenv("SHOW_UNVERIFIED_RULES", "1")
    result = check(seeded)
    names = {s["label"] for f in food(result) for s in f["substance_b"]["sources"]}
    assert names == {"Example Beer (12 oz)"}


def test_with_the_gate_off_the_food_finding_is_hidden_and_counted(seeded, monkeypatch):
    monkeypatch.setenv("SHOW_UNVERIFIED_RULES", "0")
    result = check(seeded)
    assert food(result) == []
    expected = interactions.unverified_rule_count() + food_rules.unverified_count()
    notice = result["rules_awaiting_verification"]
    assert notice["count"] == expected
    assert notice["message"] == verification.awaiting_message(expected)
    assert food_rules.unverified_count() == len(food_rules.load_rules())


def test_the_duplicate_ingredient_card_does_not_depend_on_the_gate(seeded, monkeypatch):
    monkeypatch.setenv("SHOW_UNVERIFIED_RULES", "0")
    result = check(seeded)
    assert [d["ingredient_name"] for d in result["duplicate_ingredients"]] == [
        "Acetaminophen"
    ]


def test_the_dev_run_scripts_turn_the_gate_on_by_default():
    root = __import__("pathlib").Path(__file__).resolve().parent.parent
    assert 'SHOW_UNVERIFIED_RULES="${SHOW_UNVERIFIED_RULES:-1}"' in (
        root / "run.sh"
    ).read_text(encoding="utf-8")
    assert '$env:SHOW_UNVERIFIED_RULES = "1"' in (root / "run.ps1").read_text(
        encoding="utf-8"
    )
