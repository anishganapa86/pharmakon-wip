import httpx
import pytest

from app.api import food_guidance, interactions, label_source, side_effects

CAFFEINE_CITRATE_IV = {
    "set_id": "iv-citrate",
    "openfda": {
        "generic_name": ["CAFFEINE CITRATE"],
        "brand_name": ["Caffeine Citrate"],
        "route": ["INTRAVENOUS"],
        "substance_name": ["CAFFEINE CITRATE"],
    },
}
CAFFEINE_CITRATE_ORAL = {
    "set_id": "oral-citrate",
    "openfda": {
        "generic_name": ["CAFFEINE CITRATE"],
        "brand_name": ["Caffeine Citrate Oral Solution"],
        "route": ["ORAL"],
        "substance_name": ["CAFFEINE CITRATE"],
    },
}
CAFFEINE_ORAL = {
    "set_id": "oral-caffeine",
    "openfda": {
        "generic_name": ["CAFFEINE"],
        "brand_name": ["Stay Awake"],
        "route": ["ORAL"],
        "substance_name": ["CAFFEINE"],
    },
}


def with_text(label, field, text):
    return {**label, field: [text]}


def serve(monkeypatch, module, labels, http_attr="_http"):
    searches: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        search = request.url.params["search"]
        searches.append(search)
        served = labels
        if ".exact:" in search:
            queried = search.split('.exact:"')[1].split('"')[0]
            served = [
                label for label in labels if queried in label["openfda"]["generic_name"]
            ]
        if not served:
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})
        return httpx.Response(200, json={"results": served})

    monkeypatch.setattr(
        module, http_attr, httpx.Client(transport=httpx.MockTransport(handler))
    )
    return searches


@pytest.fixture(autouse=True)
def clean_caches():
    for module in (side_effects, interactions):
        module.clear_cache()
    food_guidance._label_cache.clear()
    yield
    for module in (side_effects, interactions):
        module.clear_cache()
    food_guidance._label_cache.clear()


@pytest.mark.parametrize(
    "label_name, name, expected",
    [
        ("CAFFEINE", "caffeine", True),
        ("Caffeine", "CAFFEINE", True),
        ("CAFFEINE CITRATE", "caffeine", False),
        ("caffeine citrate", "caffeine", False),
        ("CAFFEINE", "caffeine citrate", False),
        ("CAFFEINE CITRATE", "caffeine citrate", True),
        ("DONEPEZIL HYDROCHLORIDE", "donepezil", True),
        ("CETIRIZINE HYDROCHLORIDE", "cetirizine", True),
        ("DIPHENHYDRAMINE HYDROCHLORIDE", "diphenhydramine", True),
        ("DIPHENHYDRAMINE CITRATE", "diphenhydramine", True),
        ("NICOTINE POLACRILEX", "nicotine", True),
        ("IBUPROFEN", "naproxen", False),
    ],
)
def test_same_drug_name(label_name, name, expected):
    assert label_source.same_drug_name(label_name, name) is expected


def test_only_labels_with_an_oral_route_are_oral():
    assert label_source.is_oral(CAFFEINE_ORAL)
    assert not label_source.is_oral(CAFFEINE_CITRATE_IV)
    assert not label_source.is_oral({"openfda": {"generic_name": ["CAFFEINE"]}})
    assert not label_source.is_oral({})
    assert label_source.is_oral({"openfda": {"route": ["ORAL", "SUBLINGUAL"]}})
    assert not label_source.is_oral({"openfda": {"route": ["INTRAVENOUS"]}})


def test_fallback_match_rejects_caffeine_citrate_and_keeps_caffeine():
    assert not label_source.fallback_matches(
        "generic_name", CAFFEINE_CITRATE_ORAL, "caffeine"
    )
    assert not label_source.fallback_matches(
        "generic_name", CAFFEINE_CITRATE_IV, "caffeine"
    )
    assert label_source.fallback_matches("generic_name", CAFFEINE_ORAL, "caffeine")


@pytest.mark.parametrize(
    "module, fetch, field",
    [
        (
            side_effects,
            side_effects.fetch_adverse_reaction_sections,
            "adverse_reactions",
        ),
        (
            interactions,
            interactions.fetch_drug_interaction_sections,
            "drug_interactions",
        ),
    ],
)
def test_caffeine_citrate_labels_are_never_used_for_caffeine(
    monkeypatch, module, fetch, field
):
    labels = [
        with_text(CAFFEINE_CITRATE_IV, field, "Apnea of prematurity in infants."),
        with_text(CAFFEINE_CITRATE_ORAL, field, "Neonatal oral solution text."),
    ]
    searches = serve(monkeypatch, module, labels)

    assert fetch("caffeine") == []
    assert len(searches) == 2


@pytest.mark.parametrize(
    "module, fetch, field",
    [
        (
            side_effects,
            side_effects.fetch_adverse_reaction_sections,
            "adverse_reactions",
        ),
        (
            interactions,
            interactions.fetch_drug_interaction_sections,
            "drug_interactions",
        ),
    ],
)
def test_an_oral_caffeine_label_is_used_alongside_rejected_citrate_labels(
    monkeypatch, module, fetch, field
):
    labels = [
        with_text(CAFFEINE_CITRATE_IV, field, "Citrate text."),
        with_text(CAFFEINE_ORAL, field, "Headache and jitteriness."),
        with_text(CAFFEINE_CITRATE_ORAL, field, "Oral citrate text."),
    ]
    serve(monkeypatch, module, labels)

    sections = fetch("caffeine")

    assert [s["text"] for s in sections] == ["Headache and jitteriness."]
    assert [s["set_id"] for s in sections] == ["oral-caffeine"]


def test_an_injection_only_label_is_not_used_for_an_oral_substance(monkeypatch):
    acetaminophen_iv = {
        "set_id": "iv-apap",
        "openfda": {
            "generic_name": ["ACETAMINOPHEN"],
            "route": ["INTRAVENOUS"],
        },
        "adverse_reactions": ["Nausea, vomiting, headache."],
    }
    serve(monkeypatch, side_effects, [acetaminophen_iv])

    assert side_effects.fetch_adverse_reaction_sections("acetaminophen") == []


def test_food_guidance_ignores_non_oral_and_citrate_labels_for_caffeine(monkeypatch):
    serve(
        monkeypatch,
        food_guidance,
        [CAFFEINE_CITRATE_IV, CAFFEINE_CITRATE_ORAL, CAFFEINE_ORAL],
    )

    labels = food_guidance.fetch_labels_for_substance("caffeine")

    assert [label["set_id"] for label in labels] == ["oral-caffeine"]


def test_food_guidance_has_no_label_when_only_citrate_labels_exist(monkeypatch):
    serve(monkeypatch, food_guidance, [CAFFEINE_CITRATE_IV, CAFFEINE_CITRATE_ORAL])

    assert food_guidance.fetch_labels_for_substance("caffeine") == []
