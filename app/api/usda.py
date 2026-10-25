from __future__ import annotations

import logging
import os
import time

import httpx

from .products import COMPONENT_INGREDIENT, LabelIngredient, Product

logging.getLogger("httpx").setLevel(logging.WARNING)

SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
DATA_TYPES = ["Foundation", "SR Legacy", "Survey (FNDDS)"]
TIMEOUT_SECONDS = 5.0
PAGE_SIZE = 8
NUTRIENT_COMPONENTS = {"262": "caffeine", "221": "alcohol"}
REPORTED_NUTRIENTS = {"262": "caffeine", "221": "alcohol", "430": "vitamin_k"}
FAILURE_RETRY_SECONDS = 60.0
RETRYABLE_STATUSES = frozenset({400, 500, 502, 503, 504})
ATTEMPTS = 2
RETRY_DELAY_SECONDS = 1.0

_http = httpx.Client(timeout=TIMEOUT_SECONDS)
_cache: dict[str, list[Product]] = {}
_failed_at: dict[str, float] = {}
_nutrients: dict[str, dict[str, float]] = {}
_discovered: dict[str, Product] = {}


def enabled() -> bool:
    return bool(os.environ.get("USDA_FDC_API_KEY", "").strip())


def clear_cache() -> None:
    _cache.clear()
    _failed_at.clear()
    _nutrients.clear()
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


def reported_nutrients(food: dict) -> dict[str, float]:
    found: dict[str, float] = {}
    for nutrient in food.get("foodNutrients") or []:
        key = REPORTED_NUTRIENTS.get(str(nutrient.get("nutrientNumber")))
        value = nutrient.get("value")
        if key and isinstance(value, (int, float)) and value > 0:
            found[key] = float(value)
    return found


def get_nutrients(product_id: str) -> dict[str, float]:
    return _nutrients.get(product_id, {})


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
    if time.monotonic() - _failed_at.get(key, -1e9) < FAILURE_RETRY_SECONDS:
        return []
    foods = None
    for attempt in range(ATTEMPTS):
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
            break
        except httpx.HTTPStatusError as exc:
            if (
                exc.response.status_code in RETRYABLE_STATUSES
                and attempt + 1 < ATTEMPTS
            ):
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            break
        except (httpx.HTTPError, ValueError):
            break
    if foods is None:
        _failed_at[key] = time.monotonic()
        return []
    products = [p for p in (_to_product(f) for f in foods) if p is not None]
    for food, product in zip(foods, [_to_product(f) for f in foods], strict=True):
        if product is not None:
            _discovered[product.id] = product
            _nutrients[product.id] = reported_nutrients(food)
    _cache[key] = products
    return products
