"""openFDA product discovery: condition-based and broadened name search.

Supplements the synthetic seed catalog (app/api/products.py) with real
products pulled from the FDA's public drug label API. People search for what
they HAVE ("eczema cream"), not just what they TAKE ("hydrocortisone"), so a
name-only search misses them. This module also searches the label's
`indications_and_usage` and `purpose` fields, which is how an OTC label
describes what it treats.

Every ingredient found here gets `strength_mg=None`. openFDA label text is
unstructured free-form paragraphs, not a structured dose table, and a regex
guess at a milligram value risks silently mis-attributing a dose -- exactly
what CLAUDE.md's "refuse rather than guess" invariant exists to prevent.
Ingredients still resolve against the registry, so they come back
`unknown_strength` when the substance is recognized and `unmatched` when it
isn't: the same two honest outcomes `app/api/products.py` already produces
for the synthetic catalog. Only the curated synthetic catalog carries a
strength number, and even that is unverified (see products.py's docstring).

Name search (`search_by_name`) tries brand/generic name first, then
substance/manufacturer name, then `drug/drugsfda.json` as a last resort for
names the label API's `openfda.*` fields don't carry. Every search caps at
`RESULT_LIMIT` (10); `app/api/main.py` reports how many more matched so the
client can show "Showing X of Y results".

Every network call here can raise `OpenFDAUnavailable`; callers decide how to
degrade. `app/api/main.py` falls back to whatever results it already has
(the synthetic catalog) rather than failing the search.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

import httpx

OPENFDA_LABEL_URL = "https://api.fda.gov/drug/label.json"
OPENFDA_DRUGSFDA_URL = "https://api.fda.gov/drug/drugsfda.json"

RESULT_LIMIT = 10

# Below this many combined (catalog + openFDA name) hits, also search the
# label fields that describe what a product treats.
MIN_RESULTS_BEFORE_CONDITION_FALLBACK = 3

# Replaced with a mock-transport client in tests.
_http = httpx.Client(timeout=5.0)


class OpenFDAUnavailable(Exception):
    """openFDA could not be reached or answered with an error."""


@dataclass(frozen=True)
class OpenFDAIngredient:
    name: str  # as labelled (openfda.substance_name)
    strength_mg: float | None = None  # always None; see module docstring


@dataclass(frozen=True)
class OpenFDAProduct:
    id: str
    brand_name: str
    ingredients: tuple[OpenFDAIngredient, ...]
    route: str | None  # e.g. "ORAL", "TOPICAL", as labelled; None if absent
    matched_via: str  # "name" | "indications_and_usage" | "purpose"
    # Full ingredient list (active + inactive) as labelled, for TOPICAL
    # products only -- for the skincare ingredient checker (app/api/skincare.py),
    # never for app.pk. None when the label has no active_ingredient /
    # inactive_ingredient text, or the route isn't topical.
    topical_ingredients: tuple[str, ...] | None = None


# product_id -> OpenFDAProduct, populated as search results are parsed, so a
# later POST /doses for a discovered (non-catalog) product can find it again.
_discovered: dict[str, OpenFDAProduct] = {}


def get_discovered(product_id: str) -> OpenFDAProduct | None:
    return _discovered.get(product_id)


def clear_discovered() -> None:
    _discovered.clear()


def _get(url: str, search: str, limit: int) -> list[dict]:
    try:
        r = _http.get(url, params={"search": search, "limit": limit})
    except httpx.HTTPError as exc:
        raise OpenFDAUnavailable(str(exc)) from exc
    if r.status_code == 404:  # openFDA's "no matches"
        return []
    if r.status_code != 200:
        raise OpenFDAUnavailable(f"openFDA returned {r.status_code}")
    return r.json().get("results", [])


def _product_id(brand_name: str, substance_names: list[str]) -> str:
    key = (
        brand_name.strip().lower()
        + "|"
        + "|".join(sorted(s.strip().lower() for s in substance_names))
    )
    return "openfda_" + hashlib.sha1(key.encode()).hexdigest()[:12]


def _parse_ingredient_list(text: str) -> list[str]:
    """Split a label's free-text ingredient paragraph into names.

    Real label text ("Water, Glycerin, Fragrance...") is comma/semicolon
    delimited prose, not a structured list, so this is a delimiter split, not
    a dose parse -- unlike a milligram value (CLAUDE.md: "refuse rather than
    guess"), a missed or extra token here can't misattribute a dose; the
    failure mode is a name the checker doesn't see, not a fabricated number.
    """
    parts = re.split(r"[,;\n]", text)
    return [p.strip().strip(".") for p in parts if p.strip().strip(".")]


def _topical_ingredients(result: dict, route: str | None) -> tuple[str, ...] | None:
    if route != "TOPICAL":
        return None
    names: list[str] = []
    seen: set[str] = set()
    for field in ("active_ingredient", "inactive_ingredient"):
        for text in result.get(field, []):
            for name in _parse_ingredient_list(text):
                key = name.lower()
                if key not in seen:
                    seen.add(key)
                    names.append(name)
    return tuple(names) if names else None


def _to_product(result: dict, matched_via: str) -> OpenFDAProduct | None:
    """None when the label carries no ingredient names to hang a dose on."""
    meta = result.get("openfda", {})
    names = meta.get("brand_name") or meta.get("generic_name") or []
    if not names:
        return None
    brand_name = names[0]
    substances = meta.get("substance_name", [])
    if not substances:
        return None
    route = (meta.get("route") or [None])[0]
    pid = _product_id(brand_name, substances)
    product = OpenFDAProduct(
        id=pid,
        brand_name=brand_name,
        ingredients=tuple(OpenFDAIngredient(name=s) for s in substances),
        route=route,
        matched_via=matched_via,
        topical_ingredients=_topical_ingredients(result, route),
    )
    _discovered[pid] = product
    return product


def _dedupe(products: list[OpenFDAProduct]) -> list[OpenFDAProduct]:
    seen: set[str] = set()
    out = []
    for p in products:
        key = p.brand_name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _drugsfda_to_products(results: list[dict]) -> list[OpenFDAProduct]:
    """drug/drugsfda.json: applications, each with one or more products.

    A last-resort fallback for names the label API's `openfda.*` fields don't
    carry (some older or Rx-only entries lack them). Structured the same way
    as a label result: no strength, since drugsfda doesn't carry per-unit
    dose strength either.
    """
    out = []
    for application in results:
        for prod in application.get("products", []):
            brand_name = prod.get("brand_name")
            substances = [
                ai.get("name")
                for ai in prod.get("active_ingredients", [])
                if ai.get("name")
            ]
            if not brand_name or not substances:
                continue
            pid = _product_id(brand_name, substances)
            product = OpenFDAProduct(
                id=pid,
                brand_name=brand_name,
                ingredients=tuple(OpenFDAIngredient(name=s) for s in substances),
                route=None,
                matched_via="name",
            )
            _discovered[pid] = product
            out.append(product)
    return out


def search_by_name(q: str, limit: int = RESULT_LIMIT) -> list[OpenFDAProduct]:
    """Brand/generic name first; substance/manufacturer if that's empty;
    drug/drugsfda.json as a last resort for names the label API misses."""
    q = q.strip()
    if not q:
        return []
    results = _get(
        OPENFDA_LABEL_URL,
        f'(openfda.brand_name:"{q}") OR (openfda.generic_name:"{q}")',
        limit,
    )
    if not results:
        results = _get(
            OPENFDA_LABEL_URL,
            f'(openfda.substance_name:"{q}") OR (openfda.manufacturer_name:"{q}")',
            limit,
        )
    products = [p for r in results if (p := _to_product(r, "name")) is not None]
    if not products:
        drugsfda_results = _get(
            OPENFDA_DRUGSFDA_URL, f'products.brand_name:"{q}"', limit
        )
        products = _drugsfda_to_products(drugsfda_results)
    return _dedupe(products)[:limit]


def search_by_condition_fields(
    q: str, limit: int = RESULT_LIMIT
) -> list[OpenFDAProduct]:
    """What a product treats: `indications_and_usage` and OTC `purpose`.

    Catches "eczema cream" -> a hydrocortisone label whose indications say
    "for the temporary relief of itching associated with... eczema", and
    "pain relief" -> an OTC label whose purpose says "Pain reliever".
    """
    q = q.strip()
    if not q:
        return []
    products: list[OpenFDAProduct] = []
    for field, matched_via in (
        ("indications_and_usage", "indications_and_usage"),
        ("purpose", "purpose"),
    ):
        for r in _get(OPENFDA_LABEL_URL, f'{field}:"{q}"', limit):
            p = _to_product(r, matched_via)
            if p is not None:
                products.append(p)
    return _dedupe(products)[:limit]
