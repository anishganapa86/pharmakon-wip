from __future__ import annotations

import re

from ..registry.loader import normalize, strip_salt_tokens
from .names import display_product_name

DAILYMED_URL = "https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid={set_id}"

NOT_EXACT_PRODUCT = "Not necessarily the exact product you take."


def dailymed_url(set_id: str | None) -> str | None:
    return DAILYMED_URL.format(set_id=set_id) if set_id else None


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def attribution(product_name: str | None, active_ingredient: str | None) -> str | None:
    if not active_ingredient:
        return None
    article = _article(active_ingredient)
    if product_name:
        return (
            f"From the label for {product_name}, {article} {active_ingredient} "
            f"product. {NOT_EXACT_PRODUCT}"
        )
    return (
        f"From a label for {article} {active_ingredient} product. {NOT_EXACT_PRODUCT}"
    )


def sections_from_results(
    results: list[dict], field: str, active_ingredient: str
) -> list[dict]:
    out = []
    for label in results:
        brands = (label.get("openfda") or {}).get("brand_name") or []
        for text in label.get(field) or []:
            out.append(
                {
                    "product_name": display_product_name(brands[0]) if brands else None,
                    "active_ingredient": active_ingredient,
                    "set_id": label.get("set_id"),
                    "text": text,
                }
            )
    return out


def sections_text(sections: list[dict]) -> str | None:
    return "\n".join(s["text"] for s in sections) or None


def find_section(sections: list[dict], sentence: str) -> dict | None:
    for section in sections:
        if sentence in section["text"]:
            return section
    return None


def source_fields(section: dict | None) -> dict:
    if section is None:
        return {
            "label_product_name": None,
            "label_attribution": None,
            "dailymed_url": None,
        }
    return {
        "label_product_name": section.get("product_name"),
        "label_attribution": attribution(
            section.get("product_name"), section.get("active_ingredient")
        ),
        "dailymed_url": dailymed_url(section.get("set_id")),
    }


FALLBACK_LIMIT = 25


def fallback_search(field: str, name: str, exists_field: str | None = None) -> str:
    search = f'openfda.{field}:"{name.upper()}"'
    if exists_field:
        search += f" AND _exists_:{exists_field}"
    return search


DIFFERENT_DRUG_SALTS = {"caffeine": frozenset({"citrate"})}


def _canonical_generic(name: str) -> str:
    return strip_salt_tokens(normalize(name))


def same_drug_name(label_name: str, name: str) -> bool:
    base = _canonical_generic(label_name)
    if base != _canonical_generic(name):
        return False
    distinct = DIFFERENT_DRUG_SALTS.get(base, frozenset())
    return set(normalize(label_name).split()) & distinct == (
        set(normalize(name).split()) & distinct
    )


def is_oral(label: dict) -> bool:
    return "ORAL" in ((label.get("openfda") or {}).get("route") or [])


def _names_the_drug(label: dict, name: str) -> bool:
    names = (label.get("openfda") or {}).get("generic_name") or []
    return not names or any(same_drug_name(n, name) for n in names)


def oral_labels(labels: list[dict], name: str) -> list[dict]:
    return [
        label for label in labels if is_oral(label) and _names_the_drug(label, name)
    ]


def fallback_matches(field: str, label: dict, name: str) -> bool:
    names = (label.get("openfda") or {}).get(field) or []
    if field == "generic_name":
        return len(names) == 1 and same_drug_name(names[0], name)
    pattern = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
    return any(pattern.search(n) for n in names)
