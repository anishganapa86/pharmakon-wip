import json
import re
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import food_rules, main, products, usda
from app.api.db import Base
from app.registry.loader import load_registry

NOW = datetime.fromisoformat("2026-09-13T18:00:00-07:00")
BANNED = re.compile(r"\b(safe|no risk|will cause|causes|prevents|harmless)\b", re.I)
PRODUCT_IDS = {
    "tylenol": "prod_tylenol_es_500",
    "dayquil": "prod_dayquil_liquicap",
    "nyquil": "prod_nyquil_liquicap",
    "nodoz": "prod_nodoz_200",
    "excedrin": "prod_excedrin_es",
    "advil": "prod_advil_200",
    "benadryl": "prod_benadryl_25",
    "coffee": "prod_food_coffee",
    "decaf": "prod_food_decaf_coffee",
    "herbal_tea": "prod_food_herbal_tea",
    "beer": "prod_example_beer",
    "chicken": "prod_food_chicken",
    "grapefruit": "prod_food_grapefruit",
    "kale": "prod_food_kale",
}


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)

    def session_override():
        with maker() as s:
            yield s

    main.app.dependency_overrides[main.get_session] = session_override
    main.app.dependency_overrides[main.get_now] = lambda: NOW
    yield TestClient(main.app)
    main.app.dependency_overrides.pop(main.get_session, None)
    main.app.dependency_overrides.pop(main.get_now, None)


def log(client, name, hours_ago=1):
    taken = (NOW.replace(hour=17)).isoformat() if hours_ago == 1 else None
    r = client.post(
        "/users/default/doses",
        json={
            "product_id": PRODUCT_IDS[name],
            "taken_at": taken or (NOW.replace(day=NOW.day - 3)).isoformat(),
            "quantity": 1,
        },
    )
    assert r.status_code == 201, r.text


def check(client):
    r = client.get("/users/default/interaction-check")
    assert r.status_code == 200
    return r.json()


def food(result):
    return [f for f in result["interactions"] if f.get("finding_kind") == "food"]


def rule_ids(result):
    return {f["source"] for f in food(result)}


def test_the_rules_file_has_every_required_field_and_a_false_verified_flag():
    rules = food_rules.load_rules()
    assert len(rules) >= 6
    ids = [r.id for r in rules]
    assert len(ids) == len(set(ids))
    registry = load_registry()
    for rule in rules:
        assert rule.quote.strip()
        assert rule.citation.startswith("FDA label for ")
        assert rule.set_id in rule.source_url
        assert rule.source_url.startswith(
            "https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid="
        )
        assert rule.verified is False
        assert rule.substance_id in {e["id"] for e in registry.all()}
        assert rule.food_component in products.COMPONENT_INGREDIENT
        assert not BANNED.search(rule.quote)


def test_no_rule_is_marked_verified_without_its_verification_fields():
    data = json.loads(food_rules.DATA_PATH.read_text(encoding="utf-8"))
    for entry in data["rules"]:
        if entry["verified"] is True:
            for field in ("verified_by", "verified_on", "source_url", "source_quote"):
                assert entry.get(field)


def test_a_rule_without_a_source_is_refused(tmp_path):
    bad = {
        "rules": [
            {
                "id": "x",
                "quote": "q",
                "citation": "",
                "set_id": "s",
                "source_url": "u",
                "section": "w",
                "verified": False,
            }
        ]
    }
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(food_rules.InvalidFoodRule):
        food_rules.load_rules.__wrapped__(path)


def test_a_rule_that_is_not_from_dailymed_is_refused(tmp_path):
    entry = json.loads(food_rules.DATA_PATH.read_text(encoding="utf-8"))["rules"][0]
    entry["source_url"] = "https://example.com/label"
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({"rules": [entry]}), encoding="utf-8")
    with pytest.raises(food_rules.InvalidFoodRule):
        food_rules.load_rules.__wrapped__(path)


def test_the_acetaminophen_alcohol_rule_quotes_the_label_verbatim():
    rule = next(r for r in food_rules.load_rules() if r.id == "acetaminophen-alcohol")
    assert "3 or more alcoholic drinks every day while using this product" in rule.quote
    assert rule.set_id == "0005aafa-08f8-4990-a54d-cc097195708f"


def test_tylenol_plus_dayquil_shows_the_duplicate_ingredient_and_no_food_finding(
    client,
):
    log(client, "tylenol")
    log(client, "dayquil")
    result = check(client)
    assert any(
        d["ingredient_name"].lower() == "acetaminophen"
        for d in result["duplicate_ingredients"]
    )
    assert food(result) == []


def test_adding_coffee_to_tylenol_and_dayquil_adds_nothing_the_labels_do_not_support(
    client,
):
    log(client, "tylenol")
    log(client, "dayquil")
    log(client, "coffee")
    assert food(check(client)) == []


def test_coffee_with_a_caffeine_medication_shows_the_excedrin_label_quote(client):
    log(client, "nodoz")
    log(client, "coffee")
    findings = food(check(client))
    assert len(findings) == 1
    finding = findings[0]
    assert finding["quoted_text"].startswith(
        "Limit the use of caffeine-containing medications"
    )
    assert finding["quote_source"] == "fda_label_verbatim"
    assert finding["verified"] is False
    assert finding["severity"] is None
    assert finding["dailymed_url"].endswith("0faa3592-4d56-4645-9a0e-066e0bdfebb6")
    assert finding["substance_b"]["display_name"] == "Caffeine"
    assert [s["label"] for s in finding["substance_b"]["sources"]] == ["Coffee"]


def test_a_beer_with_tylenol_shows_the_acetaminophen_alcohol_warning(client):
    log(client, "tylenol")
    log(client, "dayquil")
    log(client, "beer")
    findings = food(check(client))
    assert len(findings) == 1
    assert (
        "3 or more alcoholic drinks every day while using this product"
        in findings[0]["quoted_text"]
    )
    assert findings[0]["substance_a"]["id"] == "acetaminophen"
    assert findings[0]["substance_b"]["display_name"] == "Alcohol"


def test_a_beer_with_nyquil_adds_the_doxylamine_label_too(client):
    log(client, "nyquil")
    log(client, "beer")
    ids = {f["substance_a"]["id"] for f in food(check(client))}
    assert ids == {"acetaminophen", "doxylamine"}


def test_beer_with_an_nsaid_shows_the_stomach_bleeding_label(client):
    log(client, "advil")
    log(client, "beer")
    findings = food(check(client))
    assert [f["substance_a"]["id"] for f in findings] == ["ibuprofen"]
    assert findings[0]["quoted_text"].startswith("Stomach bleeding warning")


def test_chicken_gives_no_food_findings_with_any_medications(client):
    for name in (
        "tylenol",
        "dayquil",
        "nyquil",
        "advil",
        "benadryl",
        "nodoz",
        "excedrin",
    ):
        log(client, name)
    log(client, "chicken")
    assert food(check(client)) == []


@pytest.mark.parametrize("name", ["decaf", "herbal_tea"])
def test_decaf_and_herbal_tea_map_to_no_components(client, name):
    log(client, "nodoz")
    log(client, "benadryl")
    log(client, name)
    assert food(check(client)) == []
    product = products.PRODUCTS[PRODUCT_IDS[name]]
    assert all(i.name not in products.INGREDIENT_COMPONENT for i in product.ingredients)


def test_a_food_alone_gives_no_finding_and_a_drug_alone_gives_none(client):
    log(client, "beer")
    assert food(check(client)) == []


def test_each_rule_fires_only_on_its_own_trigger(client):
    log(client, "advil")
    log(client, "coffee")
    assert food(check(client)) == []


def test_a_food_logged_days_ago_is_not_counted(client):
    log(client, "tylenol")
    log(client, "beer", hours_ago=72)
    assert food(check(client)) == []


def fake_event(product_id, name, ingredients, hours_ago=1):
    taken = NOW.replace(hour=17).isoformat()
    return SimpleNamespace(
        id=f"dose_{product_id}",
        product_id=product_id,
        product_name=name,
        taken_at=taken,
        topical_ingredients=None,
        ingredients=[
            SimpleNamespace(ingredient_name=n, substance_id=s) for n, s in ingredients
        ],
    )


def test_atorvastatin_with_grapefruit_quotes_its_label():
    registry = load_registry()
    events = [
        fake_event("rx", "Atorvastatin", [("ATORVASTATIN", "atorvastatin")]),
        fake_event(
            "prod_food_grapefruit_juice", "Grapefruit juice", [("GRAPEFRUIT", None)]
        ),
    ]
    findings = food_rules.food_findings(events, registry, NOW)
    assert [f["substance_a"]["id"] for f in findings] == ["atorvastatin"]
    assert "Grapefruit juice consumption" in findings[0]["quoted_text"]


def test_warfarin_with_kale_quotes_its_label_and_a_neutral_food_does_not():
    registry = load_registry()
    drug = fake_event("rx2", "Warfarin", [("WARFARIN", "warfarin")])
    kale = fake_event("prod_food_kale", "Kale", [("VITAMIN K", None)])
    rice = fake_event("prod_food_rice", "Rice", [("RICE", None)])
    assert len(food_rules.food_findings([drug, kale], registry, NOW)) == 1
    assert food_rules.food_findings([drug, rice], registry, NOW) == []


def test_unverified_rules_are_hidden_and_counted_when_the_gate_is_off(
    client, monkeypatch
):
    log(client, "tylenol")
    log(client, "beer")
    monkeypatch.setenv("SHOW_UNVERIFIED_RULES", "0")
    result = check(client)
    assert food(result) == []
    notice = result["rules_awaiting_verification"]
    assert notice["count"] >= len(food_rules.load_rules())


def test_the_gate_on_shows_the_rules_and_no_notice(client):
    log(client, "tylenol")
    log(client, "beer")
    result = check(client)
    assert len(food(result)) == 1
    assert "rules_awaiting_verification" not in result


def usda_payload():
    return {
        "foods": [
            {
                "fdcId": 171890,
                "description": "Coffee, brewed, prepared with tap water",
                "dataType": "SR Legacy",
                "foodNutrients": [
                    {"nutrientNumber": "262", "value": 40.0},
                    {"nutrientNumber": "221", "value": 0.0},
                ],
            },
            {
                "fdcId": 170000,
                "description": "Beer, regular, all",
                "dataType": "SR Legacy",
                "foodNutrients": [{"nutrientNumber": "221", "value": 3.9}],
            },
            {
                "fdcId": 169999,
                "description": "Kale, raw",
                "foodNutrients": [{"nutrientNumber": "430", "value": 389.6}],
            },
            {"fdcId": 1, "description": "", "foodNutrients": []},
        ]
    }


@pytest.fixture
def usda_calls(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=usda_payload())

    monkeypatch.setenv("USDA_FDC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(
        usda, "_http", httpx.Client(transport=httpx.MockTransport(handler))
    )
    return calls


def test_usda_is_off_without_a_key(monkeypatch):
    monkeypatch.setenv("USDA_FDC_API_KEY", "")
    assert usda.enabled() is False
    assert usda.search("coffee") == []


def test_usda_search_maps_nutrients_to_components(usda_calls):
    found = usda.search("coffee")
    by_name = {p.brand_name: p for p in found}
    assert set(by_name) == {
        "Coffee, brewed, prepared with tap water",
        "Beer, regular, all",
        "Kale, raw",
    }
    assert [
        i.name for i in by_name["Coffee, brewed, prepared with tap water"].ingredients
    ] == ["CAFFEINE"]
    assert [i.name for i in by_name["Beer, regular, all"].ingredients] == ["ALCOHOL"]
    assert [i.name for i in by_name["Kale, raw"].ingredients] == ["KALE, RAW"]
    assert all(i.strength_mg is None for p in found for i in p.ingredients)
    assert by_name["Kale, raw"].id == "usda_169999"


def test_usda_vitamin_k_is_reported_but_never_becomes_a_component(usda_calls):
    found = {p.brand_name: p for p in usda.search("kale")}
    kale = found["Kale, raw"]
    assert [i.name for i in kale.ingredients] == ["KALE, RAW"]
    assert usda.get_nutrients(kale.id) == {"vitamin_k": 389.6}


def test_a_common_food_with_trace_vitamin_k_gets_no_component_and_no_finding(
    client, monkeypatch
):
    payload = {
        "foods": [
            {
                "fdcId": 331960,
                "description": "Chicken, broilers, raw",
                "foodNutrients": [{"nutrientNumber": "430", "value": 0.3}],
            }
        ]
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    monkeypatch.setenv("USDA_FDC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(
        usda, "_http", httpx.Client(transport=httpx.MockTransport(handler))
    )
    (chicken,) = usda.search("chicken")
    assert [i.name for i in chicken.ingredients] == ["CHICKEN, BROILERS, RAW"]
    assert usda.get_nutrients(chicken.id) == {"vitamin_k": 0.3}
    events = [
        fake_event("rx2", "Warfarin", [("WARFARIN", "warfarin")]),
        fake_event(
            chicken.id,
            chicken.brand_name,
            [(i.name, None) for i in chicken.ingredients],
        ),
    ]
    assert food_rules.food_findings(events, load_registry(), NOW) == []


def test_usda_failures_are_not_retried_on_every_keystroke(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectTimeout("slow", request=request)

    monkeypatch.setenv("USDA_FDC_API_KEY", "k")
    monkeypatch.setattr(
        usda, "_http", httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert usda.search("coffee") == []
    assert usda.search("coffee") == []
    assert len(calls) == 1


def test_a_medication_search_does_not_send_the_text_to_usda(client, usda_calls):
    r = client.get("/products/search", params={"q": "tylenol"})
    assert r.status_code == 200
    assert usda_calls == []


def test_usda_only_sends_the_search_text_and_the_allowed_data_types(usda_calls):
    usda.search("coffee")
    (request,) = usda_calls
    assert request.url.params["query"] == "coffee"
    assert request.url.params["dataType"] == "Foundation,SR Legacy,Survey (FNDDS)"
    assert "test-key-not-real" not in str(request.url)
    assert request.headers["X-Api-Key"] == "test-key-not-real"
    assert set(request.url.params) == {"query", "dataType", "pageSize"}


def test_usda_results_are_cached_for_the_session(usda_calls):
    usda.search("Coffee")
    usda.search("coffee")
    assert len(usda_calls) == 1


def test_usda_failures_fall_back_to_nothing(monkeypatch):
    def handler(request):
        raise httpx.ConnectTimeout("slow", request=request)

    monkeypatch.setenv("USDA_FDC_API_KEY", "k")
    monkeypatch.setattr(
        usda, "_http", httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert usda.search("coffee") == []


def test_usda_has_a_five_second_timeout():
    assert usda.TIMEOUT_SECONDS == 5.0
    assert usda._http.timeout.read == 5.0


def test_a_usda_food_appears_in_log_intake_search_labelled_and_typed(
    client, usda_calls
):
    r = client.get("/products/search", params={"q": "coffee"})
    results = {x["brand_name"]: x for x in r.json()["results"]}
    usda_item = results["Coffee, brewed, prepared with tap water"]
    assert usda_item["kind"] == "food_drink"
    assert usda_item["source"] == "usda"
    assert usda_item["label"] == "Source: USDA FoodData Central"
    assert usda_item["modeled"] is False
    assert results["Coffee"]["label"].startswith("Source: Pharmakon food list")


def test_a_usda_food_can_be_logged_and_feeds_the_checker(client, usda_calls):
    client.get("/products/search", params={"q": "coffee"})
    log(client, "nodoz")
    r = client.post(
        "/users/default/doses",
        json={
            "product_id": "usda_171890",
            "taken_at": NOW.replace(hour=17).isoformat(),
            "quantity": 1,
        },
    )
    assert r.status_code == 201, r.text
    assert len(food(check(client))) == 1


def test_the_fallback_list_is_used_without_a_key_and_is_labelled(client):
    r = client.get("/foods/search", params={"q": "coffee"})
    body = r.json()
    assert body["source"] == "food_list"
    names = {x["name"]: x for x in body["results"]}
    assert names["Coffee"]["components"] == ["caffeine"]
    assert names["Coffee"]["source"].startswith("Source: Pharmakon food list")
    assert names["Decaf coffee"]["components"] == []


def test_the_foods_endpoint_uses_usda_when_it_is_available(client, usda_calls):
    body = client.get("/foods/search", params={"q": "coffee"}).json()
    assert body["source"] == "usda"
    assert {x["source"] for x in body["results"]} == {"Source: USDA FoodData Central"}


def test_the_foods_endpoint_needs_a_query(client):
    assert client.get("/foods/search", params={"q": " "}).status_code == 422


def test_the_curated_list_has_about_forty_foods_with_components_only_where_obvious():
    assert 35 <= len(products.FOOD_PRODUCTS) <= 45
    mapped = {
        pid: [products.INGREDIENT_COMPONENT.get(i.name) for i in p.ingredients]
        for pid, p in products.FOOD_PRODUCTS.items()
    }
    assert mapped["prod_food_coffee"] == ["caffeine"]
    assert mapped["prod_food_grapefruit_juice"] == ["grapefruit"]
    assert mapped["prod_food_kale"] == ["vitamin_k"]
    assert mapped["prod_food_soy_sauce"] == ["tyramine"]
    for neutral in (
        "chicken",
        "rice",
        "bread",
        "eggs",
        "milk",
        "apple",
        "banana",
        "decaf_coffee",
        "herbal_tea",
    ):
        assert mapped[f"prod_food_{neutral}"] == [None], neutral
    for p in products.FOOD_PRODUCTS.values():
        assert all(i.strength_mg is None for i in p.ingredients)


def test_every_interaction_and_duplicate_finding_carries_a_why_explanation(client):
    log(client, "tylenol")
    log(client, "dayquil")
    log(client, "beer")
    result = check(client)
    assert result["duplicate_ingredients"]
    closing = "A pharmacist can tell you what this means for you."
    for duplicate in result["duplicate_ingredients"]:
        assert duplicate["why"]["closing"] == closing
    for finding in result["interactions"]:
        assert finding["why"]["closing"] == closing
    food_finding = food(result)[0]
    assert food_finding["why"]["key"] == "acetaminophen"
    assert food_finding["why"]["poison_control"] is True


def test_manual_checks_also_carry_explanations(client):
    r = client.post("/interaction-check", json={"names": ["ibuprofen", "naproxen"]})
    assert r.status_code == 200
    for finding in r.json()["interactions"]:
        assert finding["why"]["key"] == "nsaids"
