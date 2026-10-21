"""DrugBank import and the registry's modelability tiers.

Runs against tests/fixtures/drugbank_sample.xml (fictional drugs, in DrugBank's
format). Nothing here downloads DrugBank or touches the network.
"""

import json
import math
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import main, products, side_effects
from app.api.db import Base
from app.registry import import_drugbank as imp
from app.registry import loader
from app.registry.loader import REGISTRY_PATH, Registry

FIXTURE = Path(__file__).parent / "fixtures" / "drugbank_sample.xml"
NOW = datetime.fromisoformat("2026-09-13T18:00:00-07:00")


@pytest.fixture(scope="module")
def curated() -> Registry:
    return Registry(json.loads(REGISTRY_PATH.read_text())["substances"])


@pytest.fixture(scope="module")
def result(curated) -> imp.ImportResult:
    return imp.import_drugbank(FIXTURE, curated)


@pytest.fixture(scope="module")
def by_id(result) -> dict[str, dict]:
    return {e["id"]: e for e in result.entries}


# --- Parsing the fixture ------------------------------------------------------


def test_reads_only_top_level_drugs_and_the_release_version():
    version, drugs = imp.read_drugs(FIXTURE)
    assert version == "5.1.12"
    assert [d["name"] for d in drugs] == [
        "Examplamine",
        "Partialol",
        "Nulldrug",
        "Caffeine",
        "Rangeazole",
    ]  # the <drug> nested in a pathway is not an entry


def test_fully_parameterized_drug_gets_all_four_values(by_id):
    e = by_id["examplamine"]
    assert e["half_life_hours"] == 5.0
    assert e["vd_l_per_kg"] == 0.6
    assert e["bioavailability"] == 0.85  # the oral value, not the IV 100%
    assert e["ka_per_hour"] == 1.5
    assert e["partially_parameterized"] is False


def test_partially_parameterized_drug_gets_what_is_available(by_id):
    e = by_id["partialol"]
    assert e["half_life_hours"] == 12.0
    assert e["bioavailability"] == 0.6
    assert e["vd_l_per_kg"] is None  # "40 L" is absolute; converting needs a weight
    assert e["ka_per_hour"] is None
    assert e["partially_parameterized"] is True


def test_a_drug_with_no_pk_text_gets_nulls(by_id):
    e = by_id["nulldrug"]
    assert [e[k] for k in loader.PK_PARAMETERS] == [None] * 4
    assert e["partially_parameterized"] is True


def test_ambiguous_text_is_left_null_not_averaged(by_id):
    e = by_id["rangeazole"]  # "2-3 hours", "0.5-1 L/kg", two oral percentages
    assert [e[k] for k in loader.PK_PARAMETERS] == [None] * 4


def test_every_imported_entry_is_unverified_and_cites_the_release(result):
    assert result.entries
    for e in result.entries:
        assert e["verified"] is False
        assert e["source"] == "DrugBank Open Data 5.1.12"
        # Nothing DrugBank doesn't give us is filled in.
        assert e["threshold_24h_mg"] is None
        assert e["threshold_concentration_mg_per_l"] is None
        assert e["clearance_route"] == "unknown"
        assert e["kinetics_assumed"] is True


def test_synonyms_become_aliases_english_only(by_id):
    assert by_id["examplamine"]["aliases"] == ["Exampla-mine HCl", "EXM-100"]


# --- Curated entries win ------------------------------------------------------


def test_existing_registry_entries_are_not_imported_or_overwritten(
    result, by_id, curated
):
    assert "caffeine" not in by_id
    assert result.skipped_existing == ["Caffeine"]

    merged = Registry([*curated.all(), *result.entries])
    assert merged.get("caffeine") == curated.get("caffeine")
    assert merged.get("caffeine")["half_life_hours"] == 5.0  # not the fixture's 4
    assert merged.get("caffeine")["verified"] is False and "DrugBank" not in str(
        merged.get("caffeine")["source"]
    )


def test_an_alias_that_collides_with_a_curated_one_is_dropped(by_id, curated):
    assert curated.resolve("paracetamol") == "acetaminophen"
    assert by_id["rangeazole"]["aliases"] == ["RGZ"]  # "Paracetamol" was dropped
    merged = Registry([*curated.all(), *by_id.values()])
    assert merged.resolve("paracetamol") == "acetaminophen"


def test_even_a_colliding_entry_cannot_override_a_curated_one(curated):
    """Defense in depth: the loader itself is first-wins, whatever the importer
    did, so a stale or hand-edited import can't take over a curated id/alias."""
    hostile = {
        "id": "caffeine",
        "name": "Caffeine",
        "aliases": ["guaranine", "paracetamol"],
        "half_life_hours": 99.0,
        "vd_l_per_kg": 1.0,
        "bioavailability": 1.0,
        "ka_per_hour": 1.0,
        "clearance_route": "unknown",
        "threshold_concentration_mg_per_l": None,
    }
    merged = Registry([*curated.all(), hostile])
    assert merged.get("caffeine")["half_life_hours"] == 5.0
    assert merged.resolve("paracetamol") == "acetaminophen"


def test_two_imported_drugs_cannot_claim_the_same_name(curated):
    drugs = [
        {**d, "drugbank_id": f"X{i}", "synonyms": []}
        for i, d in enumerate(
            [
                {"name": "Twinol", "half_life": "3 hours"},
                {"name": "Twinol HCl", "half_life": "9 hours"},  # salt of the first
            ]
        )
    ]
    for d in drugs:
        d.setdefault("volume_of_distribution", None)
        d.setdefault("absorption", None)
    out = imp.build_entries("5.1.12", drugs, curated)
    assert [e["name"] for e in out.entries] == ["Twinol"]
    assert out.skipped_duplicate == ["Twinol HCl"]


# --- Free-text parsers ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "hours"),
    [
        ("5 hours", 5.0),
        ("The half-life is approximately 5.5 hrs.", 5.5),
        ("20 minutes", pytest.approx(1 / 3)),
        ("1.5 days", 36.0),
        ("2 weeks", 336.0),
        ("2-3 hours", None),
        ("2 to 3 hours", None),
        ("5 hours in adults, 8 hours in the elderly", None),
        ("Biphasic", None),
        ("", None),
        (None, None),
    ],
)
def test_half_life_parsing(text, hours):
    assert imp.parse_half_life_hours(text) == hours


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("0.6 L/kg", 0.6),
        ("Vd is 2 L/kg in adults.", 2.0),
        ("Approximately 40 L.", None),  # absolute volume
        ("5 mL/kg", None),  # a different unit, not L/kg
        ("0.5-1 L/kg", None),
        ("0.6 L/kg (adults) and 1.2 L/kg (children)", None),
        (None, None),
    ],
)
def test_volume_of_distribution_parsing(text, value):
    assert imp.parse_vd_l_per_kg(text) == value


@pytest.mark.parametrize(
    ("text", "fraction"),
    [
        ("Oral bioavailability is 85%. Intravenous bioavailability is 100%.", 0.85),
        ("Intravenous bioavailability is 100%. Oral bioavailability is 85%.", 0.85),
        ("The absolute bioavailability is 60%.", 0.6),  # unqualified
        ("Rectal bioavailability is 30%.", None),  # not oral, and no oral value
        ("Oral bioavailability is 40% (fasted) or 60% (fed).", None),  # two in one
        ("Oral bioavailability is 40%. Oral bioavailability is 60% with food.", None),
        ("Bioavailability is 50-70%.", None),
        ("Well absorbed from the gut.", None),  # nothing stated
        (None, None),
    ],
)
def test_oral_bioavailability_parsing(text, fraction):
    assert imp.parse_oral_bioavailability(text) == fraction


@pytest.mark.parametrize(
    ("text", "ka"),
    [
        ("The absorption rate constant (ka) is 1.5 h-1.", 1.5),
        ("ka = 2.0 per hour", 2.0),
        ("Absorption is rapid.", None),
        (None, None),
    ],
)
def test_ka_is_only_read_when_stated(text, ka):
    assert imp.parse_ka_per_hour(text) == ka


# --- CLI ----------------------------------------------------------------------


def test_cli_writes_a_loadable_registry_file(tmp_path, capsys):
    out = tmp_path / "imported.json"
    assert imp.main([str(FIXTURE), "--out", str(out)]) == 0
    assert "imported: 4" in capsys.readouterr().out

    data = json.loads(out.read_text())
    assert [e["id"] for e in data["substances"]] == [
        "examplamine",
        "nulldrug",
        "partialol",
        "rangeazole",
    ]
    Registry(data["substances"])  # every required key is present
    assert (
        loader.load_registry.__wrapped__(out).get("partialol")["half_life_hours"] == 12
    )


def test_cli_output_is_stable_across_runs(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    imp.main([str(FIXTURE), "--out", str(a)])
    imp.main([str(FIXTURE), "--out", str(b)])
    assert a.read_text() == b.read_text()


# --- Loader: the three tiers --------------------------------------------------


def test_modelability_follows_the_parameters_present(result, curated):
    registry = Registry([*curated.all(), *result.entries])
    assert registry.modelability("examplamine") == "full"
    assert registry.modelability("partialol") == "approximate"
    assert registry.modelability("nulldrug") == "none"
    assert registry.modelability("rangeazole") == "none"
    assert registry.modelability("caffeine") == "full"
    assert registry.modelability("ethanol") == "unsupported"  # zero-order


def test_half_life_alone_is_not_enough_to_draw_anything():
    e = {
        "id": "halfonly",
        "name": "Halfonly",
        "half_life_hours": 4.0,
        "vd_l_per_kg": None,
        "bioavailability": None,
        "ka_per_hour": None,
        "threshold_concentration_mg_per_l": None,
    }
    assert Registry([e]).modelability("halfonly") == "none"


def test_load_registry_merges_an_imported_file_after_the_curated_one(
    tmp_path, monkeypatch, result
):
    out = tmp_path / "substances_drugbank.json"
    out.write_text(json.dumps(imp.to_json(result)))
    monkeypatch.setattr(loader, "IMPORTED_PATHS", (out,))
    loader.load_registry.cache_clear()
    try:
        merged = loader.load_registry()
        assert merged.get("partialol")["source"] == "DrugBank Open Data 5.1.12"
        assert merged.get("caffeine")["half_life_hours"] == 5.0
    finally:
        loader.load_registry.cache_clear()


def test_load_registry_without_an_imported_file_is_just_the_curated_one(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(loader, "IMPORTED_PATHS", (tmp_path / "missing.json",))
    loader.load_registry.cache_clear()
    try:
        assert not any(
            "DrugBank" in str(e["source"]) for e in loader.load_registry().all()
        )
    finally:
        loader.load_registry.cache_clear()


# --- API: what each tier does -------------------------------------------------


@pytest.fixture
def client(result, curated, monkeypatch):
    registry = Registry([*curated.all(), *result.entries])
    for pid, name, label in [
        ("prod_full", "Examplamine 50", "Examplamine"),
        ("prod_partial", "Partialol 100", "Partialol"),
        ("prod_none", "Nulldrug 10", "Nulldrug"),
    ]:
        strength = {"Examplamine": 50, "Partialol": 100, "Nulldrug": 10}[label]
        monkeypatch.setitem(
            products.PRODUCTS,
            pid,
            products.Product(pid, name, (products.LabelIngredient(label, strength),)),
        )

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    def session_override():
        with SessionLocal() as s:
            yield s

    main.app.dependency_overrides[main.get_session] = session_override
    main.app.dependency_overrides[main.get_now] = lambda: NOW
    main.app.dependency_overrides[main.get_registry] = lambda: registry
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def log(client, product_id, hours_ago, quantity=1):
    r = client.post(
        "/users/default/doses",
        json={
            "product_id": product_id,
            "taken_at": (NOW - timedelta(hours=hours_ago)).isoformat(),
            "quantity": quantity,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def curve(client, substance_id, from_h=1, to_h=-30):
    return client.get(
        "/users/default/curve",
        params={
            "substance_id": substance_id,
            "from": (NOW - timedelta(hours=from_h)).isoformat(),
            "to": (NOW - timedelta(hours=to_h)).isoformat(),
        },
    ).json()


def test_full_import_gets_a_concentration_curve_that_says_kinetics_were_assumed(
    client,
):
    log(client, "prod_full", 0)
    body = curve(client, "examplamine")
    assert body["modelable"] is True
    assert body["fidelity"] == "full"
    assert body["unit"] == "mg/L"
    assert body["peak"]["concentration"] > 0
    assert any("first-order elimination is assumed" in n for n in body["notes"])


def test_partial_import_gets_a_simplified_decay_curve_with_the_note(client):
    log(client, "prod_partial", 0, quantity=2)  # 200 mg, F = 0.6
    body = curve(client, "partialol")

    assert body["modelable"] is True
    assert body["fidelity"] == "approximate"
    assert body["unit"] == "mg"  # an amount, not a concentration
    assert body["notes"][0] == "Approximate — missing distribution parameters"
    assert any("No volume of distribution" in n for n in body["notes"])
    assert body["threshold_concentration_mg_per_l"] is None

    at = {p["t"]: p["concentration"] for p in body["points"]}
    t0 = NOW.isoformat()
    assert at[t0] == pytest.approx(0.6 * 200)  # instant absorption: F * dose
    later = (NOW + timedelta(hours=12)).isoformat()  # one half-life (12 h)
    assert at[later] == pytest.approx(0.6 * 200 / 2, rel=1e-6)
    assert body["current"]["concentration"] == pytest.approx(0.6 * 200)


def test_approximate_curve_adds_doses_and_ignores_future_ones(client):
    log(client, "prod_partial", 12)  # a half-life ago: 60 mg -> 30 mg now
    log(client, "prod_partial", 0)  # 60 mg now
    assert curve(client, "partialol")["current"]["concentration"] == pytest.approx(
        30 + 60
    )


def test_a_substance_with_no_parameters_is_logged_but_never_charted(client):
    r = log(client, "prod_none", 1)
    assert r["triggered_alerts"] == []
    dose = client.get("/users/default/doses").json()[0]
    ing = dose["ingredients"][0]
    assert ing["substance_id"] == "nulldrug"  # recognized, and kept
    assert ing["unmodeled_reason"] == "no_pk_parameters"
    assert ing["amount_mg"] is None

    body = curve(client, "nulldrug")
    assert body["modelable"] is False
    assert "No pharmacokinetic parameters" in body["reason"]
    assert body["points"] == []


def test_a_substance_with_no_parameters_has_null_fidelity(client):
    log(client, "prod_none", 1)
    body = curve(client, "nulldrug")
    assert body["modelable"] is False
    assert body["fidelity"] is None


def test_search_names_the_unmodeled_reason_for_a_substance_with_no_parameters(client):
    results = client.get("/products/search", params={"q": "Nulldrug"}).json()
    ingredient = results["results"][0]["ingredients"][0]
    assert ingredient["unmodeled_reason"] == "no_pk_parameters"
    assert results["results"][0]["has_unmodeled_ingredients"] is True


def test_imported_substances_stay_quiet_however_much_is_logged(client):
    """No threshold, and no curve to accumulate on: nothing to say, so silence."""
    for hours_ago in (96, 72, 48, 24, 0):
        for pid in ("prod_partial", "prod_none", "prod_full"):
            log(client, pid, hours_ago, quantity=20)
    assert client.get("/users/default/alerts").json() == []


def test_side_effect_views_cope_with_an_approximate_substance(client, monkeypatch):
    # openFDA is never reached: every label lookup is a 404 "no matches".
    monkeypatch.setattr(
        side_effects,
        "_http",
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
    )
    side_effects.clear_cache()
    log(client, "prod_partial", 1)
    profile = client.get("/users/default/side-effect-profile")
    assert profile.status_code == 200
    entry = profile.json()["substances"][0]
    assert entry["current_level_mg_l"] is None  # an amount in mg is no level
    assert (
        client.post("/symptom-check", json={"symptom": "headache"}).status_code == 200
    )


def test_scheduled_approximate_substance_has_no_concentration_but_a_projection(
    client,
):
    r = client.post(
        "/users/default/schedule",
        json={"product_id": "prod_partial", "frequency": "daily", "times": ["20:00"]},
    )
    assert r.status_code == 201, r.text
    substance = r.json()["substances"][0]
    assert substance["current_concentration"] is None  # "—" in the client

    body = curve(client, "partialol", from_h=1, to_h=-6)
    assert body["fidelity"] == "approximate"
    assert body["projected_points"]  # the 20:00 dose, in mg
    assert max(p["concentration"] for p in body["projected_points"]) == pytest.approx(
        0.6 * 100 * math.exp(-math.log(2) / 12 * 0.5), rel=0.05
    )
