"""Skincare ingredient interaction checker: a lookup against documented
dermatological guidelines, never a model.

This is deliberately NOT pharmacokinetics. There is no curve, no
concentration, and no invented risk score here -- "high risk" and "low risk"
are not in this module's vocabulary. It checks ingredient pairs and single
ingredients against app/data/ingredient_interactions.json and reports only
what that file says. Every rule in that file carries a source citation (see
its `_README`); a rule with no source doesn't belong there, and this module
adds none of its own.

Topical ingredients are out of scope for app/pk/ (project notes: "topical-only
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

from . import verification
from .products import LabelIngredient

DATA_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "ingredient_interactions.json"
)


class InvalidRule(ValueError):
    pass


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
    verified: bool = False

    def matches_a(self, name: str) -> bool:
        return _matched_term(name, self.ingredient_a, self.aliases_a) is not None

    def matches_b(self, name: str) -> bool:
        return _matched_term(name, self.ingredient_b, self.aliases_b) is not None

    def matched_term_a(self, name: str) -> str | None:
        return _matched_term(name, self.ingredient_a, self.aliases_a)

    def matched_term_b(self, name: str) -> str | None:
        return _matched_term(name, self.ingredient_b, self.aliases_b)

    def match_a(self, name: str) -> TermMatch | None:
        return _match(name, self.ingredient_a, self.aliases_a)

    def match_b(self, name: str) -> TermMatch | None:
        return _match(name, self.ingredient_b, self.aliases_b)


@dataclass(frozen=True)
class Irritant:
    ingredient: str
    aliases: tuple[str, ...]
    effect: str
    source: str
    verified: bool = False

    def matches(self, name: str) -> bool:
        return _matched_term(name, self.ingredient, self.aliases) is not None

    def matched_term(self, name: str) -> str | None:
        return _matched_term(name, self.ingredient, self.aliases)

    def match(self, name: str) -> TermMatch | None:
        return _match(name, self.ingredient, self.aliases)


MATCH_INCI = "inci_name"
MATCH_COMMON_NAME_ONLY = "common_name_only"


@dataclass(frozen=True)
class TermMatch:
    term: str
    quality: str


def _inci_part(name: str) -> str:
    return name.split("(", 1)[0]


def _first_term_in(key: str, canonical: str, aliases: tuple[str, ...]) -> str | None:
    for candidate in (canonical, *aliases):
        needle = normalize(candidate)
        if needle and re.search(rf"\b{re.escape(needle)}\b", key):
            return candidate
    return None


def _match(name: str, canonical: str, aliases: tuple[str, ...]) -> TermMatch | None:
    """Rule terms are matched against the INCI name (the text before any
    parenthesis) first; a term found only inside the parenthetical is
    reported as common_name_only rather than dropped, so the caller can say
    the rule may describe a different ingredient than the one on the label."""
    inci_key = normalize(_inci_part(name))
    if inci_key:
        term = _first_term_in(inci_key, canonical, aliases)
        if term is not None:
            return TermMatch(term, MATCH_INCI)
    full_key = normalize(name)
    if not full_key:
        return None
    term = _first_term_in(full_key, canonical, aliases)
    if term is None:
        return None
    return TermMatch(term, MATCH_COMMON_NAME_ONLY)


def common_name_note(match: TermMatch, rule_ingredient: str) -> str | None:
    if match.quality != MATCH_COMMON_NAME_ONLY:
        return None
    return (
        "Matched on the label's common name only. The rule describes "
        f"{rule_ingredient}; this ingredient may behave differently."
    )


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
    found = _match(name, canonical, aliases)
    return found.term if found else None


@dataclass(frozen=True)
class Rules:
    incompatibilities: tuple[Incompatibility, ...]
    known_irritants: tuple[Irritant, ...]


def _reject_incomplete_verification(entry: dict, label: str) -> None:
    missing = verification.missing_verification_fields(entry)
    if missing:
        raise InvalidRule(
            f"{label} is marked verified but is missing {', '.join(missing)}"
        )


@lru_cache(maxsize=1)
def load_rules(path: Path | None = None) -> Rules:
    data = json.loads((path or DATA_PATH).read_text())
    for e in data["incompatibilities"]:
        _reject_incomplete_verification(
            e, f"incompatibility {e.get('ingredient_a')!r} + {e.get('ingredient_b')!r}"
        )
    for e in data["known_irritants"]:
        _reject_incomplete_verification(e, f"irritant {e.get('ingredient')!r}")
    incompatibilities = tuple(
        Incompatibility(
            ingredient_a=e["ingredient_a"],
            aliases_a=tuple(e.get("aliases_a", [])),
            ingredient_b=e["ingredient_b"],
            aliases_b=tuple(e.get("aliases_b", [])),
            effect=e["effect"],
            recommendation=e["recommendation"],
            source=e["source"],
            verified=e.get("verified") is True,
        )
        for e in data["incompatibilities"]
    )
    known_irritants = tuple(
        Irritant(
            ingredient=e["ingredient"],
            aliases=tuple(e.get("aliases", [])),
            effect=e["effect"],
            source=e["source"],
            verified=e.get("verified") is True,
        )
        for e in data["known_irritants"]
    )
    return Rules(incompatibilities=incompatibilities, known_irritants=known_irritants)


def unverified_rule_count(rules: Rules | None = None) -> int:
    rules = rules or load_rules()
    every = (*rules.incompatibilities, *rules.known_irritants)
    return sum(1 for r in every if not r.verified)


def active_rules(rules: Rules | None = None) -> Rules:
    rules = rules or load_rules()
    if verification.show_unverified_rules():
        return rules
    return Rules(
        incompatibilities=tuple(r for r in rules.incompatibilities if r.verified),
        known_irritants=tuple(r for r in rules.known_irritants if r.verified),
    )


@dataclass(frozen=True)
class Product:
    name: str
    ingredients: tuple[str, ...]


UNVERIFIED_TIMING_PREFIX = "Suggested timing from our unverified reference: "
_SAME_PRODUCT_UNVERIFIED = (
    "Both ingredients are in the same product, so the reference's timing "
    "advice doesn't apply directly. The reference suggests: {text} "
    "(unverified). Ask a pharmacist or dermatologist about this product."
)
_SAME_PRODUCT_VERIFIED = (
    "Both ingredients are in the same product, so the source's timing advice "
    "doesn't apply directly. The source says: {text}. Ask a pharmacist "
    "or dermatologist about this product."
)


def _strip_trailing_period(text: str) -> str:
    return text.strip().rstrip(".").rstrip()


def _recommendation_text(rule_text: str, verified: bool, same_product: bool) -> str:
    bare = _strip_trailing_period(rule_text)
    if same_product:
        template = _SAME_PRODUCT_VERIFIED if verified else _SAME_PRODUCT_UNVERIFIED
        return template.format(text=bare)
    if verified:
        return rule_text
    return f"{UNVERIFIED_TIMING_PREFIX}{bare}."


def _appears_in(ingredient: str, product_names: list[str]) -> str:
    return f"{ingredient} appears in: {', '.join(product_names)}"


def check_ingredients(products: list[Product], rules: Rules | None = None) -> dict:
    """Every ingredient pair across all products against the incompatibility
    rules (fuzzy-matched on aliases), and every ingredient against known
    irritants. A lookup, not a model: nothing here is derived or scored.

    Each finding carries matched_ingredient(_a/_b) (the exact stored string)
    and matched_alias(_a/_b) (the literal rule term -- canonical name or
    alias -- that matched it), so a finding is traceable to the specific
    text that triggered it, not just "this rule fired somewhere."
    """
    all_rules = rules or load_rules()
    rules = active_rules(all_rules)
    entries = [(p.name, ing) for p in products for ing in p.ingredients]

    groups: dict[tuple, dict] = {}
    for i, (name_a, ing_a) in enumerate(entries):
        for name_b, ing_b in entries[i + 1 :]:
            for rule in rules.incompatibilities:
                match_a = rule.match_a(ing_a)
                match_b = rule.match_b(ing_b)
                rule_ing_a, rule_ing_b = rule.ingredient_a, rule.ingredient_b
                if match_a is None or match_b is None:
                    match_a = rule.match_b(ing_a)
                    match_b = rule.match_a(ing_b)
                    rule_ing_a, rule_ing_b = rule.ingredient_b, rule.ingredient_a
                if match_a is None or match_b is None:
                    continue
                same_product = name_a == name_b
                pair = tuple(sorted([normalize(ing_a), normalize(ing_b)]))
                key = (id(rule), pair, name_a if same_product else None)
                group = groups.get(key)
                if group is None:
                    group = {
                        "rule": rule,
                        "same_product": same_product,
                        "ing_a": ing_a,
                        "ing_b": ing_b,
                        "match_a": match_a,
                        "match_b": match_b,
                        "rule_ing_a": rule_ing_a,
                        "rule_ing_b": rule_ing_b,
                        "products_a": [],
                        "products_b": [],
                    }
                    groups[key] = group
                if normalize(ing_a) == normalize(group["ing_a"]):
                    side_a, side_b = name_a, name_b
                else:
                    side_a, side_b = name_b, name_a
                if side_a not in group["products_a"]:
                    group["products_a"].append(side_a)
                if side_b not in group["products_b"]:
                    group["products_b"].append(side_b)

    incompatibilities_found = []
    for group in groups.values():
        rule = group["rule"]
        products_a = group["products_a"]
        products_b = group["products_b"]
        same_product = group["same_product"]
        ing_a, ing_b = group["ing_a"], group["ing_b"]
        incompatibilities_found.append(
            {
                "ingredient_a": ing_a,
                "product_a": products_a[0],
                "products_a": products_a,
                "appears_in_a": (
                    None if same_product else _appears_in(ing_a, products_a)
                ),
                "matched_ingredient_a": ing_a,
                "matched_alias_a": group["match_a"].term,
                "match_quality_a": group["match_a"].quality,
                "match_note_a": common_name_note(group["match_a"], group["rule_ing_a"]),
                "ingredient_b": ing_b,
                "product_b": products_b[0],
                "products_b": products_b,
                "appears_in_b": (
                    None if same_product else _appears_in(ing_b, products_b)
                ),
                "matched_ingredient_b": ing_b,
                "matched_alias_b": group["match_b"].term,
                "match_quality_b": group["match_b"].quality,
                "match_note_b": common_name_note(group["match_b"], group["rule_ing_b"]),
                "effect": rule.effect,
                "recommendation": _recommendation_text(
                    rule.recommendation, rule.verified, same_product
                ),
                "source": rule.source,
                "verified": rule.verified,
            }
        )

    irritants_found = []
    seen_irritants = set()
    for name, ing in entries:
        for rule in rules.known_irritants:
            term_match = rule.match(ing)
            if term_match is None:
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
                    "matched_alias": term_match.term,
                    "match_quality": term_match.quality,
                    "match_note": common_name_note(term_match, rule.ingredient),
                    "effect": rule.effect,
                    "source": rule.source,
                    "verified": rule.verified,
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

    body = {
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
    return verification.attach_notice(body, unverified_rule_count(all_rules))


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
