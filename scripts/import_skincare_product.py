"""Importer for curated skincare products.

Fetches each product's manufacturer page, extracts its ingredient list
verbatim, and appends a new entry to app/data/skincare_products.json. Never
invents, completes, or "fixes" an ingredient list: a page that can't be
fetched or doesn't show a clear, unambiguous ingredient section is skipped
and reported, never guessed. Never sets verified: true -- that flag is set
by a human after reading the entry against the source page.

Batch mode (fetches each URL):
    python scripts/import_skincare_product.py --input products.json

products.json is a JSON list of {"brand", "name", "category", "url"}.

Manual mode, for a page that can't be fetched (JS-rendered, blocked, etc.)
but whose ingredient list was pasted into a text file by hand:
    python scripts/import_skincare_product.py --from-text list.txt \\
        --brand BRAND --name NAME --category CATEGORY --url URL

Both modes: an existing brand+name entry is never overwritten; a category
outside RECOGNIZED_CATEGORIES is reported, not guessed at.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._skincare_common import (  # noqa: E402
    MIN_INGREDIENT_COUNT,
    HostRateLimiter,
    extract_ingredients,
    fetch_page,
    normalize_line_endings,
    sha256_hex,
    split_ingredients,
)

DATA_PATH = (
    Path(__file__).resolve().parent.parent / "app" / "data" / "skincare_products.json"
)

# app/api/alternatives.py treats category as freeform text (see
# client/src/types/contract.ts: "category is freeform text, not an enum the
# server enforces"), but app/data/skincare_products.json's own _README names
# these four as the working set. Report anything else rather than invent a
# broader mapping.
RECOGNIZED_CATEGORIES = frozenset({"cleanser", "moisturizer", "serum", "sunscreen"})


@dataclass
class ImportOutcome:
    brand: str
    name: str
    status: str  # "added" | "skipped" | "exists"
    reason: str


def load_data(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_data(path: Path, data: dict) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    path.write_text(normalize_line_endings(text), encoding="utf-8", newline="\n")


def _key(brand: str, name: str) -> tuple[str, str]:
    return (brand.strip().lower(), name.strip().lower())


def _existing_index(data: dict) -> dict[tuple[str, str], dict]:
    return {
        _key(e["brand"], e["name"]): e
        for e in data.get("products", [])
        if e.get("brand") and e.get("name")
    }


def build_entry(
    brand: str,
    name: str,
    category: str,
    url: str,
    ingredients: list[str],
    raw_text: str,
    entry_method: str,
) -> dict:
    return {
        "brand": brand,
        "name": name,
        "category": category,
        "ingredients": ingredients,
        "source_url": url,
        "date_checked": date.today().isoformat(),
        "verified": False,
        "raw_ingredient_text": raw_text,
        "page_text_hash": sha256_hex(raw_text),
        "entry_method": entry_method,
    }


def import_one(product: dict, data: dict, limiter: HostRateLimiter) -> ImportOutcome:
    brand, name = product["brand"], product["name"]
    category, url = product["category"], product["url"]

    if category not in RECOGNIZED_CATEGORIES:
        return ImportOutcome(
            brand, name, "skipped", f"unrecognized category {category!r}"
        )

    existing = _existing_index(data).get(_key(brand, name))

    fetch_result = fetch_page(url, limiter)
    if fetch_result.text is None:
        if existing is not None:
            return ImportOutcome(
                brand,
                name,
                "exists",
                f"already present; could not re-fetch now ({fetch_result.reason})",
            )
        return ImportOutcome(
            brand, name, "skipped", f"unfetchable: {fetch_result.reason}"
        )

    extraction = extract_ingredients(fetch_result.text)
    if extraction.raw_text is None:
        if existing is not None:
            return ImportOutcome(
                brand,
                name,
                "exists",
                f"already present; re-fetch could not confirm ({extraction.reason})",
            )
        return ImportOutcome(brand, name, "skipped", extraction.reason or "unknown")

    if existing is not None:
        matches = list(existing.get("ingredients", [])) == extraction.ingredients
        verdict = "matches stored entry" if matches else "DIFFERS from stored entry"
        return ImportOutcome(
            brand, name, "exists", f"already present; fetched list {verdict}"
        )

    entry = build_entry(
        brand,
        name,
        category,
        url,
        extraction.ingredients,
        extraction.raw_text,
        "fetched",
    )
    data.setdefault("products", []).append(entry)
    return ImportOutcome(brand, name, "added", "ok")


def import_from_text(
    text_path: Path, brand: str, name: str, category: str, url: str, data: dict
) -> ImportOutcome:
    if category not in RECOGNIZED_CATEGORIES:
        return ImportOutcome(
            brand, name, "skipped", f"unrecognized category {category!r}"
        )

    existing = _existing_index(data).get(_key(brand, name))
    if existing is not None:
        return ImportOutcome(
            brand, name, "exists", "already present; manual entry not re-imported"
        )

    raw_text = normalize_line_endings(text_path.read_text(encoding="utf-8")).strip()
    ingredients = split_ingredients(raw_text)
    if len(ingredients) < MIN_INGREDIENT_COUNT:
        return ImportOutcome(
            brand,
            name,
            "skipped",
            f"partial capture: only {len(ingredients)} ingredient(s) found",
        )

    entry = build_entry(brand, name, category, url, ingredients, raw_text, "manual")
    data.setdefault("products", []).append(entry)
    return ImportOutcome(brand, name, "added", "ok")


def _print_summary(outcomes: list[ImportOutcome]) -> None:
    added = [o for o in outcomes if o.status == "added"]
    skipped = [o for o in outcomes if o.status == "skipped"]
    exists = [o for o in outcomes if o.status == "exists"]

    print(f"\nAdded: {len(added)}")
    for o in added:
        print(f"  + {o.brand} | {o.name}")

    print(f"\nSkipped: {len(skipped)}")
    for o in skipped:
        print(f"  - {o.brand} | {o.name}: {o.reason}")

    print(f"\nAlready present: {len(exists)}")
    for o in exists:
        print(f"  = {o.brand} | {o.name}: {o.reason}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--input", type=Path, help="JSON file: list of {brand, name, category, url}"
    )
    parser.add_argument(
        "--from-text", type=Path, help="manual mode: pasted ingredient list"
    )
    parser.add_argument("--brand")
    parser.add_argument("--name")
    parser.add_argument("--category")
    parser.add_argument("--url")
    parser.add_argument("--data", type=Path, default=DATA_PATH)
    args = parser.parse_args(argv)

    data = load_data(args.data)
    outcomes: list[ImportOutcome] = []

    if args.from_text:
        missing = [
            f for f in ("brand", "name", "category", "url") if not getattr(args, f)
        ]
        if missing:
            parser.error("--from-text requires --" + ", --".join(missing))
        outcomes.append(
            import_from_text(
                args.from_text, args.brand, args.name, args.category, args.url, data
            )
        )
    elif args.input:
        products = json.loads(args.input.read_text(encoding="utf-8"))
        limiter = HostRateLimiter()
        for product in products:
            outcomes.append(import_one(product, data, limiter))
    else:
        parser.error(
            "pass --input FILE or --from-text FILE --brand ... --name ... "
            "--category ... --url ..."
        )

    if any(o.status == "added" for o in outcomes):
        save_data(args.data, data)

    _print_summary(outcomes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
