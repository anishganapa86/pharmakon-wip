"""Standalone CLI: DrugBank XML -> registry-format JSON.

    python -m app.registry.import_drugbank drugbank_all_full_database.xml \\
        --out app/registry/substances_drugbank.json

Never run at runtime and never run in the test suite (which uses a small
fixture): download the XML yourself, run this once, review the diff, commit the
JSON. The loader picks the file up if it exists (app/registry/loader.py).

What comes across, and what deliberately doesn't
-------------------------------------------------
DrugBank's pharmacokinetic fields are free text, so each parser below is
conservative: it returns a value only when the text states exactly one
unambiguous number in the right unit, and None otherwise. A range ("2-3
hours"), several conflicting values, or a unit that needs an assumption to
convert (an absolute volume in litres, which would need a body weight) all give
None, and the substance is marked `partially_parameterized`. Nothing is
averaged, defaulted or guessed. DrugBank publishes no absorption rate constant
as a field, so `ka_per_hour` is only ever filled when the absorption text
itself states one, which is rare; expect imported entries to be partial.

Other rules:
  * The curated registry always wins. A drug whose name the existing registry
    already resolves is skipped, and an alias that would resolve to (or
    collide with) an existing name is dropped from the imported drug.
  * Every entry is `verified: false` with source "DrugBank Open Data
    <version>". A human must open the cited source before that changes.
  * No thresholds, side effects or clearance route: DrugBank doesn't give a
    validated 24 h limit, and inventing one is exactly what the registry
    forbids. `clearance_route` is "unknown", which the engine treats as no
    renal adjustment. `kinetics` is "first_order" because the registry needs a
    value, flagged `kinetics_assumed` so the curve says so.
  * Only top-level <drug> elements are read; <drug> elements nested inside
    pathways and interactions are not separate entries.

DrugBank's licence terms (https://go.drugbank.com/releases/latest#open-data)
apply to the data this produces. Read them before committing the output.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .loader import REGISTRY_PATH, Registry, normalize, strip_salt_tokens

DEFAULT_OUT = Path(__file__).with_name("substances_drugbank.json")

_NUM = r"\d+(?:\.\d+)?"
_RANGE = r"(?:\s*(?:-|–|—|to)\s*" + _NUM + ")?"

_HOURS_PER_UNIT = {
    "min": 1 / 60,
    "h": 1.0,
    "d": 24.0,
    "w": 24.0 * 7,
}
_HALF_LIFE = re.compile(
    rf"({_NUM})({_RANGE})\s*(minutes?|mins?|hours?|hrs?|h|days?|d|weeks?|wks?)\b",
    re.IGNORECASE,
)
_VD_PER_KG = re.compile(
    rf"({_NUM})({_RANGE})\s*L\s*(?:/|\*|·|\.)\s*kg\b(?:\s*\^?-1)?",
    re.IGNORECASE,
)
_PERCENT = re.compile(rf"({_NUM})({_RANGE})\s*%")
_KA = re.compile(
    rf"(?:absorption rate constant|\bka\b)[^0-9.]{{0,40}}({_NUM})\s*"
    r"(?:h\^?-1|hr\^?-1|hour\^?-1|/\s*h(?:r|our)?\b|per hour|1\s*/\s*h)",
    re.IGNORECASE,
)

_ORAL = re.compile(r"\b(oral|orally|by mouth|tablets?|capsules?)\b", re.IGNORECASE)
_OTHER_ROUTE = re.compile(
    r"\b(intravenous(?:ly)?|iv|intramuscular(?:ly)?|im|subcutaneous(?:ly)?|"
    r"rectal(?:ly)?|intranasal(?:ly)?|nasal|topical(?:ly)?|transdermal|"
    r"sublingual(?:ly)?|buccal|inhal\w*|ophthalmic|vaginal(?:ly)?)\b",
    re.IGNORECASE,
)


# --- Free-text parsers: a value only when the text is unambiguous ---------------


def parse_half_life_hours(text: str | None) -> float | None:
    """Hours, from exactly one plain "<n> <unit>" in the text, else None."""
    if not text:
        return None
    found = _HALF_LIFE.findall(text)
    if len(found) != 1:
        return None
    number, span, unit = found[0]
    if span:  # a range
        return None
    unit = unit.lower()
    key = (
        "min"
        if unit.startswith("min")
        else "h"
        if unit.startswith("h")
        else "d"
        if unit.startswith("d")
        else "w"
    )
    hours = float(number) * _HOURS_PER_UNIT[key]
    return hours if hours > 0 else None


def parse_vd_l_per_kg(text: str | None) -> float | None:
    """L/kg, only when stated in L/kg. An absolute volume (L) needs a body
    weight to convert, so it is left null rather than assuming one."""
    if not text:
        return None
    found = _VD_PER_KG.findall(text)
    if len(found) != 1 or found[0][1]:
        return None
    value = float(found[0][0])
    return value if value > 0 else None


def parse_oral_bioavailability(text: str | None) -> float | None:
    """A fraction 0..1: the oral value, or an unqualified one, else None.

    Sentences are judged one at a time. A sentence with several percentages, a
    range, or a non-oral route without an oral mention is ignored. Two
    different oral values (say, fed and fasted) give None.
    """
    if not text:
        return None
    oral: set[float] = set()
    unqualified: set[float] = set()
    for sentence in re.split(r"(?<=[.;])\s+", text):
        if "bioavailab" not in sentence.lower():
            continue
        found = _PERCENT.findall(sentence)
        if len(found) != 1 or found[0][1]:
            continue
        fraction = float(found[0][0]) / 100.0
        if not 0.0 < fraction <= 1.0:
            continue
        if _ORAL.search(sentence):
            oral.add(fraction)
        elif not _OTHER_ROUTE.search(sentence):
            unqualified.add(fraction)
    for candidates in (oral, unqualified):
        if candidates:
            return next(iter(candidates)) if len(candidates) == 1 else None
    return None


def parse_ka_per_hour(text: str | None) -> float | None:
    """1/h, only when the text states an absorption rate constant."""
    if not text:
        return None
    found = _KA.findall(text)
    if len(found) != 1:
        return None
    value = float(found[0])
    return value if value > 0 else None


# --- XML ------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(el: ET.Element) -> dict[str, ET.Element]:
    return {_local(c.tag): c for c in el}


def _text(el: ET.Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    return " ".join(el.text.split()) or None


def read_drugs(path: Path) -> tuple[str | None, list[dict]]:
    """(DrugBank version, one raw dict per top-level <drug>).

    Streams the file, so a full multi-hundred-MB export doesn't have to fit in
    memory as a tree. <drug> also appears nested (pathways, interactions); a
    top-level drug is one whose parent is the root element.
    """
    version = None
    drugs: list[dict] = []
    depth = 0
    for event, el in ET.iterparse(path, events=("start", "end")):
        if event == "start":
            depth += 1
            if depth == 1:
                version = el.get("version")
            continue
        depth -= 1
        if depth != 1 or _local(el.tag) != "drug":
            continue
        kids = _children(el)
        drugbank_id = next(
            (
                c.text
                for c in el
                if _local(c.tag) == "drugbank-id" and c.get("primary") == "true"
            ),
            None,
        )
        synonyms = [
            _text(s)
            for s in (kids["synonyms"] if "synonyms" in kids else [])
            if (s.get("language") or "english").lower() == "english"
        ]
        drugs.append(
            {
                "drugbank_id": drugbank_id,
                "name": _text(kids.get("name")),
                "synonyms": [s for s in synonyms if s],
                "half_life": _text(kids.get("half-life")),
                "volume_of_distribution": _text(kids.get("volume-of-distribution")),
                "absorption": _text(kids.get("absorption")),
            }
        )
        el.clear()
    return version, drugs


# --- Mapping to the registry format ---------------------------------------------


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _keys(name: str) -> set[str]:
    """Every form of a name the registry would match it on."""
    n = normalize(name)
    return {k for k in (n, strip_salt_tokens(n)) if k}


@dataclass
class ImportResult:
    version: str | None
    entries: list[dict] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)  # names
    skipped_duplicate: list[str] = field(default_factory=list)
    skipped_nameless: int = 0

    @property
    def counts(self) -> dict[str, int]:
        full = sum(1 for e in self.entries if not e["partially_parameterized"])
        return {
            "imported": len(self.entries),
            "fully_parameterized": full,
            "partially_parameterized": len(self.entries) - full,
            "skipped_existing": len(self.skipped_existing),
            "skipped_duplicate": len(self.skipped_duplicate),
            "skipped_nameless": self.skipped_nameless,
        }


def build_entries(
    version: str | None, drugs: list[dict], existing: Registry
) -> ImportResult:
    label = f"DrugBank Open Data {version or 'unknown version'}"
    result = ImportResult(version)
    claimed: set[str] = set()

    def taken(name: str) -> bool:
        return existing.resolve(name) is not None or bool(_keys(name) & claimed)

    for drug in drugs:
        name = drug["name"]
        slug = slugify(name) if name else ""
        if not slug:
            result.skipped_nameless += 1
            continue
        if existing.resolve(name) is not None or existing.resolve(slug) is not None:
            result.skipped_existing.append(name)  # curated values take precedence
            continue
        if _keys(name) & claimed or _keys(slug) & claimed:
            result.skipped_duplicate.append(name)
            continue
        claimed |= _keys(name) | _keys(slug)

        aliases = []
        for alias in drug["synonyms"]:
            if alias and not taken(alias):
                aliases.append(alias)
                claimed |= _keys(alias)

        half_life = parse_half_life_hours(drug["half_life"])
        vd = parse_vd_l_per_kg(drug["volume_of_distribution"])
        bioavailability = parse_oral_bioavailability(drug["absorption"])
        ka = parse_ka_per_hour(drug["absorption"])
        params = (half_life, vd, bioavailability, ka)

        result.entries.append(
            {
                "id": slug,
                "name": name,
                "aliases": aliases,
                "half_life_hours": half_life,
                "vd_l_per_kg": vd,
                "bioavailability": bioavailability,
                "ka_per_hour": ka,
                "clearance_route": "unknown",
                "kinetics": "first_order",
                "kinetics_assumed": True,
                "threshold_24h_mg": None,
                "threshold_single_mg": None,
                "threshold_concentration_mg_per_l": None,
                "common_side_effects": [],
                "description": None,
                "source": label,
                "drugbank_id": drug["drugbank_id"],
                "verified": False,
                "partially_parameterized": any(p is None for p in params),
            }
        )
    result.entries.sort(key=lambda e: e["id"])
    return result


def import_drugbank(
    xml_path: Path, existing: Registry, version: str | None = None
) -> ImportResult:
    file_version, drugs = read_drugs(xml_path)
    return build_entries(version or file_version, drugs, existing)


def to_json(result: ImportResult) -> dict:
    return {
        "_README": [
            f"Generated by app/registry/import_drugbank.py from DrugBank "
            f"{result.version or 'unknown version'}. Do not edit by hand: "
            "re-run the importer.",
            "Every entry is verified=false. A null parameter means DrugBank's "
            "text did not state it unambiguously; it is never estimated.",
            "partially_parameterized=true: at least one of half_life_hours, "
            "vd_l_per_kg, bioavailability, ka_per_hour is null.",
            "Curated entries in substances.json always take precedence.",
        ],
        "substances": result.entries,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("xml", type=Path, help="DrugBank XML export")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument(
        "--registry",
        type=Path,
        default=REGISTRY_PATH,
        help="curated registry to defer to (default: %(default)s)",
    )
    ap.add_argument(
        "--version", help="DrugBank release, if the XML root doesn't carry one"
    )
    args = ap.parse_args(argv)

    curated = Registry(json.loads(args.registry.read_text())["substances"])
    result = import_drugbank(args.xml, curated, args.version)
    if result.version is None:
        print("no DrugBank version in the XML; pass --version", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(to_json(result), indent=2) + "\n")
    for key, n in result.counts.items():
        print(f"{key}: {n}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
