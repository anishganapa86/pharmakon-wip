import json

import pytest

from app.api import ai, scan

FULL = {
    "product_name": "Extra Strength Pain Reliever",
    "brand": "Example Brand",
    "product_type": "oral_medication",
    "ingredients": [
        {"name": "Acetaminophen", "strength": "500", "unit": "mg", "role": "active"},
        {"name": "Stearic acid", "strength": None, "unit": None, "role": "inactive"},
    ],
    "label_text_quality": "clear",
    "notes": None,
}


def parse(value):
    text = value if isinstance(value, str) else json.dumps(value)
    return scan.parse_scan(text)


def test_a_complete_answer_parses_unchanged():
    assert parse(FULL) == FULL


def test_the_gemini_schema_avoids_keywords_the_api_rejects():
    text = json.dumps(scan.GEMINI_SCHEMA)
    for keyword in (
        "default",
        "maxLength",
        "minLength",
        "maxItems",
        "$ref",
        "$defs",
        "anyOf",
        "title",
    ):
        assert f'"{keyword}"' not in text
    assert scan.GEMINI_SCHEMA["type"] == "OBJECT"
    assert set(scan.GEMINI_SCHEMA["required"]) == {
        "product_type",
        "ingredients",
        "label_text_quality",
    }


def test_the_token_budget_is_large_enough_for_a_long_ingredient_list():
    assert scan.MAX_OUTPUT_TOKENS >= 2048


def test_numbers_are_accepted_as_strengths():
    data = {
        **FULL,
        "ingredients": [
            {"name": "Acetaminophen", "strength": 500, "unit": "mg", "role": "active"}
        ],
    }
    assert parse(data)["ingredients"][0]["strength"] == "500"
    data["ingredients"][0]["strength"] = 2.5
    assert parse(data)["ingredients"][0]["strength"] == "2.5"


def test_missing_optional_fields_and_extra_whitespace_are_fine():
    text = (
        '  \n {"ingredients": [{"name": "  Water  ", "role": "inactive"}],'
        ' "label_text_quality": "clear"}  \n'
    )
    result = parse(text)
    assert result["ingredients"] == [
        {"name": "Water", "strength": None, "unit": None, "role": "inactive"}
    ]
    assert result["product_name"] is None
    assert result["product_type"] == "unknown"


def test_code_fences_are_stripped():
    assert parse("```json\n" + json.dumps(FULL) + "\n```") == FULL


def test_blank_ingredient_names_are_dropped_not_fatal():
    data = {
        **FULL,
        "ingredients": [
            {"name": "", "role": "unknown"},
            {"name": "Water", "role": "inactive"},
        ],
    }
    assert [i["name"] for i in parse(data)["ingredients"]] == ["Water"]


def test_roles_default_when_absent_but_reject_unknown_words():
    data = {**FULL, "ingredients": [{"name": "Water"}]}
    assert parse(data)["ingredients"][0]["role"] == "unknown"
    with pytest.raises(ai.AIFailure):
        parse({**FULL, "ingredients": [{"name": "Water", "role": "boss"}]})


def test_a_missing_quality_is_derived_from_what_was_read():
    data = {k: v for k, v in FULL.items() if k != "label_text_quality"}
    assert parse(data)["label_text_quality"] == "partial"
    assert parse({"ingredients": []})["label_text_quality"] == "unreadable"


def test_long_text_is_trimmed_and_long_lists_are_capped_as_partial():
    data = {
        **FULL,
        "product_name": "x" * 500,
        "ingredients": [{"name": f"i{n}", "role": "inactive"} for n in range(95)],
    }
    result = parse(data)
    assert len(result["product_name"]) == 200
    assert len(result["ingredients"]) == 80
    assert result["label_text_quality"] == "partial"
    assert result["notes"] == scan.TRUNCATED_NOTE


def test_an_unreadable_label_is_not_an_error_and_has_no_ingredients():
    data = {**FULL, "label_text_quality": "unreadable"}
    result = parse(data)
    assert result["label_text_quality"] == "unreadable"
    assert result["ingredients"] == []


def test_a_partial_label_keeps_what_was_printed():
    data = {
        "product_name": None,
        "brand": None,
        "product_type": "unknown",
        "ingredients": [
            {"name": "Acetaminophen", "strength": "500", "unit": "mg", "role": "active"}
        ],
        "label_text_quality": "partial",
        "notes": "Bottom of the panel is cut off.",
    }
    result = parse(data)
    assert result["label_text_quality"] == "partial"
    assert result["ingredients"][0]["name"] == "Acetaminophen"
    assert result["notes"] == "Bottom of the panel is cut off."


def test_a_truncated_answer_keeps_every_complete_ingredient_as_partial():
    full = json.dumps(FULL)
    cut = full[: full.index("Stearic") + 6]
    result = parse(cut)
    assert result["label_text_quality"] == "partial"
    assert result["notes"] == scan.TRUNCATED_NOTE
    assert [i["name"] for i in result["ingredients"]] == ["Acetaminophen"]
    assert result["product_name"] == "Extra Strength Pain Reliever"
    assert result["brand"] == "Example Brand"


def test_a_truncated_answer_cut_before_any_ingredient_is_partial_with_the_header():
    full = json.dumps(FULL)
    cut = full[: full.index('"ingredients"') + 20]
    result = parse(cut)
    assert result["label_text_quality"] == "partial"
    assert result["ingredients"] == []
    assert result["product_name"] == "Extra Strength Pain Reliever"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        "null",
        "{}",
        '{"hello": "world"}',
        json.dumps({**FULL, "product_type": "weapon"}),
        json.dumps({**FULL, "label_text_quality": "great"}),
        json.dumps({**FULL, "ingredients": "water"}),
        json.dumps({**FULL, "ingredients": ["water"]}),
        '{"product_name": "Only a name"',
    ],
)
def test_anything_that_is_not_the_schema_is_still_rejected(text):
    with pytest.raises(ai.AIFailure):
        scan.parse_scan(text)


def test_validation_failures_log_only_locations(caplog):
    secret = "label-secret-text"
    data = {**FULL, "ingredients": [{"name": secret, "role": "boss"}]}
    with caplog.at_level("WARNING"), pytest.raises(ai.AIFailure):
        parse(data)
    assert secret not in caplog.text
