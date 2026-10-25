from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from ..registry.loader import Registry
from . import label_source, products, service, verification
from .provenance import QUOTE_SOURCE_FDA_LABEL

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "food_drug_rules.json"
LOOKBACK_HOURS = 24.0
DAILYMED_PREFIX = "https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid="
BANNED = re.compile(r"\b(safe|no risk|will cause|causes|prevents|harmless)\b", re.I)


class InvalidFoodRule(ValueError):
    pass


@dataclass(frozen=True)
class FoodRule:
    id: str
    substance_id: str
    substance_name: str
    food_component: str
    food_component_name: str
    quote: str
    section: str
    label_product_name: str
    set_id: str
    source_url: str
    citation: str
    verified: bool


@lru_cache(maxsize=1)
def load_rules(path: Path | None = None) -> tuple[FoodRule, ...]:
    data = json.loads((path or DATA_PATH).read_text(encoding="utf-8"))
    rules = []
    for entry in data["rules"]:
        for field in ("quote", "citation", "set_id", "source_url", "section"):
            if not str(entry.get(field) or "").strip():
                raise InvalidFoodRule(f"rule {entry.get('id')!r} has no {field}")
        if not entry["source_url"].startswith(DAILYMED_PREFIX):
            raise InvalidFoodRule(f"rule {entry['id']!r} is not sourced to DailyMed")
        if not isinstance(entry.get("verified"), bool):
            raise InvalidFoodRule(f"rule {entry['id']!r} has no verified flag")
        if BANNED.search(entry["quote"]):
            raise InvalidFoodRule(f"rule {entry['id']!r} quotes a banned phrase")
        missing = verification.missing_verification_fields(entry)
        if missing:
            raise InvalidFoodRule(
                f"rule {entry['id']!r} is marked verified but is missing "
                f"{', '.join(missing)}"
            )
        rules.append(
            FoodRule(
                id=entry["id"],
                substance_id=entry["substance_id"],
                substance_name=entry["substance_name"],
                food_component=entry["food_component"],
                food_component_name=entry["food_component_name"],
                quote=entry["quote"],
                section=entry["section"],
                label_product_name=entry["label_product_name"],
                set_id=entry["set_id"],
                source_url=entry["source_url"],
                citation=entry["citation"],
                verified=entry["verified"],
            )
        )
    return tuple(rules)


def active_rules() -> tuple[FoodRule, ...]:
    rules = load_rules()
    if verification.show_unverified_rules():
        return rules
    return tuple(r for r in rules if r.verified)


def unverified_count() -> int:
    return sum(1 for r in load_rules() if not r.verified)


def event_components(event) -> set[str]:
    found = set()
    for ing in event.ingredients:
        key = products.INGREDIENT_COMPONENT.get(ing.ingredient_name.upper())
        if key:
            found.add(key)
    return found


def _ref(rule_id: str, display_name: str, events: list) -> dict:
    return {
        "id": rule_id,
        "display_name": display_name,
        "description": None,
        "sources": [
            {
                "kind": "dose",
                "label": ev.product_name,
                "dose_event_id": ev.id,
                "product_id": ev.product_id,
                "schedule_id": None,
                "taken_at": ev.taken_at,
            }
            for ev in events
        ],
    }


def food_findings(events: list, registry: Registry, now: datetime) -> list[dict]:
    recent = []
    for ev in events:
        taken_at = datetime.fromisoformat(ev.taken_at)
        if -LOOKBACK_HOURS <= service.hours_since(taken_at, now) <= 0.0:
            recent.append(ev)
    food_events = [ev for ev in recent if products.is_food(ev.product_id)]
    drug_events = [
        ev
        for ev in recent
        if not products.is_food(ev.product_id) and not ev.topical_ingredients
    ]
    findings = []
    for rule in active_rules():
        foods = [
            ev for ev in food_events if rule.food_component in event_components(ev)
        ]
        drugs = [
            ev
            for ev in drug_events
            if any(i.substance_id == rule.substance_id for i in ev.ingredients)
        ]
        if not foods or not drugs:
            continue
        food_names = sorted({ev.product_name for ev in foods})
        findings.append(
            {
                "substance_a": _ref(rule.substance_id, rule.substance_name, drugs),
                "substance_b": _ref(
                    rule.food_component, rule.food_component_name, foods
                ),
                "effect": None,
                "recommendation": None,
                "source_type": "fda_label",
                "source": rule.citation,
                "quoted_text": rule.quote,
                "quote_source": QUOTE_SOURCE_FDA_LABEL,
                "verified": rule.verified,
                "severity": None,
                "overlap": None,
                "message": (
                    f"An FDA label for a {rule.substance_name} product mentions "
                    f"{rule.food_component_name.lower()}. You logged "
                    f"{', '.join(food_names)}."
                ),
                "also_mentioned_in_label": None,
                "finding_kind": "food",
                "label_product_name": rule.label_product_name,
                "label_attribution": label_source.attribution(
                    rule.label_product_name, rule.substance_name
                ),
                "dailymed_url": rule.source_url,
            }
        )
    return findings
