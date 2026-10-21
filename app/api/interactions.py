"""Medication interaction checker: two traceable, cited sources, never a model.

Like app/api/skincare.py, this is deliberately NOT pharmacokinetics by
default -- it's a lookup, not a prediction. There are two sources:

  A) Curated rules (app/data/drug_interactions.json). Each entry cites a
     specific FDA label section; a rule with no source doesn't belong there
     (see the file's own `_README`), and `load_rules` refuses to load one
     that lacks one. Every rule is `verified: false` until a human opens the
     cited source (CLAUDE.md invariant) -- never flip that here.

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
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from itertools import combinations
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from ..pk.individualize import Patient
from ..registry.loader import Registry
from . import label_cache, products, schedule, service
from .db import DoseEvent
from .provenance import QUOTE_SOURCE_FDA_LABEL

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
    "concomitantly",
    "combination",
    "coadminister",
    "co-administer",
    "administered together",
    "other nsaid",
    "with another",
)

SUBSTANCE_CLASSES: dict[str, tuple[str, ...]] = {}
for _cls, _members in CLASS_MEMBERS.items():
    for _sid in _members:
        SUBSTANCE_CLASSES[_sid] = (*SUBSTANCE_CLASSES.get(_sid, ()), _cls)

EMPTY_MESSAGE = (
    "No documented interactions found in our sources. This does not mean "
    "none exist. Ask a pharmacist to review your full medication list."
)

LABEL_UNAVAILABLE_NOTE = (
    "openFDA was unreachable, so FDA label checks could not run for some "
    "pairs. Curated reference checks still ran."
)

# Replaced with a mock-transport client in tests.
_http = httpx.Client(timeout=5.0)

# substance_id -> drug_interactions label text, or None if no label has one.
_label_cache: dict[str, str | None] = {}


class OpenFDAUnavailable(Exception):
    """openFDA could not be reached or answered with an error."""


class InvalidRule(ValueError):
    """A curated rule is missing a required, cited source."""


def clear_cache() -> None:
    _label_cache.clear()


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
        rules.append(
            CuratedRule(
                substance_a=e["substance_a"],
                substance_b=e["substance_b"],
                effect=e["effect"],
                recommendation=e["recommendation"],
                source=source,
                verified=e.get("verified", False),
            )
        )
    return tuple(rules)


# --- B. FDA label text -----------------------------------------------------------


def fetch_drug_interactions_text(generic_name: str) -> str | None:
    return label_cache.cached(
        "drug_interactions",
        generic_name.upper(),
        lambda: _request_drug_interactions_text(generic_name),
    )


def _request_drug_interactions_text(generic_name: str) -> str | None:
    """`drug_interactions` text from single-ingredient openFDA labels, or None.

    Raises OpenFDAUnavailable on network or server errors, so the caller can
    tell "no label" apart from "couldn't ask" (same contract as
    app/api/side_effects.py's fetch_adverse_reactions).
    """
    search = (
        f'openfda.generic_name.exact:"{generic_name.upper()}"'
        " AND _exists_:drug_interactions"
    )
    try:
        r = _http.get(
            OPENFDA_LABEL_URL, params={"search": search, "limit": LABEL_LIMIT}
        )
    except httpx.HTTPError as exc:
        raise OpenFDAUnavailable(str(exc)) from exc
    if r.status_code == 404:  # openFDA's "no matches"
        return None
    if r.status_code != 200:
        raise OpenFDAUnavailable(f"openFDA returned {r.status_code}")
    sections = [
        text
        for label in r.json().get("results", [])
        for text in label.get("drug_interactions", [])
    ]
    return "\n".join(sections) or None


def _label_text(substance_id: str, generic_name: str) -> str | None:
    """Cached per substance. Not cached on failure, so the next call retries."""
    if substance_id in _label_cache:
        return _label_cache[substance_id]
    text = fetch_drug_interactions_text(generic_name)  # may raise
    _label_cache[substance_id] = text
    return text


def _prefetch_label_texts(substance_ids: list[str], registry: Registry) -> set[str]:
    names = {sid: registry.get(sid)["name"] for sid in dict.fromkeys(substance_ids)}
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


@dataclass(frozen=True)
class LabelMatch:
    sentence: str  # <=300 chars, verbatim
    matched_term: str  # the literal name/alias, or the class keyword, that hit
    is_literal: bool  # True: other_id's own name/alias appears in the sentence
    other_class: str | None  # the CLASS_MEMBERS key, only set when not literal


def _match_in_sentence(
    sentence: str, other_id: str, registry: Registry
) -> tuple[str, bool, str | None] | None:
    """(matched_term, is_literal, class) for this one sentence, or None.

    A literal hit (the other drug's own name or alias) always wins. A class
    keyword ("NSAIDs") only counts when the sentence also reads like it's
    about combining drugs -- otherwise a passing mention (e.g. aspirin-allergy
    cross-reactivity) would falsely read as a combination warning.
    """
    entry = registry.get(other_id)
    for term in (entry["name"], *entry.get("aliases", [])):
        if _mentions(sentence, term):
            return term, True, None
    if _has_combination_context(sentence):
        for cls in SUBSTANCE_CLASSES.get(other_id, ()):
            for term in CLASS_KEYWORDS.get(cls, ()):
                if _mentions(sentence, term):
                    return term, False, cls
    return None


def _find_best_match(text: str, other_id: str, registry: Registry) -> LabelMatch | None:
    """At most one match: the first literal name/alias hit if any sentence has
    one, else the first qualifying class-word hit. Literal always beats class."""
    best: LabelMatch | None = None
    for raw_sentence in _SENTENCE_SPLIT.split(text):
        sentence = raw_sentence.strip()
        if not sentence:
            continue
        found = _match_in_sentence(sentence, other_id, registry)
        if found is None:
            continue
        term, is_literal, cls = found
        match = LabelMatch(sentence[:300], term, is_literal, cls)
        if is_literal:
            return match  # literal wins outright; stop looking
        if best is None:
            best = match
    return best


def label_mention(
    self_id: str,
    other_id: str,
    registry: Registry,
    unavailable: set[str] | None = None,
) -> LabelMatch | None:
    """The best sentence in self_id's own FDA label mentioning other_id (by
    literal name/alias, preferred, or drug class in a combination-use
    sentence), or None if self_id has no label or doesn't mention it. Raises
    OpenFDAUnavailable, never interpreted as "no mention".
    """
    if unavailable is not None and self_id in unavailable:
        raise OpenFDAUnavailable(self_id)
    entry = registry.get(self_id)
    text = _label_text(self_id, entry["name"])
    if text is None:
        return None
    match = _find_best_match(text, other_id, registry)
    if match is None:
        return None
    if not _mentions(match.sentence, match.matched_term):
        # Safety net: never show a quote that doesn't literally contain
        # whatever we claim matched it. Should be unreachable given how
        # `match` was built above; drop rather than trust it if it ever isn't.
        return None
    return match


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
    }


def check(
    substances: list[RegimenSubstance],
    registry: Registry,
    now: datetime,
    events: list[DoseEvent] | None = None,
    patient: Patient = service.DEFAULT_PATIENT,
    unresolved: list[str] = (),  # noqa: B006 (never mutated)
) -> dict:
    """Every pair of distinct substances against both sources.

    `events` enables the concentration-overlap estimate (GET .../interaction-check,
    where doses are actually logged); it's None for the manual check, where
    nothing has necessarily been taken yet and no timing exists to estimate.
    `unresolved` names ingredients the caller couldn't map to a registry id --
    always surfaced in `unchecked_reason`, never dropped silently.
    """
    rules = load_rules()
    findings: list[dict] = []
    label_unavailable = False
    checked_pairs = 0
    unavailable = _prefetch_label_texts([s.substance_id for s in substances], registry)

    for a, b in combinations(sorted(substances, key=lambda s: s.substance_id), 2):
        checked_pairs += 1
        entry_a, entry_b = registry.get(a.substance_id), registry.get(b.substance_id)

        overlap = None
        if events is not None:
            overlap = compute_overlap(
                a.substance_id, b.substance_id, registry, events, now, patient
            )

        curated_matches = [
            rule for rule in rules if rule.matches(a.substance_id, b.substance_id)
        ]

        match_ab = match_ba = None
        try:
            match_ab = label_mention(
                a.substance_id, b.substance_id, registry, unavailable
            )
        except OpenFDAUnavailable:
            label_unavailable = True
        try:
            match_ba = label_mention(
                b.substance_id, a.substance_id, registry, unavailable
            )
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
    if label_unavailable:
        unchecked_reason.append(LABEL_UNAVAILABLE_NOTE)

    return {
        "interactions": findings,
        "checked_pairs": checked_pairs,
        "unchecked_reason": unchecked_reason,
        "message": EMPTY_MESSAGE if not findings else None,
    }


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
    """Active scheduled medications plus doses logged in the last 24h."""
    events = service.load_doses(session, user_id)
    entries: list[tuple[str, RegimenSource]] = []
    unresolved: list[str] = []

    for ev in events:
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
            else:
                unresolved.append(ing.ingredient_name)

    for s in schedule.list_for_user(session, user_id):
        for e in s.substance_entries:
            source = RegimenSource(
                kind="schedule",
                label=s.product_name,
                product_id=s.product_id,
                schedule_id=s.id,
            )
            if e["substance_id"] is not None:
                entries.append((e["substance_id"], source))
            else:
                unresolved.append(e["name"])

    substances = _merge(entries)
    return check(
        substances, registry, now, events=events, patient=patient, unresolved=unresolved
    )


def check_manual(names: list[str], registry: Registry, now: datetime) -> dict:
    """A user-typed list of substance or product names, checked against each
    other -- for someone considering a new medication before starting it.
    No dose events exist for these, so no concentration-overlap estimate.
    """
    entries: list[tuple[str, RegimenSource]] = []
    unresolved: list[str] = []

    for name in names:
        source = RegimenSource(kind="manual", label=name)
        substance_id = registry.resolve(name)
        if substance_id is not None:
            entries.append((substance_id, source))
            continue
        matched_product = next(iter(products.search(name, registry)), None)
        if matched_product is None:
            unresolved.append(name)
            continue
        found_any = False
        for ing in products.decompose(matched_product, registry):
            if ing.substance_id is not None:
                entries.append((ing.substance_id, source))
                found_any = True
        if not found_any:
            unresolved.append(name)

    substances = _merge(entries)
    return check(substances, registry, now, events=None, unresolved=unresolved)
