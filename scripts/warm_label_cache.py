from __future__ import annotations

import argparse
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api import (  # noqa: E402
    food_guidance,
    interactions,
    label_cache,
    products,
    side_effects,
)
from app.registry.loader import Registry, load_registry  # noqa: E402

DEFAULT_INTERVAL = 1.6

FETCHERS: tuple[tuple[str, Callable[[str], object], type[Exception]], ...] = (
    (
        "food guidance labels",
        food_guidance.fetch_labels_for_substance,
        food_guidance.OpenFDAUnavailable,
    ),
    (
        "adverse reactions",
        side_effects.fetch_adverse_reactions,
        side_effects.OpenFDAUnavailable,
    ),
    (
        "drug interactions",
        interactions.fetch_drug_interactions_text,
        interactions.OpenFDAUnavailable,
    ),
)


class Limiter:
    def __init__(self, interval: float):
        self._interval = interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next - now)
            self._next = max(now, self._next) + self._interval
        if delay:
            time.sleep(delay)


class RateLimitedClient(httpx.Client):
    def __init__(self, limiter: Limiter, **kwargs):
        super().__init__(**kwargs)
        self._limiter = limiter

    def get(self, *args, **kwargs):
        self._limiter.wait()
        return super().get(*args, **kwargs)


def warm_one(name: str) -> list[tuple[str, str | None]]:
    outcomes: list[tuple[str, str | None]] = []
    for label, fetch, unavailable in FETCHERS:
        try:
            fetch(name)
        except unavailable as exc:
            outcomes.append((label, str(exc)))
        else:
            outcomes.append((label, None))
    return outcomes


def combination_product_names(registry: Registry) -> list[str]:
    names = []
    for product in products.PRODUCTS.values():
        ids = {
            d.substance_id
            for d in products.decompose(product, registry)
            if d.substance_id
        }
        if len(ids) > 1:
            names.append(product.brand_name)
    return names


def warm_product(name: str) -> list[tuple[str, str | None]]:
    label = "combination product labels"
    try:
        food_guidance.fetch_labels_for_product(name)
    except food_guidance.OpenFDAUnavailable as exc:
        return [(label, str(exc))]
    return [(label, None)]


def warm_task(key: str) -> list[tuple[str, str | None]]:
    kind, _, name = key.partition(":")
    return warm_product(name) if kind == "product" else warm_one(name)


def warm(
    names: list[str], product_names: list[str] | None = None
) -> dict[str, list[tuple[str, str | None]] | Exception]:
    keys = [f"substance:{n}" for n in names]
    keys += [f"product:{n}" for n in product_names or []]
    return label_cache.fetch_concurrently(keys, warm_task)


def failures(results: dict) -> list[str]:
    lines = []
    for name, outcome in results.items():
        if isinstance(outcome, Exception):
            lines.append(f"{name}: {outcome!r}")
            continue
        for label, error in outcome:
            if error is not None:
                lines.append(f"{name} ({label}): {error}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch the openFDA labels used by food guidance, the side-effect "
            "profile and the interaction checker for every registry substance, "
            "and store them in the on-disk label cache (7-day TTL)."
        )
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help="minimum seconds between openFDA requests (public limit: 40/minute)",
    )
    args = parser.parse_args(argv)

    root = label_cache.cache_dir()
    if root is None:
        print(f"The label cache is disabled ({label_cache.ENV_VAR} is empty).")
        return 2

    limiter = Limiter(args.interval)
    for module in (food_guidance, side_effects, interactions):
        module._http = RateLimitedClient(limiter, timeout=5.0)

    registry = load_registry()
    names = [entry["name"] for entry in registry.all()]
    product_names = combination_product_names(registry)
    total = len(names) * len(FETCHERS) + len(product_names)
    print(
        f"Warming {len(names)} substances and {len(product_names)} combination "
        f"products into {root} ..."
    )
    started = time.monotonic()
    results = warm(names, product_names)
    problems = failures(results)

    print(
        f"Done in {time.monotonic() - started:.1f}s: "
        f"{total - len(problems)} of {total} lookups cached."
    )
    for line in problems:
        print(f"  not cached: {line}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
