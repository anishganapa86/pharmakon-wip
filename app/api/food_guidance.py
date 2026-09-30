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
"will cause". A finding is classified (take_with_food, take_on_empty_stomach,
avoid_item, separate_from, other) by simple keyword rules for display, but
the classification is never shown as more than a tag next to its quote.

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
from . import products, schedule, service
from .db import DoseEvent, ScheduledDose

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
    "water": ("water",),
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
    "with",
    "without",
    "separate",
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


def _fetch_candidates_by(field: str, value: str) -> list[dict]:
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
    return r.json().get("results", [])


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


def label_route(label: dict) -> str | None:
    routes = label.get("openfda", {}).get("route") or []
    return routes[0] if routes else None


def is_oral(label: dict) -> bool:
    routes = [r.upper() for r in (label.get("openfda", {}).get("route") or [])]
    return "ORAL" in routes


# --- Sentence extraction and qualification --------------------------------------

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _sentences_from_label(label: dict) -> list[tuple[str, str, int]]:
    """(sentence, field, field_priority_index) for every sentence of every
    text field that actually exists on this label, in TEXT_FIELDS order --
    never assumes a field is present."""
    out = []
    for idx, field in enumerate(TEXT_FIELDS):
        for block in label.get(field) or []:
            for raw in _SENTENCE_SPLIT_RE.split(block):
                sentence = raw.strip()
                if sentence:
                    out.append((sentence, field, idx))
    return out


def _phrase_in(text_lower: str, phrase: str) -> bool:
    return re.search(rf"\b{re.escape(phrase)}\b", text_lower) is not None


def _cue_count(sentence_lower: str) -> int:
    return sum(1 for cue in INSTRUCTION_CUES if _phrase_in(sentence_lower, cue))


@dataclass(frozen=True)
class QuoteMatch:
    quote: str  # <=300 chars, verbatim
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


def qualifying_matches(label: dict, item: str) -> list[QuoteMatch]:
    """At most 2 qualifying sentences for this (label, item), best-scoring
    (most instruction cues, then earliest field) first, deduped by quote
    text. A sentence qualifies only if it contains one of the item's listed
    variants AND at least one instruction cue -- both checked here, never
    assumed."""
    scored: list[tuple[tuple[int, int], str, str]] = []
    for sentence, field, field_idx in _sentences_from_label(label):
        # Qualify against the text as it will actually be quoted (<=300
        # chars) -- a real label "sentence" (the splitter isn't perfect on
        # bullet-style, period-free lists) can run past that, and checking
        # the untruncated text would let a match get created and then have
        # its evidence sliced off.
        quote = sentence[:300]
        lowered = quote.lower()
        variant = _matching_variant(lowered, item)
        if variant is None:
            continue
        cues = _cue_count(lowered)
        if cues == 0:
            continue
        # The quote must literally contain the *variant* it's credited with
        # matching -- asserted, never just assumed from the check above. The
        # base item term itself (e.g. "food") need not appear verbatim.
        assert variant in quote.lower(), (
            f"quote {quote!r} does not contain matched variant {variant!r}"
        )
        scored.append(((-cues, field_idx), quote, field))

    seen: set[str] = set()
    out: list[QuoteMatch] = []
    for _score, quote, field in sorted(scored, key=lambda t: t[0]):
        if quote in seen:
            continue
        seen.add(quote)
        out.append(QuoteMatch(quote, field, item))
        if len(out) == 2:
            break
    return out


# --- Classification and timing windows ------------------------------------------


# Conservative on purpose: a quote is classified avoid_item/take_with_food
# only on these specific phrases, and avoid_item only when the phrase sits
# close to the item term itself -- a quote that merely *mentions* "avoid"
# somewhere unrelated (e.g. "avoid drastic diet changes" in a sentence
# that's really about keeping vitamin K intake *consistent*) must not read
# as "avoid this item".
AVOID_PHRASES = ("avoid", "do not", "should not", "not recommended")
AVOID_WORD_WINDOW = 8
TAKE_WITH_FOOD_PHRASES = ("with food", "with meals", "with milk", "after eating")
# "with the evening meal", "with the morning meal", etc.
_WITH_THE_MEAL_RE = re.compile(r"\bwith the \w+ meal\b")

_WORD_RE = re.compile(r"[a-z0-9]+")


def _word_index_at(words: list[re.Match], char_pos: int) -> int:
    for i, w in enumerate(words):
        if w.start() <= char_pos < w.end():
            return i
    return min(range(len(words)), key=lambda i: abs(words[i].start() - char_pos))


def _within_words(lowered: str, phrase: str, item: str, max_words: int) -> bool:
    """True if `phrase` and `item` each appear as whole-word/phrase matches
    within `max_words` words of each other, anywhere in the text."""
    phrase_hits = [m.start() for m in re.finditer(rf"\b{re.escape(phrase)}\b", lowered)]
    item_hits = [m.start() for m in re.finditer(rf"\b{re.escape(item)}\b", lowered)]
    if not phrase_hits or not item_hits:
        return False
    words = list(_WORD_RE.finditer(lowered))
    if not words:
        return False
    for p in phrase_hits:
        pi = _word_index_at(words, p)
        for it in item_hits:
            if abs(pi - _word_index_at(words, it)) <= max_words:
                return True
    return False


def classify(quote: str, item: str) -> str:
    lowered = quote.lower()
    variants = ITEM_VARIANTS.get(item, (item.lower(),))
    if _phrase_in(lowered, "empty stomach"):
        return "take_on_empty_stomach"
    if any(
        _within_words(lowered, phrase, variant, AVOID_WORD_WINDOW)
        for phrase in AVOID_PHRASES
        for variant in variants
    ):
        return "avoid_item"
    if re.search(r"\bhours?\s+(before|after)\b", lowered) or _phrase_in(
        lowered, "separate"
    ):
        return "separate_from"
    if any(
        _phrase_in(lowered, p) for p in TAKE_WITH_FOOD_PHRASES
    ) or _WITH_THE_MEAL_RE.search(lowered):
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


def _is_real_curated_entry(e: dict) -> bool:
    """No quote, no entry -- the build script never writes one without a
    quote, but the loader refuses one anyway as a second line of defense."""
    return bool((e.get("quote") or "").strip())


@lru_cache(maxsize=1)
def load_curated(path: Path | None = None) -> tuple[CuratedGuidance, ...]:
    data = json.loads((path or DATA_PATH).read_text(encoding="utf-8"))
    return tuple(
        CuratedGuidance(
            substance=e["substance"],
            item=e["item"],
            quote=e["quote"],
            label_field=e.get("label_field", ""),
            set_id=e.get("set_id"),
            date_fetched=e.get("date_fetched", ""),
            verified=e.get("verified", False),
        )
        for e in data["entries"]
        if _is_real_curated_entry(e)
    )


def clear_curated_cache() -> None:
    load_curated.cache_clear()


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
    substance_ids = [ing.substance_id for ing in ev.ingredients if ing.substance_id]
    unresolved = [ing.ingredient_name for ing in ev.ingredients if not ing.substance_id]
    return _regimen_item_for_names(ev.product_name, substance_ids, registry), unresolved


def _item_for_schedule(
    s: ScheduledDose, registry: Registry
) -> tuple[RegimenItem | None, list[str]]:
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
) -> dict:
    return {
        "name": item.display_name,
        "is_product": item.is_product,
        "item": item_term,
        "class": classify(match.quote, item_term),
        "quote": match.quote,
        "source_type": source_type,
        "label_field": match.field,
        "verified": verified,
        "window": _window_json(parse_window(match.quote)),
        # True only when a curated entry quoting the same sentence was
        # folded into this (live) finding -- see _dedupe_against_curated.
        # Never set on a curated finding itself.
        "also_in_curated": False,
    }


def _normalize_whitespace(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _dedupe_against_curated(
    live_findings: list[dict], curated_findings: list[dict]
) -> list[dict]:
    """When a live and a curated finding quote the same sentence (exact, or
    the same after collapsing whitespace), keep only the live one, flagged
    also_in_curated -- one finding, not two, for something confirmed twice."""
    live_by_quote: dict[str, list[dict]] = {}
    for f in live_findings:
        live_by_quote.setdefault(_normalize_whitespace(f["quote"]), []).append(f)

    kept_curated: list[dict] = []
    for cf in curated_findings:
        matches = live_by_quote.get(_normalize_whitespace(cf["quote"]))
        if matches:
            matches[0]["also_in_curated"] = True
        else:
            kept_curated.append(cf)

    return live_findings + kept_curated


def _findings_for_item(item: RegimenItem, unchecked: list[str]) -> list[dict]:
    live_findings: list[dict] = []
    curated_findings: list[dict] = []

    try:
        labels = (
            fetch_labels_for_product(item.lookup_name)
            if item.is_product
            else fetch_labels_for_substance(item.lookup_name)
        )
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
                for label in oral_labels:
                    for item_term in ITEM_TERMS:
                        for match in qualifying_matches(label, item_term):
                            norm = _normalize_whitespace(match.quote)
                            if norm in seen_quotes:
                                continue
                            seen_quotes.add(norm)
                            live_findings.append(
                                _finding(item, item_term, match, "fda_label", True)
                            )

    # Source B, curated: single-ingredient only -- a combination product's
    # guidance belongs to the product (Source A), never one ingredient.
    if not item.is_product:
        for c in load_curated():
            if c.substance != item.key:
                continue
            match = QuoteMatch(c.quote, c.label_field, c.item)
            curated_findings.append(
                _finding(item, c.item, match, "curated", c.verified)
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

    def emit(key: str, display_name: str, at: datetime, marker_source: str) -> None:
        for f in guidance_by_key.get(key, []):
            cls = f["class"]
            if cls in ("take_with_food", "take_on_empty_stomach"):
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

    for item in items:
        if item.key in seen:
            continue
        seen.add(item.key)
        findings = _findings_for_item(item, unchecked)
        guidance_by_key[item.key] = findings
        guidance.extend(findings)

    body = {
        "guidance": guidance,
        "unchecked_reason": unchecked,
        "message": EMPTY_MESSAGE if not guidance else None,
    }
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
