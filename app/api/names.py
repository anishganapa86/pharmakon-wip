from __future__ import annotations

import re

KEEP_UPPERCASE = {
    "PEG": "PEG",
    "EDTA": "EDTA",
    "NP": "NP",
    "HCL": "HCl",
    "SPF": "SPF",
    "USP": "USP",
    "OTC": "OTC",
    "AM": "AM",
    "PM": "PM",
    "DM": "DM",
    "PE": "PE",
    "ER": "ER",
    "XR": "XR",
    "UV": "UV",
}
KEEP_LOWERCASE_UNITS = {"MG": "mg", "MCG": "mcg", "ML": "mL", "G": "g"}

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z']*")


def _convert(match: re.Match[str]) -> str:
    word = match.group(0)
    upper = word.upper()
    if upper in KEEP_UPPERCASE:
        return KEEP_UPPERCASE[upper]
    if upper in KEEP_LOWERCASE_UNITS:
        return KEEP_LOWERCASE_UNITS[upper]
    return word.capitalize()


def display_product_name(name: str) -> str:
    letters = [c for c in name if c.isalpha()]
    if not letters or any(c.islower() for c in letters):
        return name
    return _WORD_RE.sub(_convert, name)
