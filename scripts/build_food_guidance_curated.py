"""Builds app/data/food_drug_guidance.json from live openFDA labels.

    python scripts/build_food_guidance_curated.py [--data FILE]
    python scripts/build_food_guidance_curated.py --audit [--data FILE]

Default mode: for each (substance, item) pair in PAIRS, resolves the
substance against the registry, fetches its own oral label, and extracts a
qualifying sentence with the exact logic app/api/food_guidance.py uses for
the live path (same item-term + instruction-cue rule, same field priority,
same quote-integrity rules -- true sentence start/end, no heading or
cross-reference noise, list items carry their lead-in). Never writes an
entry from its own knowledge -- if no qualifying sentence passes (or the
label isn't oral, or openFDA can't be reached), no entry is created, and
the pair is reported skipped with why. An existing (substance, item) entry
is never overwritten.

--audit mode: re-fetches each EXISTING entry's own label by its stored
set_id (not by a fresh name search -- a registry name doesn't always match
openFDA's own generic_name string, e.g. a salt form) and checks whether its
stored quote still appears among today's quote-integrity-passing matches.
An entry that no longer passes is removed and reported with why; a
surviving entry has its product_name/dosage_form backfilled from the
re-fetched label. Never edits a quote in place -- a removed entry can only
come back via a normal (non-audit) run, from a fresh qualifying match.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api import food_guidance  # noqa: E402
from app.registry.loader import load_registry  # noqa: E402

DATA_PATH = food_guidance.DATA_PATH

# (registry substance id, item term) -- item terms match food_guidance.ITEM_TERMS.
PAIRS: list[tuple[str, str]] = [
    ("ibuprofen", "food"),
    ("naproxen", "food"),
    ("aspirin", "food"),
    ("acetaminophen", "alcohol"),
    ("metformin", "food"),
    ("metformin", "alcohol"),
    ("atorvastatin", "grapefruit"),
    ("warfarin", "vitamin k"),
    ("sertraline", "alcohol"),
    ("diphenhydramine", "alcohol"),
    ("aspirin", "alcohol"),
    ("ibuprofen", "alcohol"),
    ("naproxen", "alcohol"),
]


def load_data(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_data(path: Path, data: dict) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    path.write_text(text, encoding="utf-8", newline="\n")


def _existing_keys(data: dict) -> set[tuple[str, str]]:
    return {(e["substance"], e["item"]) for e in data.get("entries", [])}


def build_entry(
    substance_id: str, item: str, generic_name: str
) -> tuple[dict | None, str]:
    """(entry, reason). entry is None when nothing qualified; reason always
    explains the outcome, success included."""
    try:
        label = food_guidance.fetch_label_for_substance(generic_name)
    except food_guidance.OpenFDAUnavailable as exc:
        return None, f"openFDA unreachable: {exc}"
    if label is None:
        return None, "no label found"
    route = food_guidance.label_route(label)
    if route is None:
        return None, "route could not be determined from label"
    if not food_guidance.is_oral(label):
        return None, f"label route is {route}, not oral"
    matches = food_guidance.qualifying_matches(label, item)
    if not matches:
        return None, "no qualifying sentence found"
    match = matches[0]
    entry = {
        "substance": substance_id,
        "item": item,
        "quote": match.quote,
        "label_field": match.field,
        "set_id": label.get("set_id"),
        "product_name": food_guidance.label_product_name(label),
        "dosage_form": food_guidance.label_dosage_form(label),
        "date_fetched": date.today().isoformat(),
        "verified": False,
    }
    return entry, "ok"


def audit_entry(entry: dict) -> tuple[bool, str, dict | None]:
    """(kept, reason, updated_entry_or_None). Re-fetches the entry's own
    cited label (by set_id, never a fresh name search) and checks whether
    its stored quote still passes the quote-integrity rules today. A
    kept entry's returned dict has product_name/dosage_form backfilled from
    the re-fetched label; the quote text itself is never changed."""
    set_id = entry.get("set_id")
    if not set_id:
        return False, "no set_id to re-verify against", None
    try:
        label = food_guidance.fetch_label_by_set_id(set_id)
    except food_guidance.OpenFDAUnavailable as exc:
        return True, f"openFDA unreachable, kept without re-validation: {exc}", None
    if label is None:
        return False, "label no longer found by its set_id", None
    matches = food_guidance.qualifying_matches(label, entry["item"])
    stored = food_guidance._normalize_whitespace(entry["quote"])
    if not any(food_guidance._normalize_whitespace(m.quote) == stored for m in matches):
        return False, "quote no longer passes the quote-integrity rules", None
    updated = dict(entry)
    updated["product_name"] = food_guidance.label_product_name(label)
    updated["dosage_form"] = food_guidance.label_dosage_form(label)
    return True, "ok", updated


def audit(args: argparse.Namespace) -> int:
    data = load_data(args.data)
    entries = data.get("entries", [])

    kept: list[dict] = []
    removed: list[tuple[dict, str]] = []
    for entry in entries:
        ok, reason, updated = audit_entry(entry)
        if ok:
            kept.append(updated if updated is not None else entry)
        else:
            removed.append((entry, reason))

    data["entries"] = kept
    save_data(args.data, data)

    print(f"Audited: {len(entries)}")
    print(f"Kept: {len(kept)}")
    print(f"Removed: {len(removed)}")
    for e, reason in removed:
        print(f"  - {e['substance']} / {e['item']}: {reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DATA_PATH)
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Re-verify existing entries against today's quote-integrity rules "
        "instead of adding new ones.",
    )
    args = parser.parse_args(argv)

    if args.audit:
        return audit(args)

    registry = load_registry()
    data = load_data(args.data)
    existing = _existing_keys(data)

    added: list[tuple[str, str]] = []
    skipped: list[tuple[str, str, str]] = []

    for substance_id, item in PAIRS:
        if (substance_id, item) in existing:
            skipped.append((substance_id, item, "already present"))
            continue
        resolved = registry.resolve(substance_id)
        if resolved is None:
            skipped.append((substance_id, item, "not a recognized registry substance"))
            continue
        generic_name = registry.get(resolved)["name"]
        entry, reason = build_entry(resolved, item, generic_name)
        if entry is None:
            skipped.append((substance_id, item, reason))
            continue
        data.setdefault("entries", []).append(entry)
        added.append((substance_id, item))

    if added:
        save_data(args.data, data)

    print(f"\nAdded: {len(added)}")
    for s, i in added:
        print(f"  + {s} / {i}")
    print(f"\nSkipped: {len(skipped)}")
    for s, i, reason in skipped:
        print(f"  - {s} / {i}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
