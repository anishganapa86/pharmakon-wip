from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "explanations.json"
DAILYMED_PREFIX = "https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid="
REQUIRED_CLOSING = "A pharmacist can tell you what this means for you."
BANNED = re.compile(r"\b(safe|no risk|will cause|causes|prevents|harmless)\b", re.I)
MAX_WORDS = 90
SLEEPY_OR_STIMULANT = re.compile(r"drows|sedat|sleep|stimul|caffeine", re.I)


class InvalidExplanation(ValueError):
    pass


EFFICACY = re.compile(
    r"\b(supports?|improves?|treats?|cures?|relieves?|helps?\s+with|used\s+(to|for))\b",
    re.I,
)


def _check_text(where: str, text: str) -> None:
    if not text.strip():
        raise InvalidExplanation(f"{where} is empty")
    if BANNED.search(text):
        raise InvalidExplanation(f"{where} uses a banned phrase")
    if EFFICACY.search(text):
        raise InvalidExplanation(f"{where} makes a benefit claim")


def _check_entry(name: str, entry: dict) -> None:
    _check_text(f"{name}.what_this_means", entry["what_this_means"])
    could = entry.get("what_could_happen")
    if could is not None:
        for field in ("quote", "citation", "source_url", "set_id", "section"):
            if not str(could.get(field) or "").strip():
                raise InvalidExplanation(f"{name}.what_could_happen has no {field}")
        if not could["source_url"].startswith(DAILYMED_PREFIX):
            raise InvalidExplanation(f"{name}.what_could_happen is not from DailyMed")
        if BANNED.search(could["quote"]):
            raise InvalidExplanation(f"{name}.what_could_happen quotes a banned phrase")
    why = entry.get("why_it_matters")
    if why is not None:
        for field in ("text", "citation", "source_url"):
            if not str(why.get(field) or "").strip():
                raise InvalidExplanation(f"{name}.why_it_matters has no {field}")
        if not why["source_url"].startswith("https://"):
            raise InvalidExplanation(f"{name}.why_it_matters has no link")
        _check_text(f"{name}.why_it_matters", why["text"])
    if word_count(entry) > MAX_WORDS:
        raise InvalidExplanation(f"{name} is over {MAX_WORDS} words")


def word_count(entry: dict, closing: str = REQUIRED_CLOSING) -> int:
    parts = [entry["what_this_means"], closing]
    if entry.get("what_could_happen"):
        parts.append(entry["what_could_happen"]["quote"])
    if entry.get("why_it_matters"):
        parts.append(entry["why_it_matters"]["text"])
    return sum(len(p.split()) for p in parts)


@lru_cache(maxsize=1)
def load(path: Path | None = None) -> dict:
    data = json.loads((path or DATA_PATH).read_text(encoding="utf-8"))
    if data.get("closing") != REQUIRED_CLOSING:
        raise InvalidExplanation("the closing sentence must be the required one")
    for name, entry in data["categories"].items():
        _check_entry(f"categories.{name}", entry)
    for name, entry in data["ingredients"].items():
        _check_entry(f"ingredients.{name}", entry)
        if not entry.get("substances"):
            raise InvalidExplanation(f"ingredients.{name} lists no substances")
    return data


def _view(kind: str, key: str, entry: dict) -> dict:
    return {
        "kind": kind,
        "key": key,
        "title": entry["title"],
        "what_this_means": entry["what_this_means"],
        "what_could_happen": entry.get("what_could_happen"),
        "why_it_matters": entry.get("why_it_matters"),
        "poison_control": bool(entry.get("poison_control")),
        "closing": load()["closing"],
    }


def category(name: str) -> dict:
    return _view("category", name, load()["categories"][name])


def ingredient_for(substance_ids: list[str]) -> dict | None:
    wanted = set(substance_ids)
    for key, entry in load()["ingredients"].items():
        if wanted & set(entry["substances"]):
            return _view("ingredient", key, entry)
    return None


def category_for_interaction(finding: dict, shared_class: bool) -> str:
    if finding.get("finding_kind") == "food":
        return "food_or_alcohol"
    if shared_class:
        return "same_drug_class"
    if SLEEPY_OR_STIMULANT.search(str(finding.get("effect") or "")):
        return "additive_effect"
    return "mentioned_together"


def why_for_interaction(finding: dict, shared_class: bool) -> dict:
    a = finding["substance_a"]["id"]
    b = finding["substance_b"]["id"]
    if finding.get("finding_kind") == "food":
        matched = ingredient_for([a])
    else:
        first, second = ingredient_for([a]), ingredient_for([b])
        matched = first if first and second and first["key"] == second["key"] else None
    return matched or category(category_for_interaction(finding, shared_class))


def why_for_duplicate(substance_ids: list[str]) -> dict:
    return ingredient_for(substance_ids) or category("duplicate_ingredient")
