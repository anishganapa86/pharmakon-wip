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

Every network call here can raise `OpenFDAUnavailable`; callers decide how to
degrade. `app/api/main.py` falls back to whatever results it already has
(the synthetic catalog) rather than failing the search.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import httpx

OPENFDA_LABEL_URL = "https://api.fda.gov/drug/label.json"

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


# product_id -> OpenFDAProduct, populated as search results are parsed, so a
# later POST /doses for a discovered (non-catalog) product can find it again.
_discovered: dict[str, OpenFDAProduct] = {}


def get_discovered(product_id: str) -> OpenFDAProduct | None:
    return _discovered.get(product_id)


def clear_discovered() -> None:
    _discovered.clear()


def _get(search: str, limit: int) -> list[dict]:
    try:
        r = _http.get(OPENFDA_LABEL_URL, params={"search": search, "limit": limit})
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


def search_by_name(q: str, limit: int = 5) -> list[OpenFDAProduct]:
    """Brand and generic name, as a user would type either."""
    q = q.strip()
    if not q:
        return []
    results = _get(f'(openfda.brand_name:"{q}") OR (openfda.generic_name:"{q}")', limit)
    products = [p for r in results if (p := _to_product(r, "name")) is not None]
    return _dedupe(products)[:limit]


def search_by_condition_fields(q: str, limit: int = 5) -> list[OpenFDAProduct]:
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
        for r in _get(f'{field}:"{q}"', limit):
            p = _to_product(r, matched_via)
            if p is not None:
                products.append(p)
    return _dedupe(products)[:limit]
