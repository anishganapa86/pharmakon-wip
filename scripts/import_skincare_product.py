"""Importer for curated skincare products.

Fetches each product's manufacturer page, extracts its ingredient list
verbatim, and appends a new entry to app/data/skincare_products.json. Never
invents, completes, or "fixes" an ingredient list: a page that can't be
fetched or doesn't show a clear, unambiguous ingredient section is skipped
and reported, never guessed. Never sets verified: true -- that flag is set
by a human after reading the entry against the source page.

Batch mode (fetches each URL; source_type defaults to "brand" unless the
input JSON gives its own):
    python scripts/import_skincare_product.py --input products.json

products.json is a JSON list of {"brand", "name", "category", "url"}.

Manual mode, for a page that can't be fetched (JS-rendered, blocked, etc.)
or an ingredient list checked against the physical product:
    python scripts/import_skincare_product.py --from-text list.txt \\
        --brand BRAND --name NAME --category CATEGORY \\
        --source-type {brand,retailer,packaging} \\
        [--url SOURCE_URL] [--reference-url URL] [--review-notes TEXT]

--url (source_url) is required when --source-type is "brand" or
"retailer" -- that's the fetchable evidence. "packaging" entries (checked
against the product itself) may omit it; --reference-url is then a
convenience link only, never evidence, and may be omitted too.

Both modes: an existing brand+name entry is never overwritten; a category
outside RECOGNIZED_CATEGORIES or a source_type outside
RECOGNIZED_SOURCE_TYPES is reported, not guessed at.
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
    RECOGNIZED_CATEGORIES,
    RECOGNIZED_SOURCE_TYPES,
    HostRateLimiter,
    extract_ingredients,
    fetch_page,
    normalize_line_endings,
    sha256_hex,
    split_ingredients_detailed,
)

DATA_PATH = (
    Path(__file__).resolve().parent.parent / "app" / "data" / "skincare_products.json"
)


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
    source_type: str,
    source_url: str | None,
    reference_url: str | None,
    ingredients: list[str],
    raw_text: str,
    entry_method: str,
    review_notes: str | None = None,
) -> dict:
    return {
        "brand": brand,
        "name": name,
        "category": category,
        "ingredients": ingredients,
        "source_type": source_type,
        "source_url": source_url,
        "reference_url": reference_url,
        "date_checked": date.today().isoformat(),
        "verified": False,
        "raw_ingredient_text": raw_text,
        "page_text_hash": sha256_hex(raw_text),
        "entry_method": entry_method,
        "review_notes": review_notes,
    }


def import_one(product: dict, data: dict, limiter: HostRateLimiter) -> ImportOutcome:
    brand, name = product["brand"], product["name"]
    category, url = product["category"], product["url"]
    source_type = product.get("source_type", "brand")
    reference_url = product.get("reference_url")

    if category not in RECOGNIZED_CATEGORIES:
        return ImportOutcome(
            brand, name, "skipped", f"unrecognized category {category!r}"
        )
    if source_type not in RECOGNIZED_SOURCE_TYPES:
        return ImportOutcome(
            brand, name, "skipped", f"unrecognized source_type {source_type!r}"
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
        source_type,
        url,
        reference_url,
        extraction.ingredients,
        extraction.raw_text,
        "fetched",
    )
    data.setdefault("products", []).append(entry)
    return ImportOutcome(brand, name, "added", "ok")


def import_from_text(
    text_path: Path,
    brand: str,
    name: str,
    category: str,
    source_type: str,
    source_url: str | None,
    reference_url: str | None,
    data: dict,
    review_notes: str | None = None,
) -> ImportOutcome:
    if category not in RECOGNIZED_CATEGORIES:
        return ImportOutcome(
            brand, name, "skipped", f"unrecognized category {category!r}"
        )
    if source_type not in RECOGNIZED_SOURCE_TYPES:
        return ImportOutcome(
            brand, name, "skipped", f"unrecognized source_type {source_type!r}"
        )
    if source_type in ("brand", "retailer") and not source_url:
        return ImportOutcome(
            brand,
            name,
            "skipped",
            f"source_type {source_type!r} requires a non-empty source_url",
        )

    existing = _existing_index(data).get(_key(brand, name))
    if existing is not None:
        return ImportOutcome(
            brand, name, "exists", "already present; manual entry not re-imported"
        )

    raw_text = normalize_line_endings(text_path.read_text(encoding="utf-8")).strip()
    split_result = split_ingredients_detailed(raw_text)
    ingredients = split_result.ingredients
    if len(ingredients) < MIN_INGREDIENT_COUNT:
        return ImportOutcome(
            brand,
            name,
            "skipped",
            f"partial capture: only {len(ingredients)} ingredient(s) found",
        )

    entry = build_entry(
        brand,
        name,
        category,
        source_type,
        source_url,
        reference_url,
        ingredients,
        raw_text,
        "manual",
        review_notes,
    )
    data.setdefault("products", []).append(entry)

    reason = "ok"
    if split_result.excluded_trailing_code:
        reason = (
            "ok (excluded trailing batch code from ingredients array, kept in "
            f"raw_ingredient_text: {split_result.excluded_trailing_code!r})"
        )
    return ImportOutcome(brand, name, "added", reason)


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
    parser.add_argument("--url", help="source_url")
    parser.add_argument("--source-type", choices=sorted(RECOGNIZED_SOURCE_TYPES))
    parser.add_argument("--reference-url")
    parser.add_argument("--review-notes")
    parser.add_argument("--data", type=Path, default=DATA_PATH)
    args = parser.parse_args(argv)

    data = load_data(args.data)
    outcomes: list[ImportOutcome] = []

    if args.from_text:
        missing = [
            f
            for f in ("brand", "name", "category", "source_type")
            if not getattr(args, f)
        ]
        if missing:
            flags = ", --".join(f.replace("_", "-") for f in missing)
            parser.error(f"--from-text requires --{flags}")
        outcomes.append(
            import_from_text(
                args.from_text,
                args.brand,
                args.name,
                args.category,
                args.source_type,
                args.url,
                args.reference_url,
                data,
                args.review_notes,
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
