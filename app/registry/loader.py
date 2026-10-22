"""Registry loading and the ingredient-string normalization layer.

The normalization layer is the unglamorous part the spec warns about. openFDA
returns ingredient names as generics, brands, salt forms and occasional
misspellings. Everything here is deliberately conservative: an unrecognised
string returns None so the caller falls back to manual entry, rather than
guessing a match and silently mis-attributing a dose.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from ..pk.engine import PKParams

REGISTRY_PATH = Path(__file__).with_name("substances.json")
# Written by app/registry/import_drugbank.py. Optional: absent until someone
# has run the import against a DrugBank download. Loaded after the curated file,
# which always takes precedence.
IMPORTED_PATHS = (Path(__file__).with_name("substances_drugbank.json"),)

# How much of a substance the engine can honestly draw, from the parameters the
# registry actually has (never from a flag; a flag can disagree with the values).
#   full         all four PK parameters: a concentration curve in mg/L
#   approximate  half-life and bioavailability only: a decay curve of the amount
#                remaining in mg, with no volume of distribution or absorption
#   none         anything less: logged, never charted
#   unsupported  not first-order (e.g. alcohol): the engine refuses, with a reason
Modelability = Literal["full", "approximate", "none", "unsupported"]

PK_PARAMETERS = ("half_life_hours", "vd_l_per_kg", "bioavailability", "ka_per_hour")

# Salt and form suffixes stripped before matching. Extend this list from real
# openFDA responses rather than from imagination.
_SALT_TOKENS = {
    "hcl",
    "hydrochloride",
    "sodium",
    "calcium",
    "magnesium",
    "potassium",
    "citrate",
    "sulfate",
    "sulphate",
    "tartrate",
    "maleate",
    "besylate",
    "succinate",
    "trihydrate",
    "dihydrate",
    "monohydrate",
    "anhydrous",
    "polacrilex",
    "usp",
    "micronized",
    "extended",
    "release",
    "er",
    "xr",
}


BREACH_SEVERITIES = ("caution", "urgent")
DEFAULT_SEVERITY_ON_BREACH = "caution"


class Registry:
    def __init__(self, entries: list[dict]):
        for e in entries:
            severity = e.get("severity_on_breach", DEFAULT_SEVERITY_ON_BREACH)
            if severity not in BREACH_SEVERITIES:
                raise ValueError(
                    f"registry entry {e.get('id')!r} has severity_on_breach "
                    f"{severity!r}; expected one of {BREACH_SEVERITIES}"
                )
            # Required key, nullable value. A missing key would read the same as
            # "no published threshold", which hides an incomplete entry.
            if "threshold_concentration_mg_per_l" not in e:
                raise KeyError(
                    f"registry entry {e.get('id')!r} is missing "
                    "threshold_concentration_mg_per_l (use null if none)"
                )
        # First wins, for ids and for every name and alias: entries are ordered
        # curated-first, so an imported entry can never take over a curated
        # id or a curated alias.
        self._by_id: dict[str, dict] = {}
        for e in entries:
            self._by_id.setdefault(e["id"], e)
        self._alias_index: dict[str, str] = {}
        for e in self._by_id.values():
            for name in [e["name"], e["id"], *e.get("aliases", [])]:
                self._alias_index.setdefault(normalize(name), e["id"])

    def get(self, substance_id: str) -> dict:
        return self._by_id[substance_id]

    def all(self) -> list[dict]:
        return list(self._by_id.values())

    def threshold_concentration_mg_per_l(self, substance_id: str) -> float | None:
        """Published toxic or therapeutic plasma concentration, in mg/L, or None.

        Populated only from a cited source. Never derived from threshold_24h_mg:
        a dose limit converts to a concentration only via Vd, timing and
        accumulation state, so any computed value would be invented.
        """
        return self._by_id[substance_id]["threshold_concentration_mg_per_l"]

    def severity_on_breach(self, substance_id: str) -> str:
        return self._by_id[substance_id].get(
            "severity_on_breach", DEFAULT_SEVERITY_ON_BREACH
        )

    def unverified(self) -> list[str]:
        return [e["id"] for e in self._by_id.values() if not e.get("verified", False)]

    def resolve(self, raw_ingredient_name: str) -> str | None:
        """Map an observed ingredient string to a registry substance id.

        Returns None when there is no confident match. None means "ask the user
        to enter this manually", which is a working path. A wrong guess is not.
        """
        key = normalize(raw_ingredient_name)
        if key in self._alias_index:
            return self._alias_index[key]

        stripped = strip_salt_tokens(key)
        if stripped and stripped in self._alias_index:
            return self._alias_index[stripped]

        return None

    def modelability(self, substance_id: str) -> Modelability:
        e = self._by_id[substance_id]
        if e.get("kinetics", "first_order") != "first_order":
            return "unsupported"
        have = {k for k in PK_PARAMETERS if e.get(k) is not None}
        if len(have) == len(PK_PARAMETERS):
            return "full"
        if {"half_life_hours", "bioavailability"} <= have:
            return "approximate"
        return "none"

    def pk_params(self, substance_id: str) -> PKParams:
        """Parameters as stored. Vd and ka may be None unless `modelability` is
        "full", so callers must gate on it before handing these to the engine."""
        e = self._by_id[substance_id]
        if e.get("half_life_hours") is None:
            # e.g. ethanol. Give the engine something to refuse on rather than
            # crashing on a None.
            half_life = 1.0
        else:
            half_life = e["half_life_hours"]
        return PKParams(
            substance_id=e["id"],
            half_life_hours=half_life,
            vd_l_per_kg=e.get("vd_l_per_kg"),
            bioavailability=e.get("bioavailability"),
            ka_per_hour=e.get("ka_per_hour"),
            clearance_route=e.get("clearance_route", "unknown"),
            kinetics=e.get("kinetics", "first_order"),
        )


def normalize(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def strip_salt_tokens(normalized: str) -> str:
    tokens = [t for t in normalized.split() if t not in _SALT_TOKENS]
    return " ".join(tokens)


@lru_cache(maxsize=1)
def load_registry(path: Path | None = None) -> Registry:
    """The curated registry, then any imported one. An explicit `path` loads
    that file alone (tests)."""
    paths = (
        [path] if path else [REGISTRY_PATH, *(p for p in IMPORTED_PATHS if p.exists())]
    )
    entries: list[dict] = []
    for p in paths:
        entries += json.loads(p.read_text())["substances"]
    return Registry(entries)
