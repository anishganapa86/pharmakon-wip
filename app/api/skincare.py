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
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .products import LabelIngredient

DATA_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "ingredient_interactions.json"
)


# --- Ingredient-text splitting ---------------------------------------------------
#
# Shared with scripts/_skincare_common.py (the product importer/verifier),
# which imports these from here rather than defining its own copy -- app/
# code must not depend on scripts/, so the dependency runs this direction.


def normalize_line_endings(text: str) -> str:
    """CRLF/CR -> LF, so stored strings never carry \\r regardless of the
    platform a page or a pasted file came from."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


_TRAILING_CODE_RE = re.compile(r"\(\s*CODE\b[^)]*\)\s*$", re.IGNORECASE)


@dataclass
class SplitResult:
    ingredients: list[str]
    # The exact trailing batch-code parenthetical (e.g. "(CODE FIL:
    # N70042172/1)"), if one was found and excluded from `ingredients`. It
    # stays in the caller's raw_ingredient_text; only the parsed array drops
    # it, since a batch code is packaging boilerplate, not an ingredient.
    excluded_trailing_code: str | None


def _split_top_level(text: str) -> list[str]:
    """Split on commas, bullets (•), and newlines -- except a comma
    inside parentheses ("Glycerin (Kosher, Vegetable)" is one ingredient),
    or a comma with a digit immediately on both sides, which is part of a
    chemical name ("1,2-Hexanediol") rather than a separator."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    n = len(text)
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            current.append(ch)
        elif ch == "," and depth == 0:
            prev_digit = i > 0 and text[i - 1].isdigit()
            next_digit = i + 1 < n and text[i + 1].isdigit()
            if prev_digit and next_digit:
                current.append(ch)
            else:
                parts.append("".join(current))
                current = []
        elif ch in ("•", "\n") and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def split_ingredients_detailed(raw_text: str) -> SplitResult:
    """Verbatim split: preserves source spelling, case, and order. Only
    whitespace is collapsed and a trailing batch code / trailing period on
    the last item are trimmed -- nothing is reworded, added, or reordered."""
    text = normalize_line_endings(raw_text)
    stripped = text.rstrip()
    code_match = _TRAILING_CODE_RE.search(stripped)
    excluded_code: str | None = None
    working = text
    if code_match:
        excluded_code = code_match.group(0).strip()
        working = stripped[: code_match.start()]

    raw_parts = _split_top_level(working)
    cleaned = [re.sub(r"\s+", " ", p).strip() for p in raw_parts]
    cleaned = [p for p in cleaned if p]
    if cleaned and cleaned[-1].endswith("."):
        cleaned[-1] = cleaned[-1][:-1].rstrip()

    return SplitResult(cleaned, excluded_code)


def split_ingredients(raw_text: str) -> list[str]:
    return split_ingredients_detailed(raw_text).ingredients


_PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s*%")

# openFDA ingredient strings routinely wrap the actual ingredient name in
# dosage-form/label boilerplate (e.g. "Purpose Salicylic Acid 2.0% Acne
# Medication"). These carry no ingredient information themselves, so they're
# dropped before matching rather than left to (not) match as literal words.
_FILLER_WORDS = frozenset({"purpose", "active", "inactive", "medication"})


def normalize(s: str) -> str:
    s = s.lower().strip()
    s = _PERCENT_RE.sub(" ", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    words = [w for w in s.split() if w not in _FILLER_WORDS]
    return " ".join(words)


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
        return _matched_term(name, self.ingredient_a, self.aliases_a) is not None

    def matches_b(self, name: str) -> bool:
        return _matched_term(name, self.ingredient_b, self.aliases_b) is not None

    def matched_term_a(self, name: str) -> str | None:
        return _matched_term(name, self.ingredient_a, self.aliases_a)

    def matched_term_b(self, name: str) -> str | None:
        return _matched_term(name, self.ingredient_b, self.aliases_b)


@dataclass(frozen=True)
class Irritant:
    ingredient: str
    aliases: tuple[str, ...]
    effect: str
    source: str

    def matches(self, name: str) -> bool:
        return _matched_term(name, self.ingredient, self.aliases) is not None

    def matched_term(self, name: str) -> str | None:
        return _matched_term(name, self.ingredient, self.aliases)


def _matched_term(name: str, canonical: str, aliases: tuple[str, ...]) -> str | None:
    """The rule's ingredient name or alias that appears as a whole-word
    phrase inside the (normalized) label string, or None. Substring rather
    than exact match, because openFDA ingredient strings carry extra words
    (dosage, purpose, product form) around the actual ingredient name -- e.g.
    "Purpose Salicylic Acid 2.0% Acne Medication" must still match "salicylic
    acid". Returns the literal canonical/alias text that matched, exactly as
    written in the rule, so a finding can show which rule term fired --
    never a new rule or alias of its own.
    """
    key = normalize(name)
    if not key:
        return None
    for candidate in (canonical, *aliases):
        needle = normalize(candidate)
        if needle and re.search(rf"\b{re.escape(needle)}\b", key):
            return candidate
    return None


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


_SAME_PRODUCT_RECOMMENDATION = (
    "Both ingredients are in the same product, so the source's timing advice "
    "doesn't apply directly. The source says: {original}. Ask a pharmacist "
    "or dermatologist about this product."
)


def check_ingredients(products: list[Product], rules: Rules | None = None) -> dict:
    """Every ingredient pair across all products against the incompatibility
    rules (fuzzy-matched on aliases), and every ingredient against known
    irritants. A lookup, not a model: nothing here is derived or scored.

    Each finding carries matched_ingredient(_a/_b) (the exact stored string)
    and matched_alias(_a/_b) (the literal rule term -- canonical name or
    alias -- that matched it), so a finding is traceable to the specific
    text that triggered it, not just "this rule fired somewhere."
    """
    rules = rules or load_rules()
    entries = [(p.name, ing) for p in products for ing in p.ingredients]

    incompatibilities_found = []
    seen_pairs = set()
    for i, (name_a, ing_a) in enumerate(entries):
        for name_b, ing_b in entries[i + 1 :]:
            for rule in rules.incompatibilities:
                term_a = rule.matched_term_a(ing_a)
                term_b = rule.matched_term_b(ing_b)
                if term_a is None or term_b is None:
                    term_a = rule.matched_term_b(ing_a)
                    term_b = rule.matched_term_a(ing_b)
                if term_a is None or term_b is None:
                    continue
                key = (id(rule), tuple(sorted([normalize(ing_a), normalize(ing_b)])))
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)

                if name_a == name_b:
                    recommendation = _SAME_PRODUCT_RECOMMENDATION.format(
                        original=rule.recommendation
                    )
                else:
                    recommendation = rule.recommendation

                incompatibilities_found.append(
                    {
                        "ingredient_a": ing_a,
                        "product_a": name_a,
                        "matched_ingredient_a": ing_a,
                        "matched_alias_a": term_a,
                        "ingredient_b": ing_b,
                        "product_b": name_b,
                        "matched_ingredient_b": ing_b,
                        "matched_alias_b": term_b,
                        "effect": rule.effect,
                        "recommendation": recommendation,
                        "source": rule.source,
                    }
                )

    irritants_found = []
    seen_irritants = set()
    for name, ing in entries:
        for rule in rules.known_irritants:
            term = rule.matched_term(ing)
            if term is None:
                continue
            key = (normalize(ing), name)
            if key in seen_irritants:
                continue
            seen_irritants.add(key)
            irritants_found.append(
                {
                    "ingredient": ing,
                    "product": name,
                    "matched_ingredient": ing,
                    "matched_alias": term,
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
        # Total documented rules checked (incompatibilities + irritants) --
        # not a count of what matched. Lets the UI say "no matching rule
        # among our M documented rules" instead of implying "no issues".
        "rules_checked": len(rules.incompatibilities) + len(rules.known_irritants),
    }


def annotate_eczema_context(result: dict, recent_skin_texts: list[str]) -> dict:
    """Adds a contextual note to an already-flagged irritant when the user has
    a recent daily-log entry mentioning eczema AND that irritant's own
    documented (source-cited) effect text calls out eczema-prone skin.

    This never introduces a new claim -- it only surfaces text already present
    in the cited entry, at the moment it's relevant to what the user logged.
    """
    if not any("eczema" in normalize(t) for t in recent_skin_texts):
        return result
    for finding in result["irritants"]:
        if "eczema" in finding["effect"].lower():
            finding["note"] = (
                "You've recently logged something about eczema. This "
                "ingredient's documented effect specifically calls out "
                "eczema-prone skin."
            )
    return result


# --- User-entered products ----------------------------------------------------
#
# Most cosmetics aren't FDA-regulated drugs, so openFDA has no label for them
# (module docstring in app/api/openfda.py). When search finds nothing, the
# user can paste the ingredient list from the product's packaging instead.
# Kept in-process for the lifetime of the server, same pattern as
# app.api.openfda._discovered.


@dataclass(frozen=True)
class UserEnteredProduct:
    id: str
    brand_name: str
    # LabelIngredient (name + strength_mg=None), the same shape
    # app.api.products.decompose() already knows how to resolve -- a
    # user-entered ingredient goes through the identical registry lookup, and
    # comes back `unmatched` for the same honest reason a cosmetic ingredient
    # in an openFDA label would.
    ingredients: tuple[LabelIngredient, ...]


_user_products: dict[str, UserEnteredProduct] = {}


def register_user_product(
    brand_name: str, ingredient_names: list[str]
) -> UserEnteredProduct:
    product = UserEnteredProduct(
        id=f"user_{uuid.uuid4().hex[:12]}",
        brand_name=brand_name,
        ingredients=tuple(
            LabelIngredient(name=n.strip(), strength_mg=None)
            for n in ingredient_names
            if n.strip()
        ),
    )
    _user_products[product.id] = product
    return product


def get_user_product(product_id: str) -> UserEnteredProduct | None:
    return _user_products.get(product_id)


def clear_user_products() -> None:
    _user_products.clear()
