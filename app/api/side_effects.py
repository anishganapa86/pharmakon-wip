"""Symptom check: is a substance the user has at an elevated level associated
with the symptom they typed?

A substance is reported only when all three hold:

  a) the symptom appears in its FDA label's adverse_reactions section, or in
     the registry's local `common_side_effects` list when openFDA has no label;
  b) the user logged it in the last 24 hours;
  c) its 24-hour total, or its current concentration where the registry has a
     published concentration threshold, is at or above APPROACH_FRACTION of
     the limit.

This reports an association, never a cause. A label listing means the event
was observed in people taking the drug, not that the drug produced it, so the
messages say "listed as an adverse reaction" and name where the association
came from. Silence is the default: anything short of all three conditions
returns no match.

Pharmakon is not a medical device. Levels come from a one-compartment,
population-average model, for educational use only.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime

import httpx
import numpy as np
from sqlalchemy.orm import Session

from ..detect.alerts import APPROACH_FRACTION, HEPATIC_THRESHOLD_FACTOR
from ..pk.engine import UnsupportedKinetics, superpose
from ..pk.individualize import Patient, adjusted_ke
from ..registry.loader import Registry
from .service import DEFAULT_PATIENT, load_doses, logged_doses

OPENFDA_LABEL_URL = "https://api.fda.gov/drug/label.json"

# Labels fetched per substance. Several, because one manufacturer's label may
# omit a reaction another lists; all are single-ingredient (see below).
LABEL_LIMIT = 5

OPENFDA_SOURCE = "openfda_label"
LOCAL_SOURCE = "local_reference"
SOURCE_LABELS = {
    OPENFDA_SOURCE: "FDA drug label",
    LOCAL_SOURCE: "local reference (unverified)",
}

NOTHING_FOUND = (
    "None of your current substances at elevated levels are associated with "
    "this symptom."
)

# Replaced with a mock-transport client in tests.
_http = httpx.Client(timeout=5.0)

# (substance_id, normalized symptom) -> source, or None for "not associated".
_cache: dict[tuple[str, str], str | None] = {}


class OpenFDAUnavailable(Exception):
    """openFDA could not be reached or answered with an error."""


def clear_cache() -> None:
    _cache.clear()


def normalize_symptom(symptom: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", symptom.lower()))


def mentions(text: str, symptom: str) -> bool:
    """Case-insensitive phrase match on word boundaries.

    "headache" matches "Headache, dizziness, and nausea" and "headaches", and
    words may be split by any whitespace or punctuation. It does not match
    inside another word: "rash" is not found in "crash".
    """
    words = normalize_symptom(symptom).split()
    if not words:
        return False
    pattern = r"\b" + r"[\W_]+".join(map(re.escape, words)) + r"(?:e?s)?\b"
    return re.search(pattern, text, re.IGNORECASE) is not None


def fetch_adverse_reactions(generic_name: str) -> str | None:
    """adverse_reactions text from single-ingredient openFDA labels.

    Matches on the exact generic name so a combination product's label (whose
    reactions may belong to another ingredient) is never attributed to this
    substance. Returns None when no such label has an adverse_reactions
    section. Raises OpenFDAUnavailable on network or server errors, so the
    caller can tell "no label" apart from "couldn't ask".
    """
    search = (
        f'openfda.generic_name.exact:"{generic_name.upper()}"'
        " AND _exists_:adverse_reactions"
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
        for text in label.get("adverse_reactions", [])
    ]
    return "\n".join(sections) or None


def association_source(entry: dict, symptom: str) -> str | None:
    """Where the symptom is listed for this substance, or None if nowhere.

    openFDA first. The local list is consulted only when openFDA has no label
    for the substance or can't be reached: a label that exists and doesn't
    list the symptom is the answer, not a gap to fill.
    """
    key = (entry["id"], normalize_symptom(symptom))
    if key in _cache:
        return _cache[key]

    def local() -> str | None:
        listed = entry.get("common_side_effects", [])
        return LOCAL_SOURCE if any(mentions(s, symptom) for s in listed) else None

    try:
        text = fetch_adverse_reactions(entry["name"])
    except OpenFDAUnavailable:
        # Not cached: the next check should try openFDA again.
        return local()

    if text is None:
        result = local()
    else:
        result = OPENFDA_SOURCE if mentions(text, symptom) else None
    _cache[key] = result
    return result


def check_symptom(
    session: Session,
    user_id: str,
    registry: Registry,
    now: datetime,
    symptom: str,
    patient: Patient = DEFAULT_PATIENT,
) -> dict:
    events = load_doses(session, user_id)
    by_id = {ev.id: ev for ev in events}
    doses = logged_doses(events, origin=now)  # t_hours <= 0 is the past

    recent: dict[str, list] = defaultdict(list)
    for d in doses:
        if -24.0 <= d.t_hours <= 0.0:
            recent[d.substance_id].append(d)

    matches = []
    for sid in sorted(recent):
        entry = registry.get(sid)
        level = _level(entry, registry, doses, recent[sid], patient)
        # Conditions (b) and (c) first: openFDA is only asked about substances
        # already at an elevated level.
        if level is None:
            continue
        source = association_source(entry, symptom)
        if source is None:
            continue
        matches.append(
            _match(entry, symptom.strip(), source, level, recent[sid], by_id)
        )

    if not matches:
        return {"matches": [], "message": NOTHING_FOUND}
    return {
        "matches": matches,
        "message": (
            "These substances you took in the last 24 hours are at elevated "
            "levels and list this symptom as an adverse reaction. That is an "
            "association, not a diagnosis. Talk to a pharmacist or your "
            "prescriber."
        ),
    }


def known_side_effect_vocabulary(registry: Registry) -> list[str]:
    """Every side effect documented for any substance in the registry, deduped.

    This is the reverse of check_symptom's search: instead of one typed
    symptom checked against every substance, every substance's documented
    `common_side_effects` (across the whole registry, not just what's logged)
    is checked against each logged substance's own label. This is what lets a
    label mention that isn't in one substance's own curated local list (e.g.
    caffeine's FDA label naming nausea) still surface with its true source,
    without ever parsing free-text label prose into ad hoc symptom words.
    """
    seen: set[str] = set()
    out = []
    for e in registry.all():
        for effect in e.get("common_side_effects", []):
            key = normalize_symptom(effect)
            if key not in seen:
                seen.add(key)
                out.append(effect)
    return out


def side_effect_profile(
    session: Session,
    user_id: str,
    registry: Registry,
    now: datetime,
    patient: Patient = DEFAULT_PATIENT,
) -> dict:
    """Every documented side effect of every substance logged in the last 24
    hours, grouped by substance, with which products contributed each one and
    the substance's current level. Side effects that are documented for more
    than one logged substance are called out in `shared_effects` so the
    client can highlight where they compound.
    """
    events = load_doses(session, user_id)
    by_id = {ev.id: ev for ev in events}
    doses = logged_doses(events, origin=now)

    recent: dict[str, list] = defaultdict(list)
    for d in doses:
        if -24.0 <= d.t_hours <= 0.0:
            recent[d.substance_id].append(d)

    if not recent:
        return {"substances": [], "shared_effects": []}

    vocabulary = known_side_effect_vocabulary(registry)
    substances = []
    effect_names: dict[str, str] = {}
    effect_counts: dict[str, int] = defaultdict(int)

    for sid in sorted(recent):
        entry = registry.get(sid)
        level = _compute_level(entry, registry, doses, recent[sid], patient)

        side_effects_found = []
        for effect in vocabulary:
            source = association_source(entry, effect)
            if source is None:
                continue
            side_effects_found.append(
                {
                    "effect": effect,
                    "source": source,
                    "source_label": SOURCE_LABELS[source],
                }
            )
            key = normalize_symptom(effect)
            effect_names.setdefault(key, effect)
            effect_counts[key] += 1

        products: dict[str, str] = {}
        for d in recent[sid]:
            ev = by_id[d.dose_id]
            products[ev.product_id] = ev.product_name

        substances.append(
            {
                "substance": {
                    "id": entry["id"],
                    "display_name": entry["name"],
                    "description": entry.get("description"),
                },
                "current_level_mg_l": level["current_level_mg_l"],
                "total_24h_mg": level["total_24h_mg"],
                "threshold_24h_mg": level["threshold_24h_mg"],
                "side_effects": side_effects_found,
                "contributing_products": [
                    {"product_id": pid, "product_name": name}
                    for pid, name in products.items()
                ],
            }
        )

    shared_effects = sorted(
        effect_names[key] for key, count in effect_counts.items() if count > 1
    )
    return {"substances": substances, "shared_effects": shared_effects}


def _compute_level(entry, registry, all_doses, recent, patient) -> dict:
    """The substance's level against its limits. `threshold_status` is None
    when there's nothing to compare against or the level is below the
    approach fraction -- unlike `_level`, this never hides the level itself.
    """
    sid = entry["id"]
    total = sum(d.amount_mg for d in recent)

    limit = entry.get("threshold_24h_mg")
    if limit is not None and entry.get("clearance_route") in ("hepatic", "mixed"):
        limit *= HEPATIC_THRESHOLD_FACTOR[patient.hepatic_impairment]

    current = None
    params = registry.pk_params(sid)
    try:
        params.check_modelable()
    except UnsupportedKinetics:
        pass  # no curve, so no current level; the 24h total still applies
    else:
        # Only a full curve is in mg/L; an approximate one has no level to
        # compare against a limit, so `current` stays None for it.
        if registry.modelability(sid) == "full":
            ke, _ = adjusted_ke(params, patient)
            own = [d.as_dose() for d in all_doses if d.substance_id == sid]
            current = float(
                superpose(own, params, patient.weight_kg, np.array([0.0]), ke=ke)[0]
            )

    conc_limit = registry.threshold_concentration_mg_per_l(sid)

    ratios = []
    if limit:
        ratios.append(total / limit)
    if conc_limit and current is not None:
        ratios.append(current / conc_limit)

    status = None
    if ratios and max(ratios) >= APPROACH_FRACTION:
        status = "above" if max(ratios) >= 1.0 else "approaching"

    return {
        "total_24h_mg": total,
        "threshold_24h_mg": limit,
        "current_level_mg_l": current,
        "threshold_concentration_mg_per_l": conc_limit,
        "threshold_status": status,
    }


def _level(entry, registry, all_doses, recent, patient) -> dict | None:
    """The substance's level against its limits, or None if not elevated."""
    level = _compute_level(entry, registry, all_doses, recent, patient)
    return level if level["threshold_status"] is not None else None


def _match(entry, symptom, source, level, recent, by_id) -> dict:
    status = level["threshold_status"]
    detail = (
        f" ({level['total_24h_mg']:.0f} of {level['threshold_24h_mg']:.0f} mg "
        "in 24 hours)"
        if level["threshold_24h_mg"]
        else ""
    )
    message = (
        f"{symptom[:1].upper()}{symptom[1:]} is listed as an adverse reaction "
        f"of {entry['name']}, and your current level is {status} "
        f"the recommended limit{detail}. "
        f"Source: {SOURCE_LABELS[source]}."
    )
    contributing = []
    for dose_id in dict.fromkeys(d.dose_id for d in recent):
        ev = by_id[dose_id]
        contributing.append(
            {
                "dose_event_id": ev.id,
                "product_id": ev.product_id,
                "product_name": ev.product_name,
                "taken_at": ev.taken_at,
                "amount_mg": sum(d.amount_mg for d in recent if d.dose_id == ev.id),
            }
        )
    return {
        "substance": {
            "id": entry["id"],
            "display_name": entry["name"],
            "description": entry.get("description"),
        },
        "symptom": symptom,
        "source": source,
        "source_label": SOURCE_LABELS[source],
        "current_level_mg_l": level["current_level_mg_l"],
        "total_24h_mg": level["total_24h_mg"],
        "threshold_24h_mg": level["threshold_24h_mg"],
        "threshold_status": status,
        "contributing_doses": contributing,
        "message": message,
    }
