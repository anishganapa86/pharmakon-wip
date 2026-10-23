"""Alert `substance.threshold_explanation` (registry-sourced, never invented).

`app/api/service.py:alert_json` is exercised directly here rather than
through the API, since the explanation only depends on the registry entry,
not on any detector behavior (covered separately in test_api.py and
test_detect.py).
"""

from app.api.db import AlertRecord
from app.api.service import alert_json
from app.registry.loader import load_registry


def _record(substance_id: str | None) -> AlertRecord:
    return AlertRecord(
        id="alert_1",
        user_id="default",
        kind="duplication",
        severity="caution",
        substance_id=substance_id,
        message="test",
        notes=[],
        contributing_doses=[],
        uncounted_doses=[{"placeholder": True}],
        computed_at="2026-09-13T18:00:00-07:00",
    )


def test_documented_substance_carries_its_explanation():
    registry = load_registry()
    body = alert_json(_record("acetaminophen"), registry)
    assert body["substance"]["threshold_explanation"] == (
        "OTC Drug Facts labels warn that severe liver damage may occur above "
        "4,000 mg of acetaminophen in 24 hours, and risk increases with "
        "alcohol use. Many products' directions set a lower daily maximum."
    )


def test_undocumented_substance_is_null_not_invented():
    registry = load_registry()
    body = alert_json(_record("naproxen"), registry)
    assert body["substance"]["threshold_explanation"] is None


def test_documented_substance_carries_its_description():
    registry = load_registry()
    body = alert_json(_record("acetaminophen"), registry)
    assert body["substance"]["description"] == "Pain reliever and fever reducer"


def test_undescribed_substance_is_null_not_invented():
    registry = load_registry()
    body = alert_json(_record("lisinopril"), registry)
    assert body["substance"]["description"] is None


def test_product_level_incomplete_data_has_no_substance_to_explain():
    registry = load_registry()
    body = alert_json(_record(None), registry)
    assert body["substance"] is None


def test_unverified_registry_substance_is_flagged_unverified():
    registry = load_registry()
    body = alert_json(_record("acetaminophen"), registry)
    assert body["substance"]["verified"] is False


def test_verified_registry_substance_is_flagged_verified():
    class StubRegistry:
        def get(self, substance_id):
            return {"name": "Stubbed", "verified": True}

    body = alert_json(_record("stubbed"), StubRegistry())
    assert body["substance"]["verified"] is True
