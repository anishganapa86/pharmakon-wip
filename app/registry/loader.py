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

from ..pk.engine import PKParams

REGISTRY_PATH = Path(__file__).with_name("substances.json")

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


class Registry:
    def __init__(self, entries: list[dict]):
        self._by_id = {e["id"]: e for e in entries}
        self._alias_index: dict[str, str] = {}
        for e in entries:
            for name in [e["name"], e["id"], *e.get("aliases", [])]:
                self._alias_index[normalize(name)] = e["id"]

    def get(self, substance_id: str) -> dict:
        return self._by_id[substance_id]

    def all(self) -> list[dict]:
        return list(self._by_id.values())

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

    def pk_params(self, substance_id: str) -> PKParams:
        e = self._by_id[substance_id]
        if e["half_life_hours"] is None:
            # e.g. ethanol. Give the engine something to refuse on rather than
            # crashing on a None.
            half_life = 1.0
        else:
            half_life = e["half_life_hours"]
        return PKParams(
            substance_id=e["id"],
            half_life_hours=half_life,
            vd_l_per_kg=e["vd_l_per_kg"],
            bioavailability=e["bioavailability"],
            ka_per_hour=e["ka_per_hour"],
            clearance_route=e["clearance_route"],
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
    data = json.loads((path or REGISTRY_PATH).read_text())
    return Registry(data["substances"])
