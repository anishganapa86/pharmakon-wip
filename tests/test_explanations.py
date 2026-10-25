import json
import re

import pytest

from app.api import explanations, food_rules
from app.registry.loader import load_registry

BANNED = re.compile(r"\b(safe|no risk|will cause|causes|prevents|harmless)\b", re.I)
CATEGORIES = [
    "duplicate_ingredient",
    "same_drug_class",
    "additive_effect",
    "food_or_alcohol",
    "absorption_timing",
]
INGREDIENTS = ["acetaminophen", "nsaids", "sedating_antihistamines", "decongestants"]


def data():
    return explanations.load()


def all_entries():
    return [*data()["categories"].values(), *data()["ingredients"].values()]


def test_every_requested_category_has_a_plain_explanation():
    for name in CATEGORIES:
        entry = data()["categories"][name]
        assert entry["what_this_means"].strip()
        assert entry["title"].strip()


def test_every_requested_ingredient_has_an_entry_with_a_label_quote():
    for name in INGREDIENTS:
        entry = data()["ingredients"][name]
        could = entry["what_could_happen"]
        assert could and could["quote"].strip()
        assert could["source_url"].startswith(explanations.DAILYMED_PREFIX)
        assert could["set_id"] in could["source_url"]
        assert could["citation"].startswith("FDA label for ")
        assert entry["substances"]


def test_the_duplicate_ingredient_text_matches_the_agreed_wording():
    assert data()["categories"]["duplicate_ingredient"]["what_this_means"] == (
        "The same medicine is in more than one of your products under different "
        "brand names, so it's easy to take more than you realize."
    )


def test_the_closing_sentence_is_fixed_and_on_every_view():
    assert data()["closing"] == "A pharmacist can tell you what this means for you."
    for name in CATEGORIES:
        assert explanations.category(name)["closing"] == data()["closing"]
    assert explanations.ingredient_for(["ibuprofen"])["closing"] == data()["closing"]


def test_no_part_uses_a_banned_phrase():
    for entry in all_entries():
        assert not BANNED.search(entry["what_this_means"]), entry["title"]
        assert not BANNED.search(entry["title"])
        could = entry.get("what_could_happen")
        if could:
            assert not BANNED.search(could["quote"])
        why = entry.get("why_it_matters")
        if why:
            assert not BANNED.search(why["text"])


def test_no_part_promises_an_outcome():
    for entry in all_entries():
        texts = [
            entry["what_this_means"],
            (entry.get("why_it_matters") or {}).get("text", ""),
        ]
        for text in texts:
            assert not re.search(
                r"\b(will|always|definitely|guarantee)\b", text, re.I
            ), text


def test_every_entry_is_under_ninety_words():
    for entry in all_entries():
        assert explanations.word_count(entry) <= 90


def test_the_acetaminophen_public_health_fact_is_cited_to_the_federal_register():
    why = data()["ingredients"]["acetaminophen"]["why_it_matters"]
    assert why["source_url"] == (
        "https://www.govinfo.gov/content/pkg/FR-2011-01-14/html/2011-709.htm"
    )
    assert "Federal Register" in why["citation"]
    assert "January 14, 2011" in why["citation"]
    assert "leading cause of acute liver failure" in why["text"]
    assert "about half" in why["text"]
    assert (
        why["quotes"][0] == "Acetaminophen overdose has become the leading cause of ALF"
    )


def test_other_why_it_matters_parts_are_left_out_rather_than_invented():
    for name in ("nsaids", "sedating_antihistamines", "decongestants"):
        assert data()["ingredients"][name]["why_it_matters"] is None
    for entry in data()["categories"].values():
        assert entry["why_it_matters"] is None


def test_unsourced_ingredients_are_recorded_as_dropped():
    dropped = data()["dropped"]
    assert "dextromethorphan" in dropped
    assert "cold and flu" in dropped["cold_and_flu_combination_products"]


def test_poison_control_is_offered_only_where_overdose_is_the_concern():
    assert explanations.ingredient_for(["acetaminophen"])["poison_control"] is True
    assert explanations.category("duplicate_ingredient")["poison_control"] is False
    assert explanations.ingredient_for(["diphenhydramine"])["poison_control"] is False


def test_ingredient_entries_win_over_categories_for_matching_substances():
    finding = {"substance_a": {"id": "ibuprofen"}, "substance_b": {"id": "naproxen"}}
    why = explanations.why_for_interaction(finding, shared_class=True)
    assert why["kind"] == "ingredient" and why["key"] == "nsaids"


def test_unlisted_substances_fall_back_to_a_category():
    finding = {
        "substance_a": {"id": "metformin"},
        "substance_b": {"id": "lisinopril"},
        "message": "m",
    }
    assert explanations.why_for_interaction(finding, True)["key"] == "same_drug_class"
    assert (
        explanations.why_for_interaction(finding, False)["key"] == "mentioned_together"
    )
    sleepy = {**finding, "effect": "May increase drowsiness."}
    assert explanations.why_for_interaction(sleepy, False)["key"] == "additive_effect"
    food = {**finding, "finding_kind": "food"}
    assert explanations.why_for_interaction(food, False)["key"] == "food_or_alcohol"


def test_a_category_explanation_never_borrows_an_unrelated_label_quote():
    for name in CATEGORIES:
        assert explanations.category(name)["what_could_happen"] is None


def test_an_unsourced_banned_or_wrongly_closed_entry_is_refused(tmp_path):
    base = json.loads(explanations.DATA_PATH.read_text(encoding="utf-8"))
    path = tmp_path / "e.json"

    unsourced = json.loads(json.dumps(base))
    unsourced["ingredients"]["acetaminophen"]["what_could_happen"]["source_url"] = (
        "https://x.example"
    )
    banned = json.loads(json.dumps(base))
    banned["categories"]["same_drug_class"]["what_this_means"] = "This is safe."
    wrong_closing = json.loads(json.dumps(base))
    wrong_closing["closing"] = "Ask someone."
    for broken in (unsourced, banned, wrong_closing):
        path.write_text(json.dumps(broken), encoding="utf-8")
        with pytest.raises(explanations.InvalidExplanation):
            explanations.load.__wrapped__(path)


def test_the_food_rules_and_explanations_quote_the_same_label_text():
    rules = {r.id: r for r in food_rules.load_rules()}
    acet = data()["ingredients"]["acetaminophen"]["what_could_happen"]
    assert acet["set_id"] == rules["acetaminophen-alcohol"].set_id
    assert acet["quote"] in rules["acetaminophen-alcohol"].quote


def test_registry_ids_in_ingredient_entries_exist():
    ids = {e["id"] for e in load_registry().all()}
    for entry in data()["ingredients"].values():
        for sid in entry["substances"]:
            assert sid in ids, sid
