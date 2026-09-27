"""Skincare ingredient interaction checker: a lookup against documented
dermatological guidelines, never a model.

This is deliberately NOT pharmacokinetics. There is no curve, no
concentration, and no invented risk score here -- "high risk" and "low risk"
are not in this module's vocabulary. It checks ingredient pairs and single
ingredients against app/data/ingredient_interactions.json and reports only
what that file says. Every rule in that file carries a source citation (see
its `_README`); a rule with no source doesn't belong there, and this module
adds none of its own.

Topical ingredients are out of scope for app/pk/ (CLAUDE.md: "topical-only
substances... are barrier-irritation problems, not pharmacokinetic ones"),
which is exactly why this checker exists as a separate, non-PK path.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "ingredient_interactions.json"
)


def normalize(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


@dataclass(frozen=True)
class Incompatibility:
    ingredient_a: str
    aliases_a: tuple[str, ...]
    ingredient_b: str
    aliases_b: tuple[str, ...]
    effect: str
    recommendation: str
    source: str

    def matches_a(self, name: str) -> bool:
        return _matches(name, self.ingredient_a, self.aliases_a)

    def matches_b(self, name: str) -> bool:
        return _matches(name, self.ingredient_b, self.aliases_b)


@dataclass(frozen=True)
class Irritant:
    ingredient: str
    aliases: tuple[str, ...]
    effect: str
    source: str

    def matches(self, name: str) -> bool:
        return _matches(name, self.ingredient, self.aliases)


def _matches(name: str, canonical: str, aliases: tuple[str, ...]) -> bool:
    key = normalize(name)
    candidates = {normalize(canonical), *(normalize(a) for a in aliases)}
    return key in candidates


@dataclass(frozen=True)
class Rules:
    incompatibilities: tuple[Incompatibility, ...]
    known_irritants: tuple[Irritant, ...]


@lru_cache(maxsize=1)
def load_rules(path: Path | None = None) -> Rules:
    data = json.loads((path or DATA_PATH).read_text())
    incompatibilities = tuple(
        Incompatibility(
            ingredient_a=e["ingredient_a"],
            aliases_a=tuple(e.get("aliases_a", [])),
            ingredient_b=e["ingredient_b"],
            aliases_b=tuple(e.get("aliases_b", [])),
            effect=e["effect"],
            recommendation=e["recommendation"],
            source=e["source"],
        )
        for e in data["incompatibilities"]
    )
    known_irritants = tuple(
        Irritant(
            ingredient=e["ingredient"],
            aliases=tuple(e.get("aliases", [])),
            effect=e["effect"],
            source=e["source"],
        )
        for e in data["known_irritants"]
    )
    return Rules(incompatibilities=incompatibilities, known_irritants=known_irritants)


@dataclass(frozen=True)
class Product:
    name: str
    ingredients: tuple[str, ...]


def check_ingredients(products: list[Product], rules: Rules | None = None) -> dict:
    """Every ingredient pair across all products against the incompatibility
    rules (fuzzy-matched on aliases), and every ingredient against known
    irritants. A lookup, not a model: nothing here is derived or scored.
    """
    rules = rules or load_rules()
    entries = [(p.name, ing) for p in products for ing in p.ingredients]

    incompatibilities_found = []
    seen_pairs = set()
    for i, (name_a, ing_a) in enumerate(entries):
        for name_b, ing_b in entries[i + 1 :]:
            for rule in rules.incompatibilities:
                matched = (rule.matches_a(ing_a) and rule.matches_b(ing_b)) or (
                    rule.matches_a(ing_b) and rule.matches_b(ing_a)
                )
                if not matched:
                    continue
                key = (id(rule), tuple(sorted([normalize(ing_a), normalize(ing_b)])))
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                incompatibilities_found.append(
                    {
                        "ingredient_a": ing_a,
                        "product_a": name_a,
                        "ingredient_b": ing_b,
                        "product_b": name_b,
                        "effect": rule.effect,
                        "recommendation": rule.recommendation,
                        "source": rule.source,
                    }
                )

    irritants_found = []
    seen_irritants = set()
    for name, ing in entries:
        for rule in rules.known_irritants:
            if not rule.matches(ing):
                continue
            key = (normalize(ing), name)
            if key in seen_irritants:
                continue
            seen_irritants.add(key)
            irritants_found.append(
                {
                    "ingredient": ing,
                    "product": name,
                    "effect": rule.effect,
                    "source": rule.source,
                }
            )

    flagged = {normalize(f["ingredient_a"]) for f in incompatibilities_found}
    flagged |= {normalize(f["ingredient_b"]) for f in incompatibilities_found}
    flagged |= {normalize(f["ingredient"]) for f in irritants_found}

    clear = []
    seen_clear = set()
    for name, ing in entries:
        key = (normalize(ing), name)
        if normalize(ing) in flagged or key in seen_clear:
            continue
        seen_clear.add(key)
        clear.append({"ingredient": ing, "product": name})

    return {
        "products": [
            {"name": p.name, "ingredients": list(p.ingredients)} for p in products
        ],
        "incompatibilities": incompatibilities_found,
        "irritants": irritants_found,
        "clear": clear,
    }
