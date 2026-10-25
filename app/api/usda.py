from __future__ import annotations

import logging
import os

import httpx

from .products import COMPONENT_INGREDIENT, LabelIngredient, Product

logging.getLogger("httpx").setLevel(logging.WARNING)

SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
DATA_TYPES = ["Foundation", "SR Legacy", "Survey (FNDDS)"]
TIMEOUT_SECONDS = 5.0
PAGE_SIZE = 8
NUTRIENT_COMPONENTS = {"262": "caffeine", "221": "alcohol", "430": "vitamin_k"}

_http = httpx.Client(timeout=TIMEOUT_SECONDS)
_cache: dict[str, list[Product]] = {}
_discovered: dict[str, Product] = {}


def enabled() -> bool:
    return bool(os.environ.get("USDA_FDC_API_KEY", "").strip())


def clear_cache() -> None:
    _cache.clear()
    _discovered.clear()


def get_discovered(product_id: str) -> Product | None:
    return _discovered.get(product_id)


def _components(food: dict) -> list[str]:
    found: list[str] = []
    for nutrient in food.get("foodNutrients") or []:
        key = NUTRIENT_COMPONENTS.get(str(nutrient.get("nutrientNumber")))
        value = nutrient.get("value")
        if key and isinstance(value, (int, float)) and value > 0 and key not in found:
            found.append(key)
    return found


def _to_product(food: dict) -> Product | None:
    fdc_id = food.get("fdcId")
    name = (food.get("description") or "").strip()
    if not fdc_id or not name:
        return None
    components = _components(food)
    if components:
        ingredients = tuple(
            LabelIngredient(COMPONENT_INGREDIENT[c], None) for c in components
        )
    else:
        ingredients = (LabelIngredient(name.upper()[:80], None),)
    return Product(f"usda_{fdc_id}", name[:120], ingredients)


def search(query: str) -> list[Product]:
    text = query.strip()
    if not text or not enabled():
        return []
    key = text.lower()
    if key in _cache:
        return _cache[key]
    try:
        response = _http.get(
            SEARCH_URL,
            params={
                "query": text,
                "dataType": ",".join(DATA_TYPES),
                "pageSize": PAGE_SIZE,
            },
            headers={"X-Api-Key": os.environ["USDA_FDC_API_KEY"].strip()},
        )
        response.raise_for_status()
        foods = response.json().get("foods") or []
    except (httpx.HTTPError, ValueError):
        return []
    products = [p for p in (_to_product(f) for f in foods) if p is not None]
    for product in products:
        _discovered[product.id] = product
    _cache[key] = products
    return products
