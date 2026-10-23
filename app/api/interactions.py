"""Medication interaction checker: two traceable, cited sources, never a model.

Like app/api/skincare.py, this is deliberately NOT pharmacokinetics by
default -- it's a lookup, not a prediction. There are two sources:

  A) Curated rules (app/data/drug_interactions.json). Each entry cites a
     specific FDA label section; a rule with no source doesn't belong there
     (see the file's own `_README`), and `load_rules` refuses to load one
     that lacks one. Every rule is `verified: false` until a human opens the
     cited source (project notes invariant) -- never flip that here.

  B) Live openFDA label text: for a flagged pair, each substance's own
     `drug_interactions` label field is searched for the other substance's
     name, aliases, or drug class (CLASS_KEYWORDS). The matching sentence is
     quoted verbatim (<=300 chars), never paraphrased into a stronger claim.

The one place this DOES touch app.pk is `compute_overlap`, which reuses
app.api.service.compute_curve (never reimplements PK) to say when two
already-modeled, already-logged substances are both above 10% of their own
peak in the next 24 hours. It's explicitly labelled an estimate, and pairs
where either substance isn't modeled get no overlap info at all -- silence
over a guessed window.

Interaction checks are informational only. They never create or feed
app.detect alerts; those stay tied to logged dose events and the two
detectors in app/detect/alerts.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from functools import lru_cache
from itertools import combinations
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from ..pk.individualize import Patient
from ..registry.loader import Registry, normalize, strip_salt_tokens
from . import label_cache, label_source, products, schedule, service, verification
from .db import DoseEvent
from .provenance import QUOTE_SOURCE_FDA_LABEL
from .quote_text import (
    has_cross_reference,
    has_section_number_noise,
    separate_section_heading,
)

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "drug_interactions.json"

OPENFDA_LABEL_URL = "https://api.fda.gov/drug/label.json"
LABEL_LIMIT = 5

# Drug classes used by both the curated rules (as a substance_a/b term) and
# the FDA label text search (via CLASS_KEYWORDS, since a label names a
# category -- "NSAIDs" -- far more often than every member drug by name).
CLASS_MEMBERS: dict[str, tuple[str, ...]] = {
    "NSAID": ("ibuprofen", "naproxen", "aspirin"),
    "SSRI": ("sertraline",),
    "ACE_INHIBITOR": ("lisinopril",),
    "ANTIHISTAMINE_SEDATING": ("diphenhydramine", "doxylamine"),
}
CLASS_KEYWORDS: dict[str, tuple[str, ...]] = {
    "NSAID": ("NSAID", "NSAIDs", "nonsteroidal anti-inflammatory"),
    "SSRI": ("SSRI", "SSRIs", "selective serotonin reuptake inhibitor"),
    "ACE_INHIBITOR": ("ACE inhibitor", "angiotensin-converting enzyme inhibitor"),
    "ANTIHISTAMINE_SEDATING": ("antihistamine",),
}
# How the class reads in a headline: "the drug class NSAIDs", not "NSAID".
CLASS_DISPLAY: dict[str, str] = {
    "NSAID": "NSAIDs",
    "SSRI": "SSRIs",
    "ACE_INHIBITOR": "ACE inhibitors",
    "ANTIHISTAMINE_SEDATING": "sedating antihistamines",
}

# A class keyword alone (e.g. a label's aside about aspirin-sensitive asthma
# mentioning "NSAIDs") is not evidence of a combining-these-two-drugs
# interaction. Require one of these nearby before trusting a class-word match.
CONTEXT_KEYWORDS = (
    "concomitant",
    "concurrent",
    "together with",
    "in combination with",
    "coadminister",
    "other nsaids",
    "another nsaid",
)

GENERIC_INTERACTING_PARTY_TERMS = (
    "anticoagulant",
    "antiplatelet",
    "diuretic",
    "corticosteroid",
    "antidepressant",
    "serotonin reuptake",
    "SNRI",
)
CLASS_PARTY_TERMS: dict[str, tuple[str, ...]] = {
    "SSRI": ("serotonin reuptake", "SNRI", "antidepressant"),
}

PRONOUN_REFERENCE_START = re.compile(
    r"^(?:this|these|that|those|such)\s+"
    r"(?:interactions?|drugs?|medications?|medicines?|combinations?|effects?|"
    r"reactions?|agents?)\b|^(?:it|they)\s+(?:is|are|may|can|should|will)\b",
    re.IGNORECASE,
)

SUBSTANCE_CLASSES: dict[str, tuple[str, ...]] = {}
for _cls, _members in CLASS_MEMBERS.items():
    for _sid in _members:
        SUBSTANCE_CLASSES[_sid] = (*SUBSTANCE_CLASSES.get(_sid, ()), _cls)

EMPTY_MESSAGE = (
    "No documented interactions found in our sources. This does not mean "
    "none exist. Ask a pharmacist to review your full medication list."
)

# Shown instead of EMPTY_MESSAGE when checked_pairs is 0 -- "no interactions
# found" implies pairs were actually checked and came up clean, which isn't
# true when there was nothing (or only one substance) to pair at all.
NO_PAIRS_MESSAGE = "We couldn't check any pairs for this regimen."

LABEL_UNAVAILABLE_NOTE = (
    "openFDA was unreachable, so FDA label checks could not run for some "
    "pairs. Curated reference checks still ran."
)

# Replaced with a mock-transport client in tests.
_http = httpx.Client(timeout=5.0)

# substance_id (a registry id, or a NONREGISTRY_PREFIX-ed key) -> that
# substance's own drug_interactions label text, or None if it has no label
# (or no such field). One fetch per ingredient, cached here, so repeated
# pairs in the same regimen never re-fetch.
_label_cache: dict[str, str | None] = {}

_sections_cache: dict[str, list[dict]] = {}

# Key prefix for an oral ingredient that doesn't resolve against the PK
# registry but is still checked from its own FDA label, by its normalized,
# salt-stripped generic name (see resolve_for_label_check). Prefixed so it
# can never collide with a real registry id, which never contains ":".
NONREGISTRY_PREFIX = "label:"


class OpenFDAUnavailable(Exception):
    """openFDA could not be reached or answered with an error."""


class InvalidRule(ValueError):
    """A curated rule is missing a required, cited source."""


def clear_cache() -> None:
    _label_cache.clear()
    _sections_cache.clear()


# --- A. Curated rules ----------------------------------------------------------


@dataclass(frozen=True)
class CuratedRule:
    substance_a: str  # registry id or a CLASS_MEMBERS key
    substance_b: str
    effect: str
    recommendation: str
    source: str
    verified: bool

    def matches(self, sid_a: str, sid_b: str) -> bool:
        return (
            self._term_matches(self.substance_a, sid_a)
            and self._term_matches(self.substance_b, sid_b)
        ) or (
            self._term_matches(self.substance_a, sid_b)
            and self._term_matches(self.substance_b, sid_a)
        )

    @staticmethod
    def _term_matches(term: str, sid: str) -> bool:
        return term == sid or sid in CLASS_MEMBERS.get(term, ())


@lru_cache(maxsize=1)
def load_rules(path: Path | None = None) -> tuple[CuratedRule, ...]:
    data = json.loads((path or DATA_PATH).read_text())
    rules = []
    for e in data["rules"]:
        source = (e.get("source") or "").strip()
        if not source:
            raise InvalidRule(
                f"rule {e.get('substance_a')!r} + {e.get('substance_b')!r} "
                "has no source; see drug_interactions.json's _README"
            )
        missing = verification.missing_verification_fields(e)
        if missing:
            raise InvalidRule(
                f"rule {e.get('substance_a')!r} + {e.get('substance_b')!r} "
                f"is marked verified but is missing {', '.join(missing)}"
            )
        rules.append(
            CuratedRule(
                substance_a=e["substance_a"],
                substance_b=e["substance_b"],
                effect=e["effect"],
                recommendation=e["recommendation"],
                source=source,
                verified=e.get("verified") is True,
            )
        )
    return tuple(rules)


def active_rules() -> tuple[CuratedRule, ...]:
    rules = load_rules()
    if verification.show_unverified_rules():
        return rules
    return tuple(r for r in rules if r.verified)


def unverified_rule_count() -> int:
    return sum(1 for r in load_rules() if not r.verified)


# --- B. FDA label text -----------------------------------------------------------


def resolve_for_label_check(
    raw_name: str, registry: Registry
) -> tuple[str, dict] | None:
    """(key, entry) for an oral ingredient that doesn't resolve against the
    PK registry, keyed by its normalized, salt-stripped generic name (the
    same normalization Registry.resolve already uses) -- e.g. "DONEPEZIL
    HYDROCHLORIDE" looks up "donepezil". `entry` is shaped like a registry
    entry (id/name/aliases/description) so the rest of this module can
    treat it uniformly via _entry() -- except it carries no curated rules
    (no curated rule's substance_a/b ever equals this synthetic key) and no
    PK curve (registry.modelability has no row for it, so callers must
    check _is_registry_key before asking for one; see check()).

    None only when the name reduces to nothing after normalization (e.g. an
    empty string) -- there's nothing to look up by.
    """
    key_name = strip_salt_tokens(normalize(raw_name))
    if not key_name:
        return None
    key = f"{NONREGISTRY_PREFIX}{key_name}"
    entry = {"id": key, "name": key_name, "aliases": (), "description": None}
    return key, entry


def _is_registry_key(key: str) -> bool:
    return not key.startswith(NONREGISTRY_PREFIX)


def _entry(key: str, registry: Registry, shadow: dict[str, dict]) -> dict:
    """registry.get(key) for a real substance id, or the synthetic entry a
    non-registry oral ingredient was resolved to (resolve_for_label_check),
    looked up from `shadow` -- shaped identically (id/name/aliases/
    description) so every caller below can treat the two uniformly."""
    if _is_registry_key(key):
        return registry.get(key)
    return shadow[key]


def fetch_drug_interaction_sections(generic_name: str) -> list[dict]:
    sections = label_cache.cached(
        "drug_interaction_sections_v2",
        generic_name.upper(),
        lambda: _request_drug_interaction_sections(generic_name),
    )
    _sections_cache[generic_name.upper()] = sections
    return sections


def fetch_drug_interactions_text(generic_name: str) -> str | None:
    return label_source.sections_text(fetch_drug_interaction_sections(generic_name))


def _request_drug_interaction_sections(generic_name: str) -> list[dict]:
    """`drug_interactions` sections from single-ingredient openFDA labels,
    each with the label it came from, or [] if there are none.

    Raises OpenFDAUnavailable on network or server errors, so the caller can
    tell "no label" apart from "couldn't ask" (same contract as
    app/api/side_effects.py's fetch_adverse_reactions).
    """
    results = label_source.oral_labels(
        _search_results(
            f'openfda.generic_name.exact:"{generic_name.upper()}"'
            " AND _exists_:drug_interactions",
            LABEL_LIMIT,
        ),
        generic_name,
    )
    if not results:
        candidates = _search_results(
            label_source.fallback_search(
                "generic_name", generic_name, "drug_interactions"
            ),
            label_source.FALLBACK_LIMIT,
        )
        results = [
            label
            for label in candidates
            if label_source.is_oral(label)
            and label_source.fallback_matches("generic_name", label, generic_name)
        ][:LABEL_LIMIT]
    return label_source.sections_from_results(
        results, "drug_interactions", generic_name.lower()
    )


def _search_results(search: str, limit: int) -> list[dict]:
    try:
        r = _http.get(OPENFDA_LABEL_URL, params={"search": search, "limit": limit})
    except httpx.HTTPError as exc:
        raise OpenFDAUnavailable(str(exc)) from exc
    if r.status_code == 404:  # openFDA's "no matches"
        return []
    if r.status_code != 200:
        raise OpenFDAUnavailable(f"openFDA returned {r.status_code}")
    return r.json().get("results", [])


def _label_text(substance_id: str, generic_name: str) -> str | None:
    """Cached per substance. Not cached on failure, so the next call retries."""
    if substance_id in _label_cache:
        return _label_cache[substance_id]
    text = fetch_drug_interactions_text(generic_name)  # may raise
    _label_cache[substance_id] = text
    return text


def _prefetch_label_texts(
    substance_ids: list[str], registry: Registry, shadow: dict[str, dict]
) -> set[str]:
    names = {
        sid: _entry(sid, registry, shadow)["name"]
        for sid in dict.fromkeys(substance_ids)
    }
    results = label_cache.fetch_concurrently(
        list(names), lambda sid: _label_text(sid, names[sid])
    )
    unavailable: set[str] = set()
    for sid, outcome in results.items():
        if isinstance(outcome, OpenFDAUnavailable):
            unavailable.add(sid)
        elif isinstance(outcome, Exception):
            raise outcome
    return unavailable


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _mentions(text: str, phrase: str) -> bool:
    words = re.findall(r"[a-z0-9]+", phrase.lower())
    if not words:
        return False
    pattern = r"\b" + r"[\W_]+".join(map(re.escape, words)) + r"(?:e?s)?\b"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _has_combination_context(sentence: str) -> bool:
    lowered = sentence.lower()
    return any(kw in lowered for kw in CONTEXT_KEYWORDS)


def _names_other_interacting_party(sentence: str, own_class: str) -> bool:
    terms = list(GENERIC_INTERACTING_PARTY_TERMS)
    for cls, keywords in CLASS_KEYWORDS.items():
        if cls != own_class:
            terms.extend(keywords)
            terms.extend(CLASS_PARTY_TERMS.get(cls, ()))
    return any(_mentions(sentence, term) for term in terms)


def _class_sentence_is_about_use_together(sentence: str, own_class: str) -> bool:
    if PRONOUN_REFERENCE_START.search(sentence.strip()):
        return False
    if not _has_combination_context(sentence):
        return False
    return not _names_other_interacting_party(sentence, own_class)


@dataclass(frozen=True)
class LabelMatch:
    sentence: str  # <=300 chars, verbatim
    matched_term: str  # the literal name/alias, or the class keyword, that hit
    is_literal: bool  # True: other_id's own name/alias appears in the sentence
    other_class: str | None  # the CLASS_MEMBERS key, only set when not literal
    section: dict | None = None


REJECTED_CLASS_MATCH: tuple[str, bool, str | None] = ("", False, None)


def _match_in_sentence(
    sentence: str, other_id: str, registry: Registry, shadow: dict[str, dict]
) -> tuple[str, bool, str | None] | None:
    """(matched_term, is_literal, class) for this one sentence, or None.

    A literal hit (the other drug's own name or alias) always wins. A class
    keyword ("NSAIDs") only counts when the sentence also reads like it's
    about combining drugs -- otherwise a passing mention (e.g. aspirin-allergy
    cross-reactivity) would falsely read as a combination warning. A
    non-registry other_id has no class membership (SUBSTANCE_CLASSES has no
    row for it), so it can only ever be matched by its own literal name here
    -- never an inferred class, since nothing is known about what class an
    unregistered drug belongs to.
    """
    entry = _entry(other_id, registry, shadow)
    for term in (entry["name"], *entry.get("aliases", [])):
        if _mentions(sentence, term):
            return term, True, None
    for cls in SUBSTANCE_CLASSES.get(other_id, ()):
        for term in CLASS_KEYWORDS.get(cls, ()):
            if _mentions(sentence, term):
                if _class_sentence_is_about_use_together(sentence, cls):
                    return term, False, cls
                return REJECTED_CLASS_MATCH
    return None


def _find_best_match(
    text: str, other_id: str, registry: Registry, shadow: dict[str, dict]
) -> tuple[LabelMatch | None, str | None]:
    """(match, rejection_reason). At most one match: the first literal
    name/alias hit if any sentence has one, else the first qualifying
    class-word hit. Literal always beats class. rejection_reason is "class"
    when a class keyword was found but no sentence passed the use-together
    check, and "unquotable" when a matching sentence carried a cross-reference
    or an unseparable section heading and so could not be quoted cleanly."""
    best: LabelMatch | None = None
    rejected: str | None = None
    for raw_sentence in _SENTENCE_SPLIT.split(text):
        sentence = raw_sentence.strip()
        if not sentence:
            continue
        body = separate_section_heading(sentence)
        unquotable = (
            body is None or has_cross_reference(body) or has_section_number_noise(body)
        )
        candidate = sentence if body is None else body
        found = _match_in_sentence(candidate, other_id, registry, shadow)
        if found is None:
            continue
        if unquotable:
            rejected = rejected or "unquotable"
            continue
        if found is REJECTED_CLASS_MATCH:
            rejected = rejected or "class"
            continue
        term, is_literal, cls = found
        match = LabelMatch(candidate[:300], term, is_literal, cls)
        if is_literal:
            return match, None
        if best is None:
            best = match
    return best, (rejected if best is None else None)


def label_mention(
    self_id: str,
    other_id: str,
    registry: Registry,
    unavailable: set[str] | None = None,
    shadow: dict[str, dict] | None = None,
) -> LabelMatch | None:
    return label_mention_detail(self_id, other_id, registry, unavailable, shadow)[0]


def label_mention_detail(
    self_id: str,
    other_id: str,
    registry: Registry,
    unavailable: set[str] | None = None,
    shadow: dict[str, dict] | None = None,
) -> tuple[LabelMatch | None, str | None]:
    """The best sentence in self_id's own FDA label mentioning other_id (by
    literal name/alias, preferred, or drug class in a combination-use
    sentence), or None if self_id has no label or doesn't mention it. Raises
    OpenFDAUnavailable, never interpreted as "no mention".

    self_id/other_id may be a registry substance id or a non-registry
    label-check key (resolve_for_label_check); `shadow` carries the
    synthetic entries for the latter -- omit it when both ids are real
    registry ids (every existing caller/test does).
    """
    if unavailable is not None and self_id in unavailable:
        raise OpenFDAUnavailable(self_id)
    shadow = shadow or {}
    entry = _entry(self_id, registry, shadow)
    text = _label_text(self_id, entry["name"])
    if text is None:
        return None, None
    match, rejected = _find_best_match(text, other_id, registry, shadow)
    if match is None:
        return None, rejected
    if not _mentions(match.sentence, match.matched_term):
        # Safety net: never show a quote that doesn't literally contain
        # whatever we claim matched it. Should be unreachable given how
        # `match` was built above; drop rather than trust it if it ever isn't.
        return None, None
    section = label_source.find_section(
        _sections_cache.get(entry["name"].upper(), []), match.sentence
    )
    return replace(match, section=section), None


# --- Concentration overlap (the only place this module touches app.pk) --------


def _mask(result) -> tuple[list[bool], list[str]] | None:
    """Per-point "above 10% of this substance's own peak in the window", or
    None when the substance has no curve or no presence in it at all."""
    if not result.modelable or result.peak is None:
        return None
    peak_c = result.peak["concentration"]
    if peak_c <= 0:
        return None
    threshold = 0.10 * peak_c
    return [p["concentration"] >= threshold for p in result.points], [
        p["t"] for p in result.points
    ]


def _fmt_time(iso: str) -> str:
    dt = datetime.fromisoformat(iso)
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"{hour}:{dt.strftime('%M %p')}"


def compute_overlap(
    sid_a: str,
    sid_b: str,
    registry: Registry,
    events: list[DoseEvent],
    now: datetime,
    patient: Patient,
) -> list[dict] | None:
    """Windows in the next 24h where both substances are above 10% of their
    own peak, from the engine (never guessed). None unless both substances
    have a full concentration curve; this is always labelled an estimate.
    """
    if registry.modelability(sid_a) != "full" or registry.modelability(sid_b) != "full":
        return None
    end = now + timedelta(hours=24)
    result_a = service.compute_curve(
        events, sid_a, registry, now, end, "15m", now, patient
    )
    result_b = service.compute_curve(
        events, sid_b, registry, now, end, "15m", now, patient
    )
    mask_a = _mask(result_a)
    mask_b = _mask(result_b)
    if mask_a is None or mask_b is None:
        return None
    bits_a, times = mask_a
    bits_b, _ = mask_b

    windows: list[tuple[str, str]] = []
    start_idx: int | None = None
    for i, (x, y) in enumerate(zip(bits_a, bits_b, strict=True)):
        both = x and y
        if both and start_idx is None:
            start_idx = i
        elif not both and start_idx is not None:
            windows.append((times[start_idx], times[i - 1]))
            start_idx = None
    if start_idx is not None:
        windows.append((times[start_idx], times[-1]))

    return [
        {
            "from": start,
            "to": stop,
            "estimate": True,
            "description": (
                f"Both present roughly {_fmt_time(start)} to {_fmt_time(stop)} "
                "(estimated from the concentration model)."
            ),
        }
        for start, stop in windows
    ] or None


# --- Regimen assembly ------------------------------------------------------------


@dataclass(frozen=True)
class RegimenSource:
    """Where a substance in the check came from, for traceability."""

    kind: str  # "dose" | "schedule" | "manual"
    label: str  # product name, schedule's product name, or the raw text typed
    dose_event_id: str | None = None
    product_id: str | None = None
    schedule_id: str | None = None
    taken_at: str | None = None


@dataclass(frozen=True)
class RegimenSubstance:
    substance_id: str
    sources: tuple[RegimenSource, ...]


def _merge(entries: list[tuple[str, RegimenSource]]) -> list[RegimenSubstance]:
    by_id: dict[str, list[RegimenSource]] = {}
    for sid, source in entries:
        by_id.setdefault(sid, []).append(source)
    return [
        RegimenSubstance(sid, tuple(sources)) for sid, sources in sorted(by_id.items())
    ]


# --- Findings ----------------------------------------------------------------


def _substance_ref(entry: dict, rs: RegimenSubstance) -> dict:
    return {
        "id": entry["id"],
        "display_name": entry["name"],
        "description": entry.get("description"),
        "sources": [
            {
                "kind": s.kind,
                "label": s.label,
                "dose_event_id": s.dose_event_id,
                "product_id": s.product_id,
                "schedule_id": s.schedule_id,
                "taken_at": s.taken_at,
            }
            for s in rs.sources
        ],
    }


def _also_mentioned(self_entry: dict, match: LabelMatch) -> dict:
    """A literal label mention folded into a curated finding's card, rather
    than a separate finding -- the curated rule is already the primary claim
    for this pair; the label is corroboration, not a second interaction."""
    return {
        "source": f"FDA label, {self_entry['name']}, Drug Interactions section",
        "quoted_text": match.sentence,
        "quote_source": QUOTE_SOURCE_FDA_LABEL,
        "message": f"Also mentioned in the FDA label for {self_entry['name']}.",
        **label_source.source_fields(match.section),
    }


def _curated_finding(
    entry_a, entry_b, a, b, rule: CuratedRule, overlap, also_mentioned_in_label
) -> dict:
    return {
        "substance_a": _substance_ref(entry_a, a),
        "substance_b": _substance_ref(entry_b, b),
        "effect": rule.effect,
        "recommendation": rule.recommendation,
        "source_type": "curated",
        "source": rule.source,
        "quoted_text": None,
        "quote_source": None,
        "verified": rule.verified,
        "severity": None,  # never invented; no curated rule states one yet
        "overlap": overlap,
        "message": f"{entry_a['name']} + {entry_b['name']}: {rule.effect}",
        # Literal (never class-only) label mentions of the same pair, shown as
        # corroboration beneath the primary curated card. None when there are
        # none, never an empty list.
        "also_mentioned_in_label": also_mentioned_in_label or None,
        **label_source.source_fields(None),
    }


def _label_finding(
    self_entry, other_entry, self_rs, other_rs, match: LabelMatch, overlap
) -> dict:
    if match.is_literal:
        message = (
            f"The FDA label for {self_entry['name']} mentions {other_entry['name']} "
            "in its drug interactions section."
        )
    else:
        class_display = CLASS_DISPLAY.get(match.other_class, match.other_class)
        message = (
            f"The FDA label for {self_entry['name']} mentions the drug class "
            f"{class_display} in its drug interactions section. "
            f"{other_entry['name']} belongs to this class."
        )
    return {
        "substance_a": _substance_ref(self_entry, self_rs),
        "substance_b": _substance_ref(other_entry, other_rs),
        "effect": None,
        "recommendation": None,
        "source_type": "fda_label",
        "source": f"FDA label, {self_entry['name']}, Drug Interactions section",
        "quoted_text": match.sentence,
        "quote_source": QUOTE_SOURCE_FDA_LABEL,
        "verified": False,
        "severity": None,  # never interpreted from label prose
        "overlap": overlap,
        "message": message,
        "also_mentioned_in_label": None,
        **label_source.source_fields(match.section),
    }


def _rejected_note(self_entry: dict, other_entry: dict, reason: str) -> str:
    if reason == "unquotable":
        return (
            f"The FDA label for {self_entry['name']} mentions "
            f"{other_entry['name']} in a sentence we could not quote cleanly, "
            "so it was not shown as an interaction."
        )
    return (
        f"The FDA label for {self_entry['name']} mentions a drug class that "
        f"{other_entry['name']} belongs to, but not in a sentence about using "
        "the two together, so it was not shown as an interaction."
    )


def check(
    substances: list[RegimenSubstance],
    registry: Registry,
    now: datetime,
    events: list[DoseEvent] | None = None,
    patient: Patient = service.DEFAULT_PATIENT,
    unresolved: list[str] = (),  # noqa: B006 (never mutated)
    shadow: dict[str, dict] | None = None,
) -> dict:
    """Every pair of distinct substances against both sources.

    `events` enables the concentration-overlap estimate (GET .../interaction-check,
    where doses are actually logged); it's None for the manual check, where
    nothing has necessarily been taken yet and no timing exists to estimate.
    `unresolved` names ingredients the caller couldn't map to anything at
    all (not a registry id, not even a label-checkable generic name) --
    always surfaced in `unchecked_reason`, never dropped silently.
    `shadow` carries the synthetic entries for any non-registry substance
    (resolve_for_label_check) among `substances`.
    """
    shadow = shadow or {}
    rules = active_rules()
    findings: list[dict] = []
    label_unavailable = False
    rejected_notes: list[str] = []
    checked_pairs = 0
    unavailable = _prefetch_label_texts(
        [s.substance_id for s in substances], registry, shadow
    )

    # Fetch each substance's own label exactly once, before any pairwise
    # work -- "cache label fetches, one fetch per ingredient, then check
    # pairs in memory." _label_text already caches by key, so this pass
    # adds no extra fetches for substances that DO get paired below; its
    # purpose is to also catch a non-registry substance with no partner to
    # pair against at all (no curated rules ever apply to one, so a label
    # is the only possible source -- if it has none, that's reportable
    # even standing alone).
    no_label_names: list[str] = []
    for s in substances:
        entry = _entry(s.substance_id, registry, shadow)
        if s.substance_id in unavailable:
            label_unavailable = True
            continue
        text = _label_text(s.substance_id, entry["name"])
        if text is None and not _is_registry_key(s.substance_id):
            no_label_names.append(entry["name"])

    for a, b in combinations(sorted(substances, key=lambda s: s.substance_id), 2):
        checked_pairs += 1
        entry_a = _entry(a.substance_id, registry, shadow)
        entry_b = _entry(b.substance_id, registry, shadow)

        overlap = None
        if (
            events is not None
            and _is_registry_key(a.substance_id)
            and _is_registry_key(b.substance_id)
        ):
            overlap = compute_overlap(
                a.substance_id, b.substance_id, registry, events, now, patient
            )

        curated_matches = [
            rule for rule in rules if rule.matches(a.substance_id, b.substance_id)
        ]

        match_ab = match_ba = None
        try:
            match_ab, rejected_ab = label_mention_detail(
                a.substance_id, b.substance_id, registry, unavailable, shadow
            )
            if rejected_ab:
                rejected_notes.append(_rejected_note(entry_a, entry_b, rejected_ab))
        except OpenFDAUnavailable:
            label_unavailable = True
        try:
            match_ba, rejected_ba = label_mention_detail(
                b.substance_id, a.substance_id, registry, unavailable, shadow
            )
            if rejected_ba:
                rejected_notes.append(_rejected_note(entry_b, entry_a, rejected_ba))
        except OpenFDAUnavailable:
            label_unavailable = True

        if curated_matches:
            # A curated rule is the primary claim for this pair. A literal
            # label mention corroborates it beneath the same card; a
            # class-only match adds nothing a human hasn't already reviewed
            # for this exact pair, so it's dropped rather than shown twice.
            also_mentioned = []
            if match_ab is not None and match_ab.is_literal:
                also_mentioned.append(_also_mentioned(entry_a, match_ab))
            if match_ba is not None and match_ba.is_literal:
                also_mentioned.append(_also_mentioned(entry_b, match_ba))
            for rule in curated_matches:
                findings.append(
                    _curated_finding(
                        entry_a, entry_b, a, b, rule, overlap, also_mentioned
                    )
                )
        else:
            if match_ab is not None:
                findings.append(
                    _label_finding(entry_a, entry_b, a, b, match_ab, overlap)
                )
            if match_ba is not None:
                findings.append(
                    _label_finding(entry_b, entry_a, b, a, match_ba, overlap)
                )

    unchecked_reason = [
        f"{name!r} wasn't recognized, so interactions involving it "
        "could not be checked."
        for name in unresolved
    ]
    unchecked_reason += [
        f"No label found for {name!r}, so interactions involving it "
        "could not be checked from its label."
        for name in no_label_names
    ]
    unchecked_reason += rejected_notes
    if label_unavailable:
        unchecked_reason.append(LABEL_UNAVAILABLE_NOTE)

    if checked_pairs == 0:
        message = NO_PAIRS_MESSAGE
    elif not findings:
        message = EMPTY_MESSAGE
    else:
        message = None

    body = {
        "interactions": findings,
        "checked_pairs": checked_pairs,
        "unchecked_reason": unchecked_reason,
        "message": message,
    }
    return verification.attach_notice(body, unverified_rule_count())


# --- "Same ingredient in more than one product" ---------------------------------
#
# A lookup over the regimen's own products, never a model: no severity, no
# harm claim, and this never creates or feeds an app.detect alert -- those
# stay tied to logged doses and app/detect/alerts.py's own duplication
# detector (which works in mg, off DoseIngredient.amount_mg, not this).


def _duplicate_ingredient_findings(
    occurrences: list[tuple[str, RegimenSource, bool]],
    registry: Registry,
    shadow: dict[str, dict],
) -> list[dict]:
    """A card per ingredient key that shows up in 2+ distinct products in
    the regimen. The same product scheduled (or logged) more than once is
    one product, not a duplicate -- occurrences are deduped by product_id
    first; when a product shows up both scheduled and logged, "logged" wins
    for display (it's the more concrete fact)."""
    # key -> product_id -> (best source seen for that product, strength known)
    by_key: dict[str, dict[str, tuple[RegimenSource, bool]]] = {}
    for key, source, strength_known in occurrences:
        products = by_key.setdefault(key, {})
        existing = products.get(source.product_id)
        if existing is None:
            products[source.product_id] = (source, strength_known)
        else:
            existing_source, existing_known = existing
            best = (
                source
                if existing_source.kind == "schedule" and source.kind == "dose"
                else existing_source
            )
            products[source.product_id] = (best, existing_known or strength_known)

    findings: list[dict] = []
    for key, products_by_id in by_key.items():
        if len(products_by_id) < 2:
            continue
        entry = _entry(key, registry, shadow)
        display_name = entry["name"].capitalize()
        by_name = sorted(products_by_id.values(), key=lambda p: p[0].label)
        names = [source.label for source, _known in by_name]
        findings.append(
            {
                "ingredient_name": display_name,
                "product_count": len(by_name),
                "products": [
                    {
                        "product_id": source.product_id,
                        "name": source.label,
                        "kind": source.kind,  # "dose" (logged) | "schedule"
                    }
                    for source, _known in by_name
                ],
                "message": (
                    f"{display_name} appears in {len(by_name)} of your "
                    f"products: {', '.join(names)}. Ask a pharmacist to "
                    "confirm this is intended."
                ),
                "strength_note": (
                    None
                    if all(known for _source, known in by_name)
                    else (
                        "The strength of each product isn't known here, so "
                        "we can't add up the total amount."
                    )
                ),
            }
        )
    return sorted(findings, key=lambda f: f["ingredient_name"])


# --- Entry points: regimen (GET) and manual (POST) -----------------------------

# Same lookback as app/api/skincare.py's auto-check: doses logged this
# recently plus every active schedule make up "the regimen" to check.
REGIMEN_LOOKBACK_HOURS = 24.0


def check_regimen(
    session: Session,
    user_id: str,
    registry: Registry,
    now: datetime,
    patient: Patient = service.DEFAULT_PATIENT,
) -> dict:
    """Active scheduled medications plus doses logged in the last 24h --
    oral medication products only. A skincare/cosmetic product (its own
    topical_ingredients is set -- a curated skincare entry, a My products
    entry, or any openFDA product whose own label route is TOPICAL) is
    skipped entirely: not checked, and its ingredients never show up as
    "wasn't recognized" either, since this checker's whole domain is
    systemic medications.
    """
    events = service.load_doses(session, user_id)
    entries: list[tuple[str, RegimenSource]] = []
    unresolved: list[str] = []
    shadow: dict[str, dict] = {}
    # Parallel to `entries`: (key, source, strength_known) for every
    # successfully-identified ingredient, regardless of registry vs.
    # label-only -- feeds _duplicate_ingredient_findings below.
    occurrences: list[tuple[str, RegimenSource, bool]] = []

    for ev in events:
        if ev.topical_ingredients:
            continue
        taken_at = datetime.fromisoformat(ev.taken_at)
        if not (-REGIMEN_LOOKBACK_HOURS <= service.hours_since(taken_at, now) <= 0.0):
            continue
        for ing in ev.ingredients:
            source = RegimenSource(
                kind="dose",
                label=ev.product_name,
                dose_event_id=ev.id,
                product_id=ev.product_id,
                taken_at=ev.taken_at,
            )
            if ing.substance_id is not None:
                entries.append((ing.substance_id, source))
                occurrences.append(
                    (ing.substance_id, source, ing.amount_mg is not None)
                )
                continue
            resolved = resolve_for_label_check(ing.ingredient_name, registry)
            if resolved is None:
                unresolved.append(ing.ingredient_name)
                continue
            key, entry = resolved
            shadow[key] = entry
            entries.append((key, source))
            occurrences.append((key, source, ing.amount_mg is not None))

    for s in schedule.list_for_user(session, user_id):
        if s.topical_ingredients:
            continue
        for e in s.substance_entries:
            source = RegimenSource(
                kind="schedule",
                label=s.product_name,
                product_id=s.product_id,
                schedule_id=s.id,
            )
            if e["substance_id"] is not None:
                entries.append((e["substance_id"], source))
                occurrences.append(
                    (e["substance_id"], source, e.get("amount_mg") is not None)
                )
                continue
            resolved = resolve_for_label_check(e["name"], registry)
            if resolved is None:
                unresolved.append(e["name"])
                continue
            key, entry = resolved
            shadow[key] = entry
            entries.append((key, source))
            occurrences.append((key, source, e.get("amount_mg") is not None))

    substances = _merge(entries)
    result = check(
        substances,
        registry,
        now,
        events=events,
        patient=patient,
        unresolved=unresolved,
        shadow=shadow,
    )
    result["duplicate_ingredients"] = _duplicate_ingredient_findings(
        occurrences, registry, shadow
    )
    return result


def check_manual(names: list[str], registry: Registry, now: datetime) -> dict:
    """A user-typed list of substance or product names, checked against each
    other -- for someone considering a new medication before starting it.
    No dose events exist for these, so no concentration-overlap estimate.

    The synthetic catalog (app/api/products.py) carries oral OTC medications
    only, so a product match here is always oral; there is nothing
    skincare/cosmetic to exclude in this path (unlike check_regimen, which
    reads back real logged products, including topical ones).
    """
    entries: list[tuple[str, RegimenSource]] = []
    unresolved: list[str] = []
    shadow: dict[str, dict] = {}

    for name in names:
        source = RegimenSource(kind="manual", label=name)
        substance_id = registry.resolve(name)
        if substance_id is not None:
            entries.append((substance_id, source))
            continue
        matched_product = next(iter(products.search(name, registry)), None)
        if matched_product is None:
            resolved = resolve_for_label_check(name, registry)
            if resolved is None:
                unresolved.append(name)
                continue
            key, entry = resolved
            shadow[key] = entry
            entries.append((key, source))
            continue
        found_any = False
        for ing in products.decompose(matched_product, registry):
            if ing.substance_id is not None:
                entries.append((ing.substance_id, source))
                found_any = True
                continue
            resolved = resolve_for_label_check(ing.name, registry)
            if resolved is not None:
                key, entry = resolved
                shadow[key] = entry
                entries.append((key, source))
                found_any = True
        if not found_any:
            unresolved.append(name)

    substances = _merge(entries)
    result = check(
        substances, registry, now, events=None, unresolved=unresolved, shadow=shadow
    )
    # No product identity exists for typed names (no dose/schedule to be
    # "the same product" or not), so the duplicate-ingredient-across-
    # products card -- scoped to the logged/scheduled regimen -- never
    # applies here; included for a consistent response shape.
    result["duplicate_ingredients"] = []
    return result
