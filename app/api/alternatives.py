"""Curated skincare alternatives: products that avoid a flagged ingredient.

Like app/api/skincare.py, this is a lookup and a re-check, never a model or a
recommendation engine. For an ingredient flagged by the skincare checker
(app/api/skincare.py's incompatibilities/irritants), this looks for curated
products (app/data/skincare_products.json) that:

  1) don't contain the flagged ingredient or any of its cited aliases
     (from app/data/ingredient_interactions.json, via app.api.skincare), and
  2) produce no NEW incompatibility or irritant flag when the full skincare
     checker runs on the user's other logged products plus the candidate.

A candidate with a missing or empty ingredient list is excluded, never
assumed clean -- app/data/skincare_products.json's own loader already drops
any entry that doesn't carry a real ingredient list and (for "brand"/
"retailer" entries, or legacy ones with no source_type) a source_url (see
its _README), so this module never even sees a placeholder.

A "packaging" entry (checked against the physical product, not a page) has
no source_url and is loaded anyway -- it's still used to recognize a
flagged product as one of our own (category lookup), which only requires
knowing about it. It is never offered as an alternative to someone else
unless a human has verified it AND left a reference_url; see
_is_candidate_eligible.

This never claims a candidate is better, safer, or will help -- only what
was checked and what it lacks. It never touches medication ingredients: a
name that resolves against the PK registry (app.registry.loader) is
rejected, since this checker's whole domain is topical/skincare products.

Skincare only, never app.pk or app.detect.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode

from ..registry.loader import Registry
from . import skincare

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "skincare_products.json"

MAX_ALTERNATIVES = 5

DISCLAIMER = (
    "Ingredient lists were copied from manufacturer pages and may be out of "
    "date. Check the product packaging."
)


# --- Curated data ------------------------------------------------------------


@dataclass(frozen=True)
class CuratedProduct:
    brand: str
    name: str
    category: str
    ingredients: tuple[str, ...]
    source_url: str
    date_checked: str
    verified: bool
    # None for entries written before source_type existed -- treated as the
    # old "brand/retailer"-style shape (source_url required, always a
    # candidate). See _is_real_entry and _is_candidate_eligible.
    source_type: str | None = None
    reference_url: str | None = None


def _is_real_entry(e: dict) -> bool:
    """False for a placeholder: empty ingredients, or -- for anything other
    than a "packaging" entry -- a missing/"TODO" source_url. CLAUDE.md's "no
    source, no entry" invariant, enforced here rather than trusted -- a
    placeholder is an expected, in-progress state of the data file, never a
    load error, and it must simply never reach a user.

    A "packaging" entry (checked against the physical product, not a page)
    has no source_url at all; that's not a placeholder, it's the documented
    shape for that source_type (see scripts/validate_skincare_products.py).
    It still needs real ingredients to be loaded at all.
    """
    ingredients = e.get("ingredients") or []
    if not ingredients:
        return False
    if e.get("source_type") == "packaging":
        return True
    source_url = (e.get("source_url") or "").strip()
    return bool(source_url) and source_url != "TODO"


def _is_candidate_eligible(c: CuratedProduct) -> bool:
    """Every loaded entry can be used to infer a flagged product's own
    category (see _infer_category) -- that's just recognizing the user's own
    product. But a "packaging" entry (no source_url; checked against the
    physical product rather than a page) is only ever offered to someone
    else as an alternative once a human has verified it AND left a
    reference_url -- until then there's nothing another user could open to
    see what was checked. "brand"/"retailer" entries, and legacy entries
    with no source_type at all, behave as before: eligible once loaded.
    """
    if c.source_type == "packaging":
        return c.verified and bool(c.reference_url)
    return True


@lru_cache(maxsize=1)
def load_products(path: Path | None = None) -> tuple[CuratedProduct, ...]:
    data = json.loads((path or DATA_PATH).read_text())
    return tuple(
        CuratedProduct(
            brand=e["brand"],
            name=e["name"],
            category=e["category"],
            ingredients=tuple(e["ingredients"]),
            source_url=e.get("source_url") or "",
            date_checked=e.get("date_checked", ""),
            verified=e.get("verified", False),
            source_type=e.get("source_type"),
            reference_url=e.get("reference_url"),
        )
        for e in data["products"]
        if _is_real_entry(e)
    )


def clear_cache() -> None:
    load_products.cache_clear()


# --- Matching ------------------------------------------------------------------


def _ingredient_matches(ingredient: str, target: str) -> bool:
    """Whole-word substring match on normalized text -- the same tolerance
    app.api.skincare uses for messy label strings (e.g. "Purpose Salicylic
    Acid 2.0% Acne Medication" still matching "salicylic acid")."""
    key = skincare.normalize(ingredient)
    needle = skincare.normalize(target)
    if not key or not needle:
        return False
    return re.search(rf"\b{re.escape(needle)}\b", key) is not None


def _aliases_for(flagged_name: str, rules: skincare.Rules) -> tuple[str, ...]:
    """The flagged ingredient's own name plus every alias any cited rule
    documents for it -- never invented, always what ingredient_interactions.json
    already says."""
    found = {flagged_name}
    for rule in rules.incompatibilities:
        if rule.matches_a(flagged_name):
            found.add(rule.ingredient_a)
            found.update(rule.aliases_a)
        if rule.matches_b(flagged_name):
            found.add(rule.ingredient_b)
            found.update(rule.aliases_b)
    for rule in rules.known_irritants:
        if rule.matches(flagged_name):
            found.add(rule.ingredient)
            found.update(rule.aliases)
    return tuple(found)


def _contains_any(candidate: CuratedProduct, targets: tuple[str, ...]) -> bool:
    return any(
        _ingredient_matches(ingredient, target)
        for ingredient in candidate.ingredients
        for target in targets
    )


def _introduces_new_flags(
    candidate_name: str,
    candidate_ingredients: tuple[str, ...],
    other_products: list[skincare.Product],
    rules: skincare.Rules,
) -> bool:
    candidate_product = skincare.Product(candidate_name, candidate_ingredients)
    result = skincare.check_ingredients([candidate_product, *other_products], rules)
    for f in result["incompatibilities"]:
        if f["product_a"] == candidate_name or f["product_b"] == candidate_name:
            return True
    return any(f["product"] == candidate_name for f in result["irritants"])


def _infer_category(
    product_name: str, curated: tuple[CuratedProduct, ...]
) -> str | None:
    """The flagged product's category, only when it happens to itself be one
    of the curated products (matched by brand+name or name alone). There is
    no other source for a logged or manually-entered product's category, and
    guessing one is exactly what this project refuses to do -- so when this
    comes back None, alternatives are searched across every curated category
    rather than one invented for the occasion.
    """
    key = skincare.normalize(product_name)
    for c in curated:
        if (
            skincare.normalize(f"{c.brand} {c.name}") == key
            or skincare.normalize(c.name) == key
        ):
            return c.category
    return None


def _search_url(brand: str, name: str) -> str:
    """Plain Google Shopping search, no affiliate or tracking parameters."""
    return "https://www.google.com/search?" + urlencode(
        {"tbm": "shop", "q": f"{brand} {name}"}
    )


def _candidate_json(candidate: CuratedProduct, flagged_display: str) -> dict:
    return {
        "brand": candidate.brand,
        "name": candidate.name,
        "category": candidate.category,
        "ingredients": list(candidate.ingredients),
        "source_url": candidate.source_url,
        "date_checked": candidate.date_checked,
        "verified": candidate.verified,
        "checked_note": (
            f"Does not contain {flagged_display}. "
            "No documented interactions found with your other products."
        ),
        "search_url": _search_url(candidate.brand, candidate.name),
    }


def _alternatives_for(
    flagged_name: str,
    product_name: str,
    other_products: list[skincare.Product],
    curated: tuple[CuratedProduct, ...],
    rules: skincare.Rules,
) -> dict:
    targets = _aliases_for(flagged_name, rules)
    # Category lookup uses every loaded entry, including ineligible
    # packaging ones -- it's only ever recognizing the flagged product
    # against itself, never suggesting it to someone else.
    category = _infer_category(product_name, curated)
    eligible = tuple(c for c in curated if _is_candidate_eligible(c))
    pool = (
        eligible
        if category is None
        else tuple(c for c in eligible if c.category == category)
    )

    alternatives: list[dict] = []
    for candidate in pool:
        if _contains_any(candidate, targets):
            continue
        candidate_name = f"{candidate.brand} {candidate.name}"
        if _introduces_new_flags(
            candidate_name, candidate.ingredients, other_products, rules
        ):
            continue
        alternatives.append(_candidate_json(candidate, flagged_name))
        if len(alternatives) == MAX_ALTERNATIVES:
            break

    return {
        "flagged_ingredient": flagged_name,
        "product_name": product_name,
        "category": category,
        "alternatives": alternatives,
    }


def _flagged_ingredient_names(product_name: str, check_result: dict) -> list[str]:
    """Every ingredient (deduped, first-seen order) this product was flagged
    for -- by name or by whichever alias the checker actually matched on."""
    names: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        key = skincare.normalize(name)
        if key not in seen:
            seen.add(key)
            names.append(name)

    for f in check_result["incompatibilities"]:
        if f["product_a"] == product_name:
            add(f["ingredient_a"])
        if f["product_b"] == product_name:
            add(f["ingredient_b"])
    for f in check_result["irritants"]:
        if f["product"] == product_name:
            add(f["ingredient"])
    return names


# --- Entry points --------------------------------------------------------------


def check_alternatives_for_product(
    target: skincare.Product,
    regimen: list[skincare.Product],
    curated: tuple[CuratedProduct, ...] | None = None,
) -> dict:
    """Alternatives for one product's flagged ingredients, checked against
    the rest of the user's regimen (`regimen`, which must not include
    `target` itself)."""
    curated = load_products() if curated is None else curated
    rules = skincare.load_rules()
    check_result = skincare.check_ingredients([target, *regimen], rules)
    flagged = _flagged_ingredient_names(target.name, check_result)
    results = [
        _alternatives_for(name, target.name, regimen, curated, rules)
        for name in flagged
    ]
    return {"results": results, "disclaimer": DISCLAIMER}


def check_alternatives_manual(
    products: list[skincare.Product],
    curated: tuple[CuratedProduct, ...] | None = None,
) -> dict:
    """Alternatives for every flagged ingredient across a manually entered
    list of products -- the "check a new product" form's equivalent."""
    curated = load_products() if curated is None else curated
    rules = skincare.load_rules()
    check_result = skincare.check_ingredients(products, rules)

    results = []
    for i, product in enumerate(products):
        flagged = _flagged_ingredient_names(product.name, check_result)
        if not flagged:
            continue
        other_products = [p for j, p in enumerate(products) if j != i]
        results.extend(
            _alternatives_for(name, product.name, other_products, curated, rules)
            for name in flagged
        )
    return {"results": results, "disclaimer": DISCLAIMER}


# --- Medication guard ------------------------------------------------------------


def medication_ingredient_names(names: list[str], registry: Registry) -> list[str]:
    """Names among these that resolve against the PK medication registry --
    this checker's entire domain is topical/skincare products, and a name
    that resolves here almost certainly means someone typed a medication into
    the wrong form. Never silently accepted."""
    return [name for name in names if registry.resolve(name) is not None]
