"""Universal daily logger: POST/GET/DELETE /users/{id}/log (app/api/logger.py).

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


def create(client, **overrides):
    body = {"category": "skin", "value": "flare-up on left arm"} | overrides
    r = client.post("/users/default/log", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def list_log(client, **params):
    r = client.get("/users/default/log", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# --- POST /users/{id}/log -----------------------------------------------------


def test_create_and_read_back_log_entry(client):
    created = create(
        client,
        category="sleep",
        value="slept 4 hours",
        body_location=None,
        notes="woke up twice",
    )
    assert created["category"] == "sleep"
    assert created["value"] == "slept 4 hours"
    assert created["body_location"] is None
    assert created["notes"] == "woke up twice"
    assert created["logged_at"]
    assert created["created_at"]
    assert created["id"]

    (entry,) = list_log(client)
    assert entry == created


def test_logged_at_defaults_to_now(client):
    created = create(client)
    assert datetime.fromisoformat(created["logged_at"]) == NOW


def test_logged_at_can_be_set_to_earlier_today(client):
    created = create(client, logged_at=iso(3))
    assert datetime.fromisoformat(created["logged_at"]) == NOW - timedelta(hours=3)


def test_body_location_is_optional(client):
    created = create(
        client, category="pain", value="dull ache", body_location="left knee"
    )
    assert created["body_location"] == "left knee"


def test_freeform_category_and_value_accepted(client):
    """Not limited to the suggested list -- anything non-empty works."""
    created = create(
        client, category="shoulder mobility", value="better than yesterday, 7/10"
    )
    assert created["category"] == "shoulder mobility"
    assert created["value"] == "better than yesterday, 7/10"


@pytest.mark.parametrize("field", ["category", "value"])
def test_empty_field_is_rejected(client, field):
    body = {"category": "mood", "value": "okay"}
    body[field] = ""
    r = client.post("/users/default/log", json=body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_unknown_user_is_404(client):
    r = client.post("/users/someone/log", json={"category": "mood", "value": "okay"})
    assert r.status_code == 404


# --- GET /users/{id}/log ------------------------------------------------------


def test_filter_by_category(client):
    create(client, category="skin", value="clear")
    create(client, category="sleep", value="good night")

    skin_only = list_log(client, category="skin")
    assert {e["category"] for e in skin_only} == {"skin"}


def test_filter_by_date_range(client):
    create(client, category="headache", value="mild", logged_at=iso(48))
    recent = create(client, category="headache", value="gone", logged_at=iso(1))

    r = list_log(
        client,
        **{"from": iso(24), "to": iso(0)},
    )
    ids = {e["id"] for e in r}
    assert ids == {recent["id"]}


def test_default_range_is_last_30_days(client):
    old = create(client, category="mood", value="fine", logged_at=iso(31 * 24))
    recent = create(client, category="mood", value="great", logged_at=iso(29 * 24))

    entries = list_log(client)
    ids = {e["id"] for e in entries}
    assert recent["id"] in ids
    assert old["id"] not in ids


def test_range_end_must_be_after_start(client):
    r = client.get(
        "/users/default/log",
        params={"from": iso(1), "to": iso(2)},
    )
    assert r.status_code == 422


def test_newest_first(client):
    first = create(client, category="mood", value="a", logged_at=iso(2))
    second = create(client, category="mood", value="b", logged_at=iso(1))

    entries = list_log(client)
    assert [e["id"] for e in entries] == [second["id"], first["id"]]


# --- DELETE /users/{id}/log/{log_id} ------------------------------------------


def test_delete_removes_entry(client):
    created = create(client)
    r = client.delete(f"/users/default/log/{created['id']}")
    assert r.status_code == 204

    entries = list_log(client)
    assert entries == []


def test_delete_unknown_id_is_404(client):
    r = client.delete("/users/default/log/log_doesnotexist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
