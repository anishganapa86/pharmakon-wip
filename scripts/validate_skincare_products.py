"""Structural validator for app/data/skincare_products.json.

Reports, without modifying the file: duplicate brand+name pairs, empty
ingredient lists, missing date_checked, verified: true with no date_checked,
unrecognized categories or source_types, a "brand"/"retailer" entry with no
source_url, and ingredient strings carrying trailing periods, \\r, other
line-break artifacts, or stray HTML.

source_type distinguishes how an entry was checked: "brand"/"retailer" means
a fetchable page is the evidence, so source_url is required; "packaging"
means it was checked against the physical product, so source_url may be
absent (reference_url, if given, is a convenience link only, never
evidence). An entry with no source_type at all is treated as legacy and
held to the old blanket "needs a source_url" rule, so nothing already in the
file is newly flagged by this distinction.

Known placeholder entries (empty ingredients + source_url "TODO", the seed
rows shipped in the data file) are not reported -- they're an expected,
marked, in-progress state per the file's own _README.

    python scripts/validate_skincare_products.py [--data FILE]

Exits nonzero if any problem is found.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._skincare_common import (  # noqa: E402
    RECOGNIZED_CATEGORIES,
    RECOGNIZED_SOURCE_TYPES,
)

DATA_PATH = (
    Path(__file__).resolve().parent.parent / "app" / "data" / "skincare_products.json"
)

_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")


def _is_placeholder(entry: dict) -> bool:
    return not entry.get("ingredients") and entry.get("source_url") == "TODO"


def _bad_ingredient_strings(ingredients: list[str]) -> list[str]:
    problems = []
    for ing in ingredients:
        if "\r" in ing:
            problems.append(f"{ing!r} contains \\r")
        elif "\n" in ing or "\t" in ing:
            problems.append(f"{ing!r} contains a line break or tab")
        elif _HTML_TAG_RE.search(ing):
            problems.append(f"{ing!r} contains stray HTML")
        elif ing != ing.strip():
            problems.append(f"{ing!r} has leading/trailing whitespace")
        elif ing.rstrip().endswith("."):
            problems.append(f"{ing!r} has a trailing period")
    return problems


def validate(data: dict) -> list[str]:
    problems: list[str] = []
    seen: dict[tuple[str, str], int] = {}

    for idx, entry in enumerate(data.get("products", [])):
        if _is_placeholder(entry):
            continue
        label = f"[{idx}] {entry.get('brand', '?')} | {entry.get('name', '?')}"

        key = (
            str(entry.get("brand", "")).strip().lower(),
            str(entry.get("name", "")).strip().lower(),
        )
        if key in seen:
            problems.append(
                f"{label}: duplicate of entry [{seen[key]}] (same brand+name)"
            )
        else:
            seen[key] = idx

        if not entry.get("ingredients"):
            problems.append(f"{label}: empty ingredient list")
        else:
            for p in _bad_ingredient_strings(entry["ingredients"]):
                problems.append(f"{label}: ingredient {p}")

        source_type = entry.get("source_type")
        if source_type is not None and source_type not in RECOGNIZED_SOURCE_TYPES:
            problems.append(f"{label}: unrecognized source_type {source_type!r}")

        if source_type in ("brand", "retailer"):
            if not entry.get("source_url"):
                problems.append(
                    f"{label}: source_type {source_type!r} requires a "
                    "non-empty source_url"
                )
        elif source_type is None and not entry.get("source_url"):
            # No source_type at all: legacy shape, held to the old blanket
            # rule so nothing already in the file is newly flagged.
            problems.append(f"{label}: missing source_url")

        if not entry.get("date_checked"):
            problems.append(f"{label}: missing date_checked")
        if entry.get("verified") is True and not entry.get("date_checked"):
            problems.append(f"{label}: verified true but no date_checked")

        category = entry.get("category")
        if category not in RECOGNIZED_CATEGORIES:
            problems.append(f"{label}: unrecognized category {category!r}")

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DATA_PATH)
    args = parser.parse_args(argv)

    data = json.loads(args.data.read_text(encoding="utf-8"))
    problems = validate(data)

    if not problems:
        print("No problems found.")
        return 0

    print(f"{len(problems)} problem(s) found:\n")
    for p in problems:
        print(f"  - {p}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
