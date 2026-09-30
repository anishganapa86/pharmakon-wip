"""Verifier for app/data/skincare_products.json.

Re-fetches every entry's source_url, re-extracts the ingredient text with
the same logic the importer uses, and compares it to what's stored. Never
modifies the data file.

    python scripts/verify_skincare_products.py [--data FILE] [--out FILE]

Writes a report to reports/skincare_verification.txt and exits nonzero if
any entry is MISMATCH. MATCH only means the stored list equals today's
page -- it says nothing about whether the page itself is correct.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._skincare_common import (  # noqa: E402
    HostRateLimiter,
    extract_ingredients,
    fetch_page,
)

DATA_PATH = (
    Path(__file__).resolve().parent.parent / "app" / "data" / "skincare_products.json"
)
REPORT_PATH = (
    Path(__file__).resolve().parent.parent / "reports" / "skincare_verification.txt"
)

REPORT_HEADER = (
    "MATCH means the stored list equals the source page as of today. It "
    "does not mean the page is correct. verified stays false until a human "
    "sets it."
)


def _is_placeholder(entry: dict) -> bool:
    return not entry.get("ingredients") and entry.get("source_url") in (
        None,
        "",
        "TODO",
    )


@dataclass
class VerifyResult:
    brand: str
    name: str
    status: str  # "MATCH" | "MISMATCH" | "UNFETCHABLE" | "SKIPPED"
    detail: str


def diff_ingredients(stored: list[str], fresh: list[str]) -> str:
    removed = [i for i in stored if i not in fresh]
    added = [i for i in fresh if i not in stored]
    lines = []
    if added:
        lines.append("added:   " + ", ".join(added))
    if removed:
        lines.append("removed: " + ", ".join(removed))
    if not added and not removed and stored != fresh:
        lines.append("reordered: " + " -> ".join(fresh))
    return "\n           ".join(lines)


def verify_entry(entry: dict, limiter: HostRateLimiter) -> VerifyResult:
    brand, name = entry.get("brand", "?"), entry.get("name", "?")
    url = entry.get("source_url")
    is_manual = entry.get("entry_method") == "manual"

    fetch_result = fetch_page(url, limiter)
    if fetch_result.text is None:
        status = "SKIPPED" if is_manual else "UNFETCHABLE"
        detail = (
            f"manual entry, not fetchable now ({fetch_result.reason})"
            if is_manual
            else fetch_result.reason
        )
        return VerifyResult(brand, name, status, detail or "")

    extraction = extract_ingredients(fetch_result.text)
    if extraction.raw_text is None:
        status = "SKIPPED" if is_manual else "UNFETCHABLE"
        detail = (
            f"manual entry, ingredient section not confirmable ({extraction.reason})"
            if is_manual
            else extraction.reason
        )
        return VerifyResult(brand, name, status, detail or "")

    stored = list(entry.get("ingredients", []))
    if stored == extraction.ingredients:
        return VerifyResult(brand, name, "MATCH", "")
    return VerifyResult(
        brand, name, "MISMATCH", diff_ingredients(stored, extraction.ingredients)
    )


def render_report(results: list[VerifyResult]) -> str:
    lines = [REPORT_HEADER, ""]
    for r in results:
        lines.append(f"{r.status:<10} {r.brand} | {r.name}")
        if r.detail:
            lines.append(f"           {r.detail}")

    counts = {
        status: sum(r.status == status for r in results)
        for status in ("MATCH", "MISMATCH", "UNFETCHABLE", "SKIPPED")
    }
    lines.append("")
    lines.append(
        f"{len(results)} checked: {counts['MATCH']} match, "
        f"{counts['MISMATCH']} mismatch, {counts['UNFETCHABLE']} unfetchable, "
        f"{counts['SKIPPED']} skipped"
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DATA_PATH)
    parser.add_argument("--out", type=Path, default=REPORT_PATH)
    args = parser.parse_args(argv)

    data = json.loads(args.data.read_text(encoding="utf-8"))
    limiter = HostRateLimiter()

    results = [
        verify_entry(entry, limiter)
        for entry in data.get("products", [])
        if not _is_placeholder(entry)
    ]

    report = render_report(results)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8", newline="\n")

    print(report)
    print(f"Full report written to {args.out}")

    return 1 if any(r.status == "MISMATCH" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
