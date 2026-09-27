"""API tests: POST /doses, GET /curve, GET /alerts.

The clock is pinned through the get_now dependency so 24-hour windows are
deterministic. Each test gets a fresh in-memory SQLite database.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import main
from app.api.db import Base
from app.registry.loader import Registry, load_registry

NOW = datetime.fromisoformat("2026-09-13T18:00:00-07:00")


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock():
    return Clock(NOW)


@pytest.fixture
def client(clock):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    def session_override():
        with SessionLocal() as s:
            yield s

    main.app.dependency_overrides[main.get_session] = session_override
    main.app.dependency_overrides[main.get_now] = clock
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def iso(hours_before_now: float) -> str:
    return (NOW - timedelta(hours=hours_before_now)).isoformat()


def log(client, product_id, hours_ago, quantity=1):
    r = client.post(
        "/users/default/doses",
        json={
            "product_id": product_id,
            "taken_at": iso(hours_ago),
            "quantity": quantity,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def assert_traceable(alert):
    """The contract's Traceability rule and required fields, on every alert."""
    for key in (
        "id",
        "kind",
        "severity",
        "substance",
        "message",
        "notes",
        "computed_at",
        "contributing_doses",
        "uncounted_doses",
        "dismissed_at",
        "resolved_at",
    ):
        assert key in alert, key
    assert isinstance(alert["notes"], list)
    assert isinstance(alert["contributing_doses"], list)
    assert isinstance(alert["uncounted_doses"], list)
    assert alert["contributing_doses"] or alert["uncounted_doses"]
    assert alert["dismissed_at"] is None  # no dismissal in v1
    assert "payload" not in alert
    for d in alert["contributing_doses"]:
        assert set(d) == {
            "dose_event_id",
            "product_id",
            "product_name",
            "taken_at",
            "amount_mg",
        }


# --- POST /doses -------------------------------------------------------------


def test_logging_a_dose_returns_its_id_and_no_alerts_when_quiet(client):
    body = log(client, "prod_tylenol_es_500", 2, quantity=2)
    assert body["dose_event_id"]
    assert body["triggered_alerts"] == []


def test_unknown_product_is_404_with_error_body(client):
    r = client.post(
        "/users/default/doses",
        json={"product_id": "prod_nope", "taken_at": iso(1), "quantity": 1},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_unknown_user_is_404(client):
    r = client.post(
        "/users/someone/doses",
        json={"product_id": "prod_tylenol_es_500", "taken_at": iso(1), "quantity": 1},
    )
    assert r.status_code == 404


def test_timestamp_without_offset_is_rejected(client):
    r = client.post(
        "/users/default/doses",
        json={
            "product_id": "prod_tylenol_es_500",
            "taken_at": "2026-09-13T08:30:00",
            "quantity": 1,
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_timestamp"


@pytest.mark.parametrize("quantity", [0, -1])
def test_non_positive_quantity_is_rejected(client, quantity):
    r = client.post(
        "/users/default/doses",
        json={
            "product_id": "prod_tylenol_es_500",
            "taken_at": iso(1),
            "quantity": quantity,
        },
    )
    assert r.status_code == 422


def test_hidden_acetaminophen_across_products_triggers_duplication(client):
    """Tylenol + DayQuil. Neither product looks dangerous on its own."""
    a = log(client, "prod_tylenol_es_500", 12, quantity=2)  # 1000 mg
    b = log(client, "prod_dayquil_liquicap", 10, quantity=2)  # 650 mg
    c = log(client, "prod_tylenol_es_500", 6, quantity=2)  # 1000 mg
    assert a["triggered_alerts"] == b["triggered_alerts"] == c["triggered_alerts"] == []

    d = log(client, "prod_dayquil_liquicap", 2, quantity=2)  # 3300 of 4000
    assert len(d["triggered_alerts"]) == 1
    alert = d["triggered_alerts"][0]
    assert_traceable(alert)
    assert alert["kind"] == "duplication"
    assert alert["severity"] == "caution"
    assert alert["substance"] == {
        "id": "acetaminophen",
        "display_name": "Acetaminophen",
        "threshold_explanation": (
            "The 4000 mg daily limit is set by the FDA to prevent liver "
            "damage. Exceeding it repeatedly can cause acute liver failure. "
            "Risk increases with alcohol use or liver impairment."
        ),
        "description": "Pain reliever and fever reducer",
    }
    assert alert["uncounted_doses"] == []

    by_id = {x["dose_event_id"]: x for x in alert["contributing_doses"]}
    ids = [a, b, c, d]
    assert set(by_id) == {x["dose_event_id"] for x in ids}
    # amount_mg is the substance mass, not the product quantity.
    assert by_id[b["dose_event_id"]]["amount_mg"] == 650
    assert by_id[b["dose_event_id"]]["product_name"] == "DayQuil"
    assert by_id[a["dose_event_id"]]["taken_at"] == iso(12)


def test_an_already_active_alert_is_not_re_triggered(client):
    log(client, "prod_tylenol_es_500", 12, quantity=2)
    log(client, "prod_dayquil_liquicap", 10, quantity=2)
    log(client, "prod_tylenol_es_500", 6, quantity=2)
    log(client, "prod_dayquil_liquicap", 4, quantity=2)  # 3300: caution fires
    again = log(client, "prod_dayquil_liquicap", 2, quantity=1)  # 3625: still caution
    assert again["triggered_alerts"] == []


def test_breaching_the_limit_triggers_urgent_and_resolves_the_caution(client):
    log(client, "prod_tylenol_es_500", 12, quantity=2)
    log(client, "prod_dayquil_liquicap", 10, quantity=2)
    log(client, "prod_tylenol_es_500", 6, quantity=2)
    log(client, "prod_dayquil_liquicap", 4, quantity=2)  # 3300 caution
    r = log(client, "prod_tylenol_es_500", 1, quantity=2)  # 4300 urgent
    assert [a["severity"] for a in r["triggered_alerts"]] == ["urgent"]

    active = client.get("/users/default/alerts").json()
    assert [a["severity"] for a in active] == ["urgent"]
    inactive = client.get("/users/default/alerts", params={"active": "false"}).json()
    assert [a["severity"] for a in inactive] == ["caution"]
    assert inactive[0]["resolved_at"] is not None


def test_zero_order_substance_surfaces_as_unmodelable(client):
    r = log(client, "prod_example_beer", 1)
    kinds = [(a["kind"], a["severity"]) for a in r["triggered_alerts"]]
    assert kinds == [("unmodelable", "info")]
    assert_traceable(r["triggered_alerts"][0])


def test_unmodeled_ingredients_are_loggable_but_excluded_from_totals(client):
    """Unknown-strength acetaminophen must not be counted as 0 mg or guessed."""
    log(client, "prod_tylenol_es_500", 12, quantity=4)  # 2000
    log(client, "prod_example_cold_multi", 8, quantity=2)  # APAP strength unknown
    r = log(client, "prod_tylenol_es_500", 2, quantity=2)  # counted 3000
    (alert,) = r["triggered_alerts"]
    assert alert["kind"] == "threshold"  # only one product was *counted*
    names = {d["product_name"] for d in alert["contributing_doses"]}
    assert names == {"Tylenol Extra Strength"}


@pytest.mark.xfail(
    strict=True,
    reason=(
        "incomplete_data detector not implemented in app/detect "
        "(docs/api-contract.md, Implementation follow-ups)"
    ),
)
def test_unknown_strength_dose_raises_incomplete_data(client):
    log(client, "prod_tylenol_es_500", 12, quantity=2)
    r = log(client, "prod_example_cold_multi", 8, quantity=2)
    kinds = {a["kind"] for a in r["triggered_alerts"]}
    assert "incomplete_data" in kinds


# --- GET /curve ---------------------------------------------------------------


def curve(client, substance_id="caffeine", from_h=6, to_h=0, **extra):
    return client.get(
        "/users/default/curve",
        params={
            "substance_id": substance_id,
            "from": iso(from_h),
            "to": iso(to_h),
            **extra,
        },
    )


def test_curve_shape_with_no_doses(client):
    r = curve(client)
    assert r.status_code == 200
    body = r.json()
    assert body["substance"] == {
        "id": "caffeine",
        "display_name": "Caffeine",
        "description": "Stimulant found in coffee, tea, and energy drinks",
    }
    assert body["modelable"] is True
    assert body["reason"] is None
    assert body["unit"] == "mg/L"
    assert body["threshold_concentration_mg_per_l"] is None
    assert len(body["points"]) == 6 * 4 + 1
    assert all(p["concentration"] == 0 for p in body["points"])
    assert body["peak"] is None


def test_curve_reflects_logged_doses_in_mg_per_l(client):
    log(client, "prod_nodoz_200", 3)
    body = curve(client).json()
    ts = [datetime.fromisoformat(p["t"]) for p in body["points"]]
    assert all(t.tzinfo is not None for t in ts)
    assert ts == sorted(ts)
    assert ts[1] - ts[0] == timedelta(minutes=15)
    assert body["points"][0]["concentration"] == 0  # before the dose
    assert body["peak"]["concentration"] > 0
    # 200 mg into ~42 L: single-digit mg/L, not ug/mL-scale numbers.
    assert 1 < body["peak"]["concentration"] < 10
    assert body["current"]["concentration"] > 0
    assert datetime.fromisoformat(body["current"]["t"]) == NOW


def test_renal_substance_curve_notes_population_clearance(client):
    """No creatinine on the default patient: say so, don't hide it."""
    body = curve(client, substance_id="metformin").json()
    assert any("population clearance" in n for n in body["notes"])


def test_hepatic_substance_curve_has_empty_notes(client):
    assert curve(client).json()["notes"] == []


def test_unmodelable_curve_still_carries_notes(client):
    assert curve(client, substance_id="ethanol").json()["notes"] == []


def test_hourly_resolution_is_decimation_of_the_15m_grid(client):
    log(client, "prod_nodoz_200", 3)
    fine = curve(client).json()["points"]
    coarse = curve(client, resolution="1h").json()["points"]
    assert coarse == fine[::4]


@pytest.mark.parametrize("resolution", ["30m", "5m", "1H", ""])
def test_other_resolutions_are_rejected(client, resolution):
    assert curve(client, resolution=resolution).status_code == 422


def test_zero_order_substance_is_200_with_modelable_false(client):
    log(client, "prod_example_beer", 1)
    r = curve(client, substance_id="ethanol")
    assert r.status_code == 200
    body = r.json()
    assert body["modelable"] is False
    assert body["reason"]
    assert body["points"] == []
    assert body["peak"] is None and body["current"] is None
    assert body["unit"] == "mg/L"


def test_current_is_null_when_now_is_outside_the_window(client):
    log(client, "prod_nodoz_200", 10)
    body = curve(client, from_h=12, to_h=4).json()
    assert body["current"] is None
    assert body["peak"] is not None


def test_threshold_comes_from_the_registry(client):
    base = load_registry()
    entries = [dict(e) for e in base.all()]
    for e in entries:
        if e["id"] == "caffeine":
            e["threshold_concentration_mg_per_l"] = 15.0
    main.app.dependency_overrides[main.get_registry] = lambda: Registry(entries)
    assert curve(client).json()["threshold_concentration_mg_per_l"] == 15.0


def test_curve_rejects_bad_windows(client):
    assert curve(client, from_h=0, to_h=6).status_code == 422  # to before from
    r = client.get(
        "/users/default/curve",
        params={
            "substance_id": "caffeine",
            "from": "2026-09-13T08:00:00",
            "to": iso(0),
        },
    )
    assert r.status_code == 422


def test_unknown_substance_is_404(client):
    assert curve(client, substance_id="unobtainium").status_code == 404


# --- GET /alerts ----------------------------------------------------------------


def trigger_duplication(client):
    log(client, "prod_tylenol_es_500", 12, quantity=2)
    log(client, "prod_dayquil_liquicap", 10, quantity=2)
    log(client, "prod_tylenol_es_500", 6, quantity=2)
    log(client, "prod_dayquil_liquicap", 2, quantity=2)


def test_no_alerts_is_an_empty_array(client):
    r = client.get("/users/default/alerts")
    assert r.status_code == 200
    assert r.json() == []


def test_active_alerts_are_listed_by_default(client):
    trigger_duplication(client)
    alerts = client.get("/users/default/alerts").json()
    assert len(alerts) == 1
    assert_traceable(alerts[0])
    assert alerts[0]["resolved_at"] is None


@pytest.mark.parametrize("value", ["yes", "1", "True", ""])
def test_active_param_is_whitelisted(client, value):
    r = client.get("/users/default/alerts", params={"active": value})
    assert r.status_code == 422


def test_alert_resolves_by_recompute_once_the_window_rolls_past(client, clock):
    trigger_duplication(client)
    clock.now = NOW + timedelta(hours=30)

    assert client.get("/users/default/alerts").json() == []
    inactive = client.get("/users/default/alerts", params={"active": "false"}).json()
    assert len(inactive) == 1
    assert inactive[0]["resolved_at"] is not None
    assert inactive[0]["dismissed_at"] is None
    assert_traceable(inactive[0])

    everything = client.get("/users/default/alerts", params={"active": "all"}).json()
    assert len(everything) == 1


def test_still_over_the_limit_means_still_active(client, clock):
    """No auto-expiry: time passing alone doesn't clear an alert."""
    trigger_duplication(client)
    clock.now = NOW + timedelta(hours=1)
    assert len(client.get("/users/default/alerts").json()) == 1


def test_unknown_user_alerts_is_404(client):
    assert client.get("/users/nobody/alerts").status_code == 404


# --- CORS ----------------------------------------------------------------------


def test_cors_allows_the_vite_dev_server(client):
    r = client.options(
        "/users/default/doses",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_cors_does_not_allow_other_origins(client):
    r = client.get("/users/default/alerts", headers={"Origin": "http://evil.test"})
    assert "access-control-allow-origin" not in r.headers


# --- GET /products/search ------------------------------------------------------


def test_search_matches_brand_name_case_insensitively(client):
    r = client.get("/products/search", params={"q": "nyquil"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == len(body["results"])
    # "nyquil" also matches the catalog's "NyQuil Severe" entry.
    (product,) = [p for p in body["results"] if p["brand_name"] == "NyQuil"]
    assert set(product) == {
        "id",
        "brand_name",
        "ingredients",
        "has_unmodeled_ingredients",
        "source",
        "route",
    }
    assert product["source"] == "catalog"
    assert product["route"] is None
    by_name = {i["name"]: i for i in product["ingredients"]}
    assert by_name["ACETAMINOPHEN"]["substance_id"] == "acetaminophen"
    assert by_name["ACETAMINOPHEN"]["unmodeled_reason"] is None
    assert by_name["DOXYLAMINE SUCCINATE"]["substance_id"] == "doxylamine"
    assert by_name["DOXYLAMINE SUCCINATE"]["unmodeled_reason"] is None
    assert by_name["DEXTROMETHORPHAN HBR"]["substance_id"] == "dextromethorphan"
    assert by_name["DEXTROMETHORPHAN HBR"]["unmodeled_reason"] is None
    assert product["has_unmodeled_ingredients"] is False


def test_search_matches_ingredient_names(client):
    body = client.get("/products/search?q=acetaminophen").json()
    ids = {p["id"] for p in body["results"]}
    assert {
        "prod_tylenol_es_500",
        "prod_dayquil_liquicap",
        "prod_nyquil_liquicap",
    } <= ids
    assert "prod_advil_200" not in ids


def test_search_with_no_hits_is_an_empty_list(client):
    r = client.get("/products/search", params={"q": "zzzz"})
    assert r.status_code == 200
    assert r.json() == {"results": [], "total": 0}


def test_search_results_are_capped_with_total_reported(client):
    body = client.get("/products/search", params={"q": "a"}).json()
    assert len(body["results"]) <= 10
    assert body["total"] >= len(body["results"])


@pytest.mark.parametrize("q", ["", "   "])
def test_blank_search_is_422(client, q):
    r = client.get("/products/search", params={"q": q})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


# --- GET /users/{id}/doses -------------------------------------------------------


def test_dose_list_is_empty_with_no_doses(client):
    r = client.get("/users/default/doses")
    assert r.status_code == 200
    assert r.json() == []


def test_dose_list_returns_doses_oldest_first_with_ingredients(client):
    later = log(client, "prod_tylenol_es_500", 1, quantity=2)["dose_event_id"]
    earlier = log(client, "prod_nyquil_liquicap", 3)["dose_event_id"]
    doses = client.get("/users/default/doses").json()
    assert [d["dose_event_id"] for d in doses] == [earlier, later]
    tylenol = doses[1]
    assert tylenol["taken_at"] == iso(1)
    assert tylenol["quantity"] == 2
    assert tylenol["ingredients"] == [
        {
            "name": "ACETAMINOPHEN",
            "substance_id": "acetaminophen",
            "display_name": "Acetaminophen",
            "description": "Pain reliever and fever reducer",
            "amount_mg": 1000,
            "unmodeled_reason": None,
        }
    ]
