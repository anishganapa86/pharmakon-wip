"""GET /users/{id}/timeline: doses and daily log entries merged on one axis.

Each test gets a fresh in-memory SQLite database and a pinned clock, same
convention as tests/test_api.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import main
from app.api.db import Base

NOW = datetime.fromisoformat("2026-09-13T18:00:00-07:00")


@pytest.fixture
def client():
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
    main.app.dependency_overrides[main.get_now] = lambda: NOW
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def iso(hours_before_now: float) -> str:
    return (NOW - timedelta(hours=hours_before_now)).isoformat()


def log_dose(client, hours_ago, product_id="prod_tylenol_es_500", quantity=1):
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


def log_entry(client, hours_ago, category="skin", value="clear"):
    r = client.post(
        "/users/default/log",
        json={"category": category, "value": value, "logged_at": iso(hours_ago)},
    )
    assert r.status_code == 201, r.text
    return r.json()


def timeline(client, **params):
    r = client.get("/users/default/timeline", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_timeline_merges_doses_and_logs_in_chronological_order(client):
    dose1 = log_dose(client, 5)
    entry = log_entry(client, 3)
    dose2 = log_dose(client, 1)

    result = timeline(client)
    assert [e["id"] for e in result] == [
        dose1["dose_event_id"],
        entry["id"],
        dose2["dose_event_id"],
    ]
    assert [e["type"] for e in result] == ["dose", "log", "dose"]


def test_dose_entry_shape(client):
    log_dose(client, 2, quantity=2)
    (entry,) = timeline(client)
    assert entry["type"] == "dose"
    assert entry["product_name"] == "Tylenol Extra Strength"
    assert entry["quantity"] == 2
    (ing,) = entry["ingredients"]
    assert ing["name"] == "ACETAMINOPHEN"
    assert ing["substance_id"] == "acetaminophen"
    assert ing["display_name"] == "Acetaminophen"
    assert ing["description"] == "Pain reliever and fever reducer"
    assert ing["amount_mg"] == 1000
    assert ing["unmodeled_reason"] is None


def test_log_entry_shape(client):
    log_entry(client, 2, category="headache", value="migraine")
    (entry,) = timeline(client)
    assert entry["type"] == "log"
    assert entry["category"] == "headache"
    assert entry["value"] == "migraine"
    assert entry["body_location"] is None
    assert entry["notes"] is None


def test_doses_without_any_logs_still_show(client):
    log_dose(client, 1)
    result = timeline(client)
    assert len(result) == 1
    assert result[0]["type"] == "dose"


def test_logs_without_any_doses_still_show(client):
    log_entry(client, 1)
    result = timeline(client)
    assert len(result) == 1
    assert result[0]["type"] == "log"


def test_empty_timeline_is_an_empty_list(client):
    assert timeline(client) == []


def test_filtering_by_date_range(client):
    log_dose(client, 48)
    recent_entry = log_entry(client, 1)

    result = timeline(client, **{"from": iso(24), "to": iso(0)})
    assert [e["id"] for e in result] == [recent_entry["id"]]


def test_log_entry_appears_immediately_after_creation(client):
    assert timeline(client) == []
    created = log_entry(client, 0)
    result = timeline(client)
    assert [e["id"] for e in result] == [created["id"]]


def test_default_range_is_last_30_days(client):
    log_dose(client, 31 * 24)
    recent = log_entry(client, 1)

    result = timeline(client)
    assert [e["id"] for e in result] == [recent["id"]]


def test_timeline_log_entry_id_is_the_id_the_delete_endpoint_takes(client):
    keep = log_entry(client, 3, value="keep")
    drop = log_entry(client, 2, value="drop")
    entries = timeline(client)
    drop_id = next(
        e["id"] for e in entries if e["type"] == "log" and e["value"] == "drop"
    )
    assert drop_id == drop["id"]

    r = client.delete(f"/users/default/log/{drop_id}")

    assert r.status_code == 204
    remaining = [e["id"] for e in timeline(client) if e["type"] == "log"]
    assert remaining == [keep["id"]]


def test_deleting_a_log_entry_leaves_doses_on_the_timeline(client):
    log_dose(client, 4)
    entry = log_entry(client, 2)

    assert client.delete(f"/users/default/log/{entry['id']}").status_code == 204

    assert [e["type"] for e in timeline(client)] == ["dose"]


def test_deleting_an_unknown_log_entry_is_a_404_not_a_silent_success(client):
    r = client.delete("/users/default/log/log_missing")

    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
