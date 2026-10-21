"""Oral medication food, drink, and timing guidance: two traceable, cited
sources, never a model, never an alert.

Like app/api/interactions.py, there are two sources:

  A) Live openFDA label text. For each oral substance (or combination
     product) in the user's regimen, up to LABEL_SELECT_LIMIT of the
     substance's (or product's) own labels are fetched (single-ingredient
     labels preferred) and each is read for whichever of TEXT_FIELDS
     actually exist on it -- openFDA labels don't carry every field, and
     this never assumes one does. A sentence qualifies only when it
     contains one of an item's listed variants (ITEM_VARIANTS; food, milk,
     grapefruit, ...) and an instruction cue (take, avoid, before, hours,
     ...); the matched variant is asserted present in the quote before it's
     ever shown, in code, not just by convention. Label selection can also
     prefer OTC labels (`prefer_otc`), but nothing in the regimen/product
     model currently says whether the user's own product is OTC or Rx, so
     no caller sets it yet -- it's live but unused.

  B) Curated quotes (app/data/food_drug_guidance.json), built by
     scripts/build_food_guidance_curated.py from the same live-label logic
     for a fixed (substance, item) list, never hand-written. verified stays
     false until a human opens the source. An entry with no quote is never
     created, and the loader here rejects one if it somehow existed.

Every quote is verbatim, <=300 chars, never paraphrased into "safe" or
"will cause". A finding is classified (take_with_food, take_with_food_if_upset,
take_on_empty_stomach, avoid_item, separate_from, other) by simple keyword
rules for display, but the classification is never shown as more than a tag
next to its quote.

Oral only: a substance whose own label declares a non-oral route (or no
route at all) produces no guidance and is reported under unchecked_reason
instead of silently guessed at.

Time windows are parsed only from an explicit two-sided statement ("at
least 2 hours before or 4 hours after"); anything vaguer gets the quote
with no window, never an invented one.

This never creates or feeds an app.detect alert -- informational only, the
same boundary app/api/interactions.py keeps.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from ..registry.loader import Registry
from . import label_cache, products, schedule, service, verification
from .db import DoseEvent, ScheduledDose
from .provenance import QUOTE_SOURCE_FDA_LABEL
from .quote_text import (
    has_cross_reference,
    has_section_number_noise,
    separate_section_heading,
)

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "food_drug_guidance.json"

OPENFDA_LABEL_URL = "https://api.fda.gov/drug/label.json"
# Candidates fetched per openFDA query, to select from -- not all of these
# are read; see LABEL_SELECT_LIMIT.
LABEL_SEARCH_LIMIT = 10
# Labels actually read for quotes, per substance/product, after selection.
LABEL_SELECT_LIMIT = 3

# Priority order: also the tie-break when two sentences score equally on
# instruction-cue count (an earlier field's sentence is preferred).
# spl_medguide is the FDA Medication Guide text -- a distinct patient-facing
# field from information_for_patients that many Rx labels carry.
TEXT_FIELDS = (
    "dosage_and_administration",
    "directions",
    "information_for_patients",
    "spl_medguide",
    "warnings",
    "precautions",
    "when_using",
    "do_not_use",
    "ask_doctor",
    "ask_doctor_or_pharmacist",
    "drug_interactions",
)

# Explicit variant lists, not stemming and not fuzzy matching. A sentence
# qualifies for an item only when it literally contains one of that item's
# listed variants (word-boundary matched) -- "meal"/"dairy" are folded in as
# variants of food/milk rather than kept as separate items, since a label
# that says "with the evening meal" is giving food guidance, not a distinct
# instruction. Items with a single obvious surface form just list themselves.
ITEM_VARIANTS: dict[str, tuple[str, ...]] = {
    "food": ("food", "meal", "meals", "eating"),
    "milk": ("milk", "dairy"),
    "alcohol": (
        "alcohol",
        "alcoholic",
        "alcoholic drink",
        "alcoholic drinks",
        "drinks containing alcohol",
    ),
    "grapefruit": ("grapefruit",),
    "juice": ("juice",),
    "caffeine": ("caffeine",),
    "antacid": ("antacid",),
    "empty stomach": ("empty stomach",),
    "vitamin k": ("vitamin k",),
    "supplement": ("supplement",),
    "calcium": ("calcium",),
    "iron": ("iron",),
}

ITEM_TERMS = tuple(ITEM_VARIANTS.keys())

INSTRUCTION_CUES = (
    "take",
    "taken",
    "avoid",
    "do not",
    "should not",
    "before",
    "after",
    "hours",
    "without",
    "separate",
    "with food",
    "with meals",
    "with milk",
    "after eating",
    "on an empty stomach",
    "before meals",
)

EMPTY_MESSAGE = (
    "No food, drink or timing instructions found in our sources for these "
    "medications. This does not mean none exist. Check the label and ask a "
    "pharmacist."
)

ONGOING_NOTE = (
    "The label gives no time window; applies for as long as you take this medication."
)

REGIMEN_LOOKBACK_HOURS = 24.0
TIMELINE_HORIZON_HOURS = 24.0

# Replaced with a mock-transport client in tests.
_http = httpx.Client(timeout=5.0)

# cache key ("substance:<name>" | "product:<name>", lowercased) -> raw
# candidate labels as openFDA returned them (pre-selection), or [] if none
# were found. Not cached on failure (an OpenFDAUnavailable raise happens
# before the cache write), so the next call retries.
_label_cache: dict[str, list[dict]] = {}


class OpenFDAUnavailable(Exception):
    """openFDA could not be reached or answered with an error."""


def clear_cache() -> None:
    _label_cache.clear()


# --- Fetching ------------------------------------------------------------------


KEPT_OPENFDA_FIELDS = ("route", "substance_name", "product_type", "brand_name")


def _trim_label(label: dict) -> dict:
    kept = {
        f: label[f]
        for f in (*TEXT_FIELDS, *_DOSAGE_FORM_FIELDS, "set_id")
        if f in label
    }
    openfda = label.get("openfda") or {}
    kept["openfda"] = {k: openfda[k] for k in KEPT_OPENFDA_FIELDS if k in openfda}
    return kept


def _fetch_candidates_by(field: str, value: str) -> list[dict]:
    return label_cache.cached(
        "food_guidance_labels",
        f"{field}:{value.upper()}",
        lambda: _request_candidates_by(field, value),
    )


def _request_candidates_by(field: str, value: str) -> list[dict]:
    search = f'openfda.{field}.exact:"{value.upper()}"'
    try:
        r = _http.get(
            OPENFDA_LABEL_URL, params={"search": search, "limit": LABEL_SEARCH_LIMIT}
        )
    except httpx.HTTPError as exc:
        raise OpenFDAUnavailable(str(exc)) from exc
    if r.status_code == 404:  # openFDA's "no matches"
        return []
    if r.status_code != 200:
        raise OpenFDAUnavailable(f"openFDA returned {r.status_code}")
    return [_trim_label(label) for label in r.json().get("results", [])]


def _is_single_ingredient(label: dict) -> bool:
    substances = label.get("openfda", {}).get("substance_name") or []
    return len(substances) <= 1


def _is_otc(label: dict) -> bool:
    return "HUMAN OTC DRUG" in (label.get("openfda", {}).get("product_type") or [])


def _select_labels(candidates: list[dict], limit: int, prefer_otc: bool) -> list[dict]:
    """Up to `limit` candidates: single-ingredient labels first, then (only
    when prefer_otc) OTC labels first. openFDA's own result order is the
    tie-break within each group -- sorted() is stable, so nothing is
    re-ranked beyond these two criteria."""

    def _key(label: dict) -> tuple[int, int]:
        return (
            0 if _is_single_ingredient(label) else 1,
            0 if prefer_otc and _is_otc(label) else 1,
        )

    return sorted(candidates, key=_key)[:limit]


def fetch_labels_for_substance(
    generic_name: str, *, limit: int = LABEL_SELECT_LIMIT, prefer_otc: bool = False
) -> list[dict]:
    """Up to `limit` labels for this substance, single-ingredient labels
    preferred (and OTC labels preferred when `prefer_otc` is set -- the
    regimen/product model doesn't carry an OTC/Rx flag yet, so no caller
    passes this True today; see the module docstring)."""
    key = f"substance:{generic_name.strip().lower()}"
    if key not in _label_cache:
        _label_cache[key] = _fetch_candidates_by("generic_name", generic_name)
    return _select_labels(_label_cache[key], limit, prefer_otc)


def fetch_label_for_substance(generic_name: str) -> dict | None:
    labels = fetch_labels_for_substance(generic_name, limit=1)
    return labels[0] if labels else None


def fetch_labels_for_product(
    product_name: str, *, limit: int = LABEL_SELECT_LIMIT, prefer_otc: bool = False
) -> list[dict]:
    """A combination product's own labels, by brand name -- its guidance
    belongs to the whole product, never attributed to one ingredient."""
    key = f"product:{product_name.strip().lower()}"
    if key not in _label_cache:
        _label_cache[key] = _fetch_candidates_by("brand_name", product_name)
    return _select_labels(_label_cache[key], limit, prefer_otc)


def fetch_label_for_product(product_name: str) -> dict | None:
    labels = fetch_labels_for_product(product_name, limit=1)
    return labels[0] if labels else None


def fetch_label_by_set_id(set_id: str) -> dict | None:
    """The exact label a curated entry cites, by its own set_id -- used to
    re-verify a curated quote against the specific label it came from,
    sidestepping any generic-name/alias mismatch a fresh name search could
    hit (e.g. a registry name that doesn't match openFDA's own generic_name
    string for a salt form)."""
    try:
        r = _http.get(
            OPENFDA_LABEL_URL, params={"search": f'set_id:"{set_id}"', "limit": 1}
        )
    except httpx.HTTPError as exc:
        raise OpenFDAUnavailable(str(exc)) from exc
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise OpenFDAUnavailable(f"openFDA returned {r.status_code}")
    results = r.json().get("results", [])
    return results[0] if results else None


def label_route(label: dict) -> str | None:
    routes = label.get("openfda", {}).get("route") or []
    return routes[0] if routes else None


def is_oral(label: dict) -> bool:
    routes = [r.upper() for r in (label.get("openfda", {}).get("route") or [])]
    return "ORAL" in routes


# --- Sentence/list extraction and quote integrity --------------------------------

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

MAX_QUOTE_CHARS = 300
# How far into a plain sentence a ":" may sit and still be read as a short
# audience-qualifier prefix ("Adults:", "Children under 12 years:") rather
# than a second, flattened-together Drug Facts entry ("...product Allergy
# alert: Acetaminophen may cause...").
_LEAD_COLON_MAX_CHARS = 40

# openFDA's plain-text extraction strips virtually all structural markup:
# across naproxen, sertraline, acetaminophen, aspirin, and ibuprofen's own
# labels, the only separator that survives into the text at all is a
# literal "•" bullet character, and only on some fields of some label
# submissions (spl_medguide, sometimes drug_interactions or
# dosage_and_administration) -- never a newline, never a comma or
# semicolon marking list items. OTC Drug-Facts fields (warnings,
# do_not_use, ask_doctor*, directions) carry NO separator of any kind: list
# items are joined with a single space and are otherwise indistinguishable
# from prose ("...must not use with any other drug containing acetaminophen
# (prescription or n..."). So "•" is the only structural signal this
# code can safely treat as "this is a list" -- everything else is read as
# plain sentences, and a list item with no bullet and no colon-terminated
# lead-in right before it is never reconstructed, just left unquotable
# (see qualifying_pointers).
_LIST_BULLET = "•"

# A numbered ALL-CAPS section heading that leaked into extracted text, e.g.
# "2 DOSAGE AND ADMINISTRATION" or "17 PATIENT COUNSELING INFORMATION".
_HEADING_RE = re.compile(r"\b\d{1,2}(?:\.\d+)?\s+[A-Z]{2,}(?:\s+[A-Z]{2,}){1,}")


def _has_heading_or_xref_noise(text: str) -> bool:
    return bool(
        _HEADING_RE.search(text)
        or has_cross_reference(text)
        or has_section_number_noise(text)
    )


def _split_sentences(chunk: str, block_end: str) -> list[tuple[str, bool]]:
    """(sentence, ends_properly) for each `.!?`-delimited piece of `chunk`.
    ends_properly is True when the piece itself ends in terminal
    punctuation, or -- only for whichever piece is the true tail of the
    whole field -- when `block_end` (the field's own trimmed end) ends with
    it. Never True just because a piece happened to be the last one
    produced by this particular split call."""
    pieces = [s.strip() for s in _SENTENCE_SPLIT_RE.split(chunk) if s.strip()]
    out: list[tuple[str, bool]] = []
    for i, s in enumerate(pieces):
        proper = s[-1:] in ".!?"
        if (
            not proper
            and i == len(pieces) - 1
            and len(s) >= 8
            and block_end.endswith(s)
        ):
            proper = True
        out.append((s, proper))
    return out


def _units_from_block(block: str) -> list[tuple[str, bool, bool]]:
    """(text, is_list_item, ends_properly) quotable units from one raw
    field block. List structure is read directly off "•" in the raw
    text, before any flattening -- a list item's assembled text always
    carries its governing lead-in (the immediately preceding clause ending
    in ":", itself starting at a capital letter or digit), joined with
    " • ". A bullet with no valid lead-in right before it produces no
    list-item unit -- its raw text still flows through as plain-sentence
    units via consume(), which will fail the "starts capitalized" rule on
    its own (bullet fragments are grammatical continuations, so they start
    lowercase) rather than being silently reconstructed."""
    units: list[tuple[str, bool, bool]] = []
    block_end = block.rstrip()
    lead_in: str | None = None

    def consume(pieces: list[tuple[str, bool]]) -> None:
        nonlocal lead_in
        if not pieces:
            return
        last_text, _ = pieces[-1]
        if (
            last_text.endswith(":")
            and last_text[:1]
            and (last_text[0].isupper() or last_text[0].isdigit())
        ):
            lead_in = last_text
            units.extend((s, False, proper) for s, proper in pieces[:-1])
        else:
            lead_in = None
            units.extend((s, False, proper) for s, proper in pieces)

    if _LIST_BULLET not in block:
        consume(_split_sentences(block, block_end))
        return units

    parts = block.split(_LIST_BULLET)
    consume(_split_sentences(parts[0], block_end))
    for part in parts[1:]:
        inner = _split_sentences(part, block_end)
        if lead_in is not None and inner:
            item_text, _ = inner[0]
            units.append((f"{lead_in} {_LIST_BULLET} {item_text}", True, True))
        consume(inner[1:])
    return units


def _quote_ok(text: str, is_list_item: bool, ends_properly: bool) -> bool:
    """The quote-integrity gate: under the character cap, no heading or
    cross-reference noise anywhere in it, and -- for a plain sentence -- a
    true sentence start (capital letter or digit) and a true end (terminal
    punctuation or the real end of the field, never an arbitrary cut). A
    list item is exempt from the start/end check: it's already bounded by
    real "•" structure (or its own internal sentence end) by
    construction in _units_from_block, and its lead-in's start was already
    validated there."""
    if not text or len(text) > MAX_QUOTE_CHARS:
        return False
    if _has_heading_or_xref_noise(text):
        return False
    if is_list_item:
        return True
    if not (text[0].isupper() or text[0].isdigit()):
        return False
    colon = text.find(":")
    if colon != -1:
        lead_clause = text[:colon].lower()
        # A short leading clause ending in ":" is allowed only when it
        # actually names an audience ("Adults:", "Children under 12 years
        # and over:") -- a real Drug Facts pattern. "This product"/"when
        # using" is the field re-announcing its own section name inline
        # (the same self-titling that "2 DOSAGE AND ADMINISTRATION" does
        # for Rx labels, just not ALL-CAPS), not an audience, and a colon
        # deep in the sentence is a second Drug-Facts entry flattened onto
        # the first with no line break, e.g. "...alcoholic drinks every day
        # while using this product Allergy alert: Acetaminophen may cause
        # severe skin reactions." Either way it means this "sentence" isn't
        # one single, coherent instruction.
        if colon > _LEAD_COLON_MAX_CHARS or "product" in lead_clause:
            return False
    return ends_properly


def _phrase_in(text_lower: str, phrase: str) -> bool:
    return re.search(rf"\b{re.escape(phrase)}\b", text_lower) is not None


def _cue_count(sentence_lower: str) -> int:
    return sum(1 for cue in INSTRUCTION_CUES if _phrase_in(sentence_lower, cue))


@dataclass(frozen=True)
class QuoteMatch:
    quote: str  # <=300 chars, verbatim, passes _quote_ok
    field: str
    item: str


@dataclass(frozen=True)
class PointerMatch:
    field: str
    item: str


def _matching_variant(text_lower: str, item: str) -> str | None:
    """The first of `item`'s listed variants that literally appears in
    `text_lower` (word-boundary matched), or None. Never stems or
    fuzzy-matches -- a variant not on the list is not a match."""
    for variant in ITEM_VARIANTS[item]:
        if _phrase_in(text_lower, variant):
            return variant
    return None


def _label_units(label: dict) -> list[tuple[str, str, int, bool, bool]]:
    """(text, field, field_priority_index, is_list_item, ends_properly) for
    every quotable unit of every text field that actually exists on this
    label, in TEXT_FIELDS order -- never assumes a field is present."""
    out = []
    for idx, field in enumerate(TEXT_FIELDS):
        for block in label.get(field) or []:
            for text, is_list_item, ends_properly in _units_from_block(block):
                body = separate_section_heading(text)
                if body is not None:
                    text = body
                out.append((text, field, idx, is_list_item, ends_properly))
    return out


def label_matches(
    label: dict, item: str
) -> tuple[list[QuoteMatch], list[PointerMatch]]:
    """Every unit (sentence or reconstructed list item) that qualifies by
    item-variant + instruction cue: at most 2 QuoteMatches (best-scoring --
    most instruction cues, then earliest field -- deduped by text) for the
    units that also pass _quote_ok, and a PointerMatch for any other field
    whose only qualifying units failed quote integrity -- never silently
    dropped, just not quotable. A field that produced at least one passing
    quote never also produces a pointer for the same item."""
    scored: list[tuple[tuple[int, int], str, str]] = []
    field_status: dict[str, str] = {}  # field -> "pass" | "fail"

    for text, field, field_idx, is_list_item, ends_properly in _label_units(label):
        lowered = text.lower()
        variant = _matching_variant(lowered, item)
        if variant is None:
            continue
        cues = _cue_count(lowered)
        if cues == 0:
            continue
        if _quote_ok(text, is_list_item, ends_properly):
            # The quote must literally contain the *variant* it's credited
            # with matching -- asserted, never just assumed from the check
            # above. The base item term itself (e.g. "food") need not
            # appear verbatim.
            assert variant in lowered, (
                f"quote {text!r} does not contain matched variant {variant!r}"
            )
            scored.append(((-cues, field_idx), text, field))
            field_status[field] = "pass"
        elif field not in field_status:
            field_status[field] = "fail"

    seen: set[str] = set()
    quotes: list[QuoteMatch] = []
    for _score, text, field in sorted(scored, key=lambda t: t[0]):
        if text in seen:
            continue
        seen.add(text)
        quotes.append(QuoteMatch(text, field, item))
        if len(quotes) == 2:
            break

    pointers = [
        PointerMatch(field, item)
        for field, status in field_status.items()
        if status == "fail"
    ]
    return quotes, pointers


def qualifying_matches(label: dict, item: str) -> list[QuoteMatch]:
    """At most 2 quote-integrity-passing matches -- see label_matches. Kept
    as its own function since it's the direct, pre-pointer-fallback
    contract scripts/build_food_guidance_curated.py builds curated entries
    from: an entry is only ever written from a real, passing quote."""
    return label_matches(label, item)[0]


def qualifying_pointers(label: dict, item: str) -> list[PointerMatch]:
    """Fields with a qualifying-but-not-quotable unit -- see label_matches."""
    return label_matches(label, item)[1]


# --- Dosage form and product name (for attribution, not matching) ---------------

_DOSAGE_FORM_FIELDS = (
    "dosage_forms_and_strengths",
    "package_label_principal_display_panel",
    "dosage_and_administration",
    "directions",
)

# The modifier group is a fixed release/form vocabulary, not "any preceding
# word" -- a drug's own name often precedes its dosage-form noun too (e.g.
# "Warfarin Sodium Tablets"), and "sodium" is no more a dosage form than
# "warfarin" is.
_DOSAGE_FORM_NOUN_RE = re.compile(
    r"\b(?:((?:Extended|Immediate|Delayed|Sustained|Controlled)-Release|"
    r"Orally[- ]Disintegrating|Chewable|Effervescent)\s+)?"
    r"(Tablets?|Capsules?|Caplets?|Gelcaps?|Softgels?|Chewables?|"
    r"Suspensions?|Solutions?|Syrups?|Liquids?|Lozenges?|Powders?|Granules?)\b",
    re.IGNORECASE,
)


def _singularize(word: str) -> str:
    lower = word.lower()
    if lower.endswith("ies"):
        return lower[:-3] + "y"
    if lower.endswith("s") and not lower.endswith("ss"):
        return lower[:-1]
    return lower


def label_dosage_form(label: dict) -> str | None:
    """A dosage form phrase read off the label's own text (e.g.
    "extended-release tablet") with a fixed noun vocabulary -- never
    guessed when nothing matches. openFDA's structured
    openfda.dosage_form field is empty on nearly every label in this
    dataset, so free text (dosage_forms_and_strengths for Rx labels,
    package_label_principal_display_panel for OTC Drug Facts panels) is
    all there is."""
    for field in _DOSAGE_FORM_FIELDS:
        for block in label.get(field) or []:
            m = _DOSAGE_FORM_NOUN_RE.search(block)
            if m:
                modifier = (m.group(1) or "").strip()
                noun = _singularize(m.group(2))
                return f"{modifier.lower()} {noun}".strip()
    return None


def label_product_name(label: dict) -> str | None:
    names = label.get("openfda", {}).get("brand_name") or []
    return names[0] if names else None


# --- Pointer text (DailyMed) ------------------------------------------------------

DAILYMED_URL = "https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid={set_id}"

FIELD_DISPLAY_NAMES = {
    "dosage_and_administration": "Dosage and Administration",
    "directions": "Directions",
    "information_for_patients": "Patient Counseling Information",
    "spl_medguide": "Medication Guide",
    "warnings": "Warnings",
    "precautions": "Precautions",
    "when_using": "When Using This Product",
    "do_not_use": "Do Not Use",
    "ask_doctor": "Ask a Doctor",
    "ask_doctor_or_pharmacist": "Ask a Doctor or Pharmacist",
    "drug_interactions": "Drug Interactions",
}


def _field_display(field: str) -> str:
    return FIELD_DISPLAY_NAMES.get(field, field.replace("_", " ").title())


def _join_items(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def pointer_text(product_name: str, item: str, field: str, set_id: str | None) -> str:
    """Never invents a DailyMed link -- built only from a real set_id, the
    citation sentence otherwise stands alone."""
    base = (
        f"The FDA label for {product_name} mentions {item} in its "
        f"{_field_display(field)} section."
    )
    if not set_id:
        return base
    url = DAILYMED_URL.format(set_id=set_id)
    return f"{base} Read the full section on DailyMed: {url}"


# --- Classification and timing windows ------------------------------------------


# Conservative on purpose: a quote is classified avoid_item/take_with_food
# only on these specific phrases, and avoid_item only when the phrase sits
# close to the item term itself -- a quote that merely *mentions* "avoid"
# somewhere unrelated (e.g. "avoid drastic diet changes" in a sentence
# that's really about keeping vitamin K intake *consistent*) must not read
# as "avoid this item".
AVOID_PHRASES = ("avoid", "do not", "should not", "not recommended", "must not")
AVOID_MAX_GAP_WORDS = 4
CUE_GOVERNED_VERBS = frozenset(
    {
        "crush",
        "crushing",
        "chew",
        "chewing",
        "split",
        "splitting",
        "break",
        "breaking",
        "dissolve",
        "dissolving",
        "swallow",
        "swallowing",
        "cut",
        "cutting",
        "open",
        "opening",
        "divide",
        "dividing",
        "exceed",
        "exceeding",
    }
)
_CLAUSE_BREAK_RE = re.compile(r"[.;:]")
TAKE_WITH_FOOD_PHRASES = ("with food", "with meals", "with milk", "after eating")
# "with the evening meal", "with the morning meal", etc.
_WITH_THE_MEAL_RE = re.compile(r"\bwith the \w+ meal\b")
_CONDITIONAL_RES = (
    re.compile(r"\bif\b"),
    re.compile(r"\bwhen\b.*\boccurs?\b"),
    re.compile(r"\bshould\b.*\boccur\b"),
    re.compile(r"\bin case of\b"),
)

_WORD_RE = re.compile(r"[a-z0-9]+")


def _avoid_cue_governs_item(lowered: str, variants: tuple[str, ...]) -> bool:
    for phrase in AVOID_PHRASES:
        for cue in re.finditer(rf"\b{re.escape(phrase)}\b", lowered):
            tail = lowered[cue.end() :]
            for variant in variants:
                for hit in re.finditer(rf"\b{re.escape(variant)}\b", tail):
                    gap = tail[: hit.start()]
                    if _CLAUSE_BREAK_RE.search(gap):
                        continue
                    gap_words = _WORD_RE.findall(gap)
                    if len(gap_words) > AVOID_MAX_GAP_WORDS:
                        continue
                    if CUE_GOVERNED_VERBS.intersection(gap_words):
                        continue
                    return True
    return False


def _is_conditional(lowered: str) -> bool:
    return any(
        rx.search(clause) for clause in lowered.split(";") for rx in _CONDITIONAL_RES
    )


def classify(quote: str, item: str) -> str:
    lowered = quote.lower()
    variants = ITEM_VARIANTS.get(item, (item.lower(),))
    if _phrase_in(lowered, "empty stomach"):
        return "take_on_empty_stomach"
    if _avoid_cue_governs_item(lowered, variants):
        return "avoid_item"
    if re.search(r"\bhours?\s+(before|after)\b", lowered) or _phrase_in(
        lowered, "separate"
    ):
        return "separate_from"
    if any(
        _phrase_in(lowered, p) for p in TAKE_WITH_FOOD_PHRASES
    ) or _WITH_THE_MEAL_RE.search(lowered):
        if _is_conditional(lowered):
            return "take_with_food_if_upset"
        return "take_with_food"
    return "other"


_BEFORE_AFTER_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*hours?\s*before.{0,40}?(\d+(?:\.\d+)?)\s*hours?\s*after",
    re.IGNORECASE,
)
_AFTER_BEFORE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*hours?\s*after.{0,40}?(\d+(?:\.\d+)?)\s*hours?\s*before",
    re.IGNORECASE,
)


def parse_window(quote: str) -> tuple[float, float] | None:
    """(before_hours, after_hours), only from an explicit two-sided
    statement like "at least 2 hours before or 4 hours after". A one-sided
    or vague range yields no window -- the quote alone, never a guessed
    window."""
    m = _BEFORE_AFTER_RE.search(quote)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = _AFTER_BEFORE_RE.search(quote)
    if m:
        return float(m.group(2)), float(m.group(1))
    return None


def _window_json(window: tuple[float, float] | None) -> dict | None:
    if window is None:
        return None
    before_hours, after_hours = window
    return {"before_hours": before_hours, "after_hours": after_hours}


# --- Curated data (Source B) ----------------------------------------------------


@dataclass(frozen=True)
class CuratedGuidance:
    substance: str  # registry id, as given to the build script
    item: str
    quote: str
    label_field: str
    set_id: str | None
    date_fetched: str
    verified: bool
    product_name: str | None = None
    dosage_form: str | None = None


def _is_real_curated_entry(e: dict) -> bool:
    """No quote, no entry -- the build script never writes one without a
    quote, but the loader refuses one anyway as a second line of defense."""
    return bool((e.get("quote") or "").strip())


class InvalidCuratedEntry(ValueError):
    pass


@lru_cache(maxsize=1)
def load_curated(path: Path | None = None) -> tuple[CuratedGuidance, ...]:
    data = json.loads((path or DATA_PATH).read_text(encoding="utf-8"))
    for e in data["entries"]:
        missing = verification.missing_verification_fields(e)
        if missing:
            raise InvalidCuratedEntry(
                f"entry {e.get('substance')!r} / {e.get('item')!r} is marked "
                f"verified but is missing {', '.join(missing)}"
            )
    return tuple(
        CuratedGuidance(
            substance=e["substance"],
            item=e["item"],
            quote=e["quote"],
            label_field=e.get("label_field", ""),
            set_id=e.get("set_id"),
            date_fetched=e.get("date_fetched", ""),
            verified=e.get("verified") is True,
            product_name=e.get("product_name"),
            dosage_form=e.get("dosage_form"),
        )
        for e in data["entries"]
        if _is_real_curated_entry(e)
    )


def clear_curated_cache() -> None:
    load_curated.cache_clear()


def unverified_curated_count() -> int:
    return sum(1 for c in load_curated() if not c.verified)


# --- Regimen assembly ------------------------------------------------------------


@dataclass(frozen=True)
class RegimenItem:
    key: str  # substance id, or "product:<lowercased name>" for a combo
    display_name: str
    is_product: bool
    lookup_name: str  # generic name (single) or brand/product name (combo)


def _regimen_item_for_names(
    product_name: str, substance_ids: list[str], registry: Registry
) -> RegimenItem | None:
    if not substance_ids:
        return None
    if len(substance_ids) == 1:
        entry = registry.get(substance_ids[0])
        return RegimenItem(substance_ids[0], entry["name"], False, entry["name"])
    return RegimenItem(
        f"product:{product_name.strip().lower()}", product_name, True, product_name
    )


def _item_for_dose(
    ev: DoseEvent, registry: Registry
) -> tuple[RegimenItem | None, list[str]]:
    # Skincare/cosmetic products (topical_ingredients set -- curated
    # skincare entries, My products entries, or any openFDA product whose
    # own label route is TOPICAL) are out of scope for food/drug guidance
    # entirely: not checked, and their ingredients never show up as
    # "wasn't recognized" either.
    if ev.topical_ingredients:
        return None, []
    substance_ids = [ing.substance_id for ing in ev.ingredients if ing.substance_id]
    unresolved = [ing.ingredient_name for ing in ev.ingredients if not ing.substance_id]
    return _regimen_item_for_names(ev.product_name, substance_ids, registry), unresolved


def _item_for_schedule(
    s: ScheduledDose, registry: Registry
) -> tuple[RegimenItem | None, list[str]]:
    if s.topical_ingredients:
        return None, []
    substance_ids = [
        e["substance_id"] for e in s.substance_entries if e["substance_id"]
    ]
    unresolved = [e["name"] for e in s.substance_entries if not e["substance_id"]]
    return _regimen_item_for_names(s.product_name, substance_ids, registry), unresolved


# --- Findings --------------------------------------------------------------------


def _finding(
    item: RegimenItem,
    item_term: str,
    match: QuoteMatch,
    source_type: str,
    verified: bool,
    product_name: str | None = None,
    dosage_form: str | None = None,
    quote_source: str | None = None,
) -> dict:
    return {
        "name": item.display_name,
        "is_product": item.is_product,
        "item": item_term,
        "items": [item_term],
        "class": classify(match.quote, item_term),
        "quote": match.quote,
        "pointer": None,
        "source_type": source_type,
        "label_field": match.field,
        "verified": verified,
        "quote_source": quote_source,
        "window": _window_json(parse_window(match.quote)),
        # True only when a curated entry quoting the same sentence was
        # folded into this (live) finding -- see _dedupe_against_curated.
        # Never set on a curated finding itself.
        "also_in_curated": False,
        # The label this finding was read from -- so a form-specific
        # instruction (e.g. "extended-release tablet") is never displayed
        # as if it applied generally. Read off the label's own text; None
        # when nothing matched, never guessed.
        "product_name": product_name,
        "dosage_form": dosage_form,
    }


def _pointer_finding(
    item: RegimenItem,
    item_term: str,
    pointer: PointerMatch,
    product_name: str | None,
    dosage_form: str | None,
    set_id: str | None,
) -> dict:
    return _collapsed_pointer_finding(
        item, [item_term], pointer.field, product_name, dosage_form, set_id
    )


def _collapsed_pointer_finding(
    item: RegimenItem,
    item_terms: list[str],
    field: str,
    product_name: str | None,
    dosage_form: str | None,
    set_id: str | None,
) -> dict:
    """A unit qualified by item + instruction cue but failed the quote
    integrity rules -- reported anyway, as a citation with no quote, rather
    than silently dropped."""
    display_product = product_name or item.display_name
    joined = _join_items(item_terms)
    return {
        "name": item.display_name,
        "is_product": item.is_product,
        "item": joined,
        "items": item_terms,
        "class": "other",
        "quote": None,
        "pointer": pointer_text(display_product, joined, field, set_id),
        "source_type": "fda_label",
        "label_field": field,
        "verified": False,
        "quote_source": None,
        "window": None,
        "also_in_curated": False,
        "product_name": product_name,
        "dosage_form": dosage_form,
    }


def _normalize_whitespace(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _dedupe_against_curated(
    live_findings: list[dict], curated_findings: list[dict]
) -> list[dict]:
    """When a live and a curated finding quote the same sentence (exact, or
    the same after collapsing whitespace), keep only the live one, flagged
    also_in_curated -- one finding, not two, for something confirmed twice.
    Pointer findings (quote is None) never participate -- there's no quote
    text to compare."""
    live_by_quote: dict[str, list[dict]] = {}
    for f in live_findings:
        if f["quote"] is None:
            continue
        live_by_quote.setdefault(_normalize_whitespace(f["quote"]), []).append(f)

    kept_curated: list[dict] = []
    for cf in curated_findings:
        matches = live_by_quote.get(_normalize_whitespace(cf["quote"]))
        if matches:
            matches[0]["also_in_curated"] = True
        else:
            kept_curated.append(cf)

    return live_findings + kept_curated


def _labels_for(item: RegimenItem) -> list[dict]:
    if item.is_product:
        return fetch_labels_for_product(item.lookup_name)
    return fetch_labels_for_substance(item.lookup_name)


def _prefetch_labels(items: list[RegimenItem]) -> dict[str, list[dict] | Exception]:
    by_key = {item.key: item for item in items}
    return label_cache.fetch_concurrently(
        list(by_key), lambda key: _labels_for(by_key[key])
    )


def _findings_for_item(
    item: RegimenItem,
    unchecked: list[str],
    prefetched: list[dict] | Exception | None = None,
) -> list[dict]:
    live_findings: list[dict] = []
    curated_findings: list[dict] = []

    try:
        if prefetched is None:
            labels = _labels_for(item)
        elif isinstance(prefetched, Exception):
            raise prefetched
        else:
            labels = prefetched
    except OpenFDAUnavailable:
        unchecked.append(
            f"openFDA was unreachable, so label checks could not run for "
            f"{item.display_name}."
        )
    else:
        if not labels:
            unchecked.append(f"No oral label found for {item.display_name}.")
        else:
            oral_labels = [label for label in labels if is_oral(label)]
            if not oral_labels:
                # Message from the top-ranked candidate -- the one that
                # would have been read, had it been oral.
                route = label_route(labels[0])
                if route is None:
                    unchecked.append(
                        f"{item.display_name}'s route could not be "
                        "determined from its label."
                    )
                else:
                    unchecked.append(
                        f"No oral label found for {item.display_name} "
                        f"(label route is {route.title()})."
                    )
            else:
                seen_quotes: set[str] = set()
                seen_pointers: set[tuple[str, str]] = set()
                for label in oral_labels:
                    product_name = label_product_name(label)
                    dosage_form = label_dosage_form(label)
                    set_id = label.get("set_id")
                    pointer_items: dict[str, list[str]] = {}
                    for item_term in ITEM_TERMS:
                        quotes, pointers = label_matches(label, item_term)
                        for match in quotes:
                            norm = _normalize_whitespace(match.quote)
                            if norm in seen_quotes:
                                continue
                            seen_quotes.add(norm)
                            live_findings.append(
                                _finding(
                                    item,
                                    item_term,
                                    match,
                                    "fda_label",
                                    False,
                                    product_name,
                                    dosage_form,
                                    QUOTE_SOURCE_FDA_LABEL,
                                )
                            )
                        for p in pointers:
                            key = (p.field, p.item)
                            if key in seen_pointers:
                                continue
                            seen_pointers.add(key)
                            pointer_items.setdefault(p.field, []).append(p.item)
                    for field, terms in pointer_items.items():
                        live_findings.append(
                            _collapsed_pointer_finding(
                                item,
                                terms,
                                field,
                                product_name,
                                dosage_form,
                                set_id,
                            )
                        )

    # Source B, curated: single-ingredient only -- a combination product's
    # guidance belongs to the product (Source A), never one ingredient.
    if not item.is_product:
        for c in load_curated():
            if c.substance != item.key:
                continue
            if not (c.verified or verification.show_unverified_rules()):
                continue
            match = QuoteMatch(c.quote, c.label_field, c.item)
            curated_findings.append(
                _finding(
                    item,
                    c.item,
                    match,
                    "curated",
                    c.verified,
                    c.product_name,
                    c.dosage_form,
                )
            )

    return _dedupe_against_curated(live_findings, curated_findings)


# --- Timeline --------------------------------------------------------------------


def _timeline_entries(
    session: Session,
    user_id: str,
    events: list[DoseEvent],
    registry: Registry,
    now: datetime,
    guidance_by_key: dict[str, list[dict]],
) -> list[dict]:
    entries: list[dict] = []
    ongoing_emitted: set[str] = set()
    markers_emitted: set[tuple[str, str, str]] = set()

    def emit(key: str, display_name: str, at: datetime, marker_source: str) -> None:
        for f in guidance_by_key.get(key, []):
            cls = f["class"]
            if cls in ("take_with_food", "take_on_empty_stomach"):
                marker_key = (at.isoformat(), key, cls)
                if marker_key in markers_emitted:
                    continue
                markers_emitted.add(marker_key)
                entries.append(
                    {
                        "type": "dose_marker",
                        "class": cls,
                        "name": display_name,
                        "quote": f["quote"],
                        "at": at.isoformat(),
                        "source": marker_source,
                    }
                )
            elif cls == "separate_from" and f["window"] is not None:
                before_h = f["window"]["before_hours"]
                after_h = f["window"]["after_hours"]
                entries.append(
                    {
                        "type": "window",
                        "class": cls,
                        "name": display_name,
                        "quote": f["quote"],
                        "from": (at - timedelta(hours=before_h)).isoformat(),
                        "to": (at + timedelta(hours=after_h)).isoformat(),
                    }
                )
            elif cls == "avoid_item" and key not in ongoing_emitted:
                ongoing_emitted.add(key)
                entries.append(
                    {
                        "type": "ongoing",
                        "class": cls,
                        "name": display_name,
                        "quote": f["quote"],
                        "note": ONGOING_NOTE,
                    }
                )

    for ev in events:
        taken_at = datetime.fromisoformat(ev.taken_at)
        if not (-REGIMEN_LOOKBACK_HOURS <= service.hours_since(taken_at, now) <= 0.0):
            continue
        item, _unresolved = _item_for_dose(ev, registry)
        if item is not None:
            emit(item.key, item.display_name, taken_at, "logged")

    for s in schedule.list_for_user(session, user_id):
        item, _unresolved = _item_for_schedule(s, registry)
        if item is None:
            continue
        end = now + timedelta(hours=TIMELINE_HORIZON_HOURS)
        for slot in schedule.evaluate(s, events, now, now, end):
            emit(item.key, item.display_name, slot.at, "scheduled")

    return entries


# --- Response assembly ------------------------------------------------------------


def _unresolved_reasons(names: list[str]) -> list[str]:
    return [f"{name!r} wasn't recognized as a medication ingredient." for name in names]


def _respond(items: list[RegimenItem], unresolved: list[str]) -> tuple[dict, dict]:
    """(response body minus timeline, guidance keyed by item for the
    timeline builder)."""
    guidance: list[dict] = []
    unchecked: list[str] = _unresolved_reasons(unresolved)
    guidance_by_key: dict[str, list[dict]] = {}
    seen: set[str] = set()

    unique = list({item.key: item for item in items}.values())
    prefetched = _prefetch_labels(unique)

    for item in items:
        if item.key in seen:
            continue
        seen.add(item.key)
        findings = _findings_for_item(item, unchecked, prefetched[item.key])
        guidance_by_key[item.key] = findings
        guidance.extend(findings)

    body = {
        "guidance": guidance,
        "unchecked_reason": unchecked,
        "message": EMPTY_MESSAGE if not guidance else None,
    }
    verification.attach_notice(body, unverified_curated_count())
    return body, guidance_by_key


def check_regimen(
    session: Session, user_id: str, registry: Registry, now: datetime
) -> dict:
    """Active scheduled medications plus doses logged in the last 24h --
    same regimen definition as app/api/interactions.py.check_regimen."""
    events = service.load_doses(session, user_id)
    items: list[RegimenItem] = []
    unresolved: list[str] = []

    for ev in events:
        taken_at = datetime.fromisoformat(ev.taken_at)
        if not (-REGIMEN_LOOKBACK_HOURS <= service.hours_since(taken_at, now) <= 0.0):
            continue
        item, unres = _item_for_dose(ev, registry)
        unresolved.extend(unres)
        if item is not None:
            items.append(item)

    for s in schedule.list_for_user(session, user_id):
        item, unres = _item_for_schedule(s, registry)
        unresolved.extend(unres)
        if item is not None:
            items.append(item)

    body, guidance_by_key = _respond(items, unresolved)
    body["timeline"] = _timeline_entries(
        session, user_id, events, registry, now, guidance_by_key
    )
    return body


def check_manual(names: list[str], registry: Registry) -> dict:
    """A user-typed list of medication names -- no dose events or schedule
    exist for these, so no timeline."""
    items: list[RegimenItem] = []
    unresolved: list[str] = []

    for name in names:
        substance_id = registry.resolve(name)
        if substance_id is not None:
            item = _regimen_item_for_names(name, [substance_id], registry)
            if item is not None:
                items.append(item)
            continue
        matched_product = next(iter(products.search(name, registry)), None)
        if matched_product is None:
            unresolved.append(name)
            continue
        decomposed = products.decompose(matched_product, registry)
        substance_ids = [d.substance_id for d in decomposed if d.substance_id]
        item = _regimen_item_for_names(
            matched_product.brand_name, substance_ids, registry
        )
        if item is None:
            unresolved.append(name)
        else:
            items.append(item)

    body, _guidance_by_key = _respond(items, unresolved)
    body["timeline"] = []
    return body
