from __future__ import annotations

import re

NUMERIC_XREF_RE = re.compile(r"\(\s*\d+(?:\.\d+)?(?:\s*,\s*\d+(?:\.\d+)?)*\s*\)")
SEE_XREF_RE = re.compile(r"[(\[]\s*see\b[^)\]]*[)\]]", re.IGNORECASE)

SECTION_NUMBER_RE = re.compile(r"^\d{1,2}(?:\.\d+)+\s+(?=[A-Z])")
MID_SECTION_NUMBER_RE = re.compile(r"\s\d{1,2}\.\d{1,2}\s+[A-Z][\w-]*\s+[A-Z][\w-]*\s+")

HEADING_MINOR_WORDS = frozenset(
    {"of", "and", "with", "for", "in", "to", "the", "a", "on", "or", "by", "from"}
)
SENTENCE_STARTERS = frozenset(
    {
        "The",
        "A",
        "An",
        "This",
        "These",
        "Concomitant",
        "Coadministration",
        "Co-administration",
        "Patients",
        "When",
        "If",
        "Do",
        "Avoid",
    }
)
MIN_HEADING_TOKENS = 2


def has_cross_reference(text: str) -> bool:
    return bool(NUMERIC_XREF_RE.search(text) or SEE_XREF_RE.search(text))


def has_leading_section_number(text: str) -> bool:
    return bool(SECTION_NUMBER_RE.match(text))


def has_section_number_noise(text: str) -> bool:
    return has_leading_section_number(text) or bool(MID_SECTION_NUMBER_RE.search(text))


def _is_heading_token(token: str) -> bool:
    bare = token.strip("()[],;:")
    if not bare:
        return False
    if bare.lower() in HEADING_MINOR_WORDS:
        return True
    return bare[0].isupper() or bare[0].isdigit()


def separate_section_heading(text: str) -> str | None:
    match = SECTION_NUMBER_RE.match(text)
    if match is None:
        return text
    tokens = text[match.end() :].split()
    for i in range(MIN_HEADING_TOKENS, len(tokens) - 1):
        token = tokens[i]
        following = tokens[i + 1]
        if token not in SENTENCE_STARTERS:
            continue
        if not following[:1].islower() or following.lower() in HEADING_MINOR_WORDS:
            continue
        if not all(_is_heading_token(t) for t in tokens[:i]):
            continue
        return " ".join(tokens[i:])
    return None
