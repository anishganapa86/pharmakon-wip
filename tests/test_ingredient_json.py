"""`app/api/service.py:ingredient_json` -- display_name/description come from
the registry and are null when substance_id is null, since there's no entry
to pull them from. Exercised directly, same convention as
test_alert_explanations.py.
"""

from dataclasses import dataclass

from app.api.service import ingredient_json
from app.registry.loader import load_registry


@dataclass
class _FakeIngredient:
    ingredient_name: str
    substance_id: str | None
    amount_mg: float | None
    unmodeled_reason: str | None


def test_resolved_ingredient_carries_display_name_and_description():
    registry = load_registry()
    ing = _FakeIngredient("ACETAMINOPHEN", "acetaminophen", 500.0, None)
    body = ingredient_json(ing, registry)
    assert body["display_name"] == "Acetaminophen"
    assert body["description"] == "Pain reliever and fever reducer"


def test_resolved_ingredient_with_no_registry_description_is_null():
    registry = load_registry()
    ing = _FakeIngredient("LISINOPRIL", "lisinopril", 10.0, None)
    body = ingredient_json(ing, registry)
    assert body["display_name"] == "Lisinopril"
    assert body["description"] is None


def test_unmatched_ingredient_has_no_display_name_or_description():
    registry = load_registry()
    ing = _FakeIngredient("HYDROCORTISONE", None, None, "unmatched")
    body = ingredient_json(ing, registry)
    assert body["display_name"] is None
    assert body["description"] is None
    assert body["name"] == "HYDROCORTISONE"
