import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import main
from app.api.db import Base, UserSurvey

pytestmark = pytest.mark.real_auth

PASSWORD = "survey-test-password-1"
FULL = {
    "tracks": ["medications", "skincare"],
    "goals": ["avoid_interactions", "reminders"],
    "experience": "some",
    "guidance": "step_by_step",
    "status": "completed",
}


@pytest.fixture
def maker():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def make_client(maker):
    def session_override():
        with maker() as s:
            yield s

    main.app.dependency_overrides[main.get_session] = session_override
    yield lambda: TestClient(main.app)
    main.app.dependency_overrides.clear()


def sign_up(client, email):
    r = client.post("/auth/signup", json={"email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_signup_creates_a_pending_survey(make_client, maker):
    client = make_client()
    user_id = sign_up(client, "a@example.com")
    with maker() as s:
        row = s.scalar(select(UserSurvey).where(UserSurvey.user_id == user_id))
    assert row.status == "pending"
    assert row.tracks == [] and row.goals == []
    assert client.get("/auth/me").json()["survey_status"] == "pending"


def test_get_returns_the_pending_shape(make_client):
    client = make_client()
    user_id = sign_up(client, "a@example.com")
    body = client.get(f"/users/{user_id}/survey").json()
    assert set(body) == {
        "tracks",
        "goals",
        "experience",
        "guidance",
        "status",
        "updated_at",
    }
    assert body["status"] == "pending"
    assert body["experience"] is None and body["guidance"] is None


def test_round_trip(make_client):
    client = make_client()
    user_id = sign_up(client, "a@example.com")
    put = client.put(f"/users/{user_id}/survey", json=FULL)
    assert put.status_code == 200
    got = client.get(f"/users/{user_id}/survey").json()
    for key, value in FULL.items():
        assert got[key] == value
    assert got["updated_at"] is not None
    assert client.get("/auth/me").json()["survey_status"] == "completed"


def test_skip_keeps_partial_answers_and_sets_skipped(make_client):
    client = make_client()
    user_id = sign_up(client, "a@example.com")
    body = {
        "tracks": ["caffeine"],
        "goals": [],
        "experience": None,
        "guidance": None,
        "status": "skipped",
    }
    assert client.put(f"/users/{user_id}/survey", json=body).status_code == 200
    got = client.get(f"/users/{user_id}/survey").json()
    assert got["status"] == "skipped" and got["tracks"] == ["caffeine"]
    assert client.get("/auth/me").json()["survey_status"] == "skipped"


def test_a_later_put_replaces_the_answers(make_client):
    client = make_client()
    user_id = sign_up(client, "a@example.com")
    client.put(f"/users/{user_id}/survey", json=FULL)
    changed = {**FULL, "tracks": ["caffeine"], "guidance": "minimal"}
    client.put(f"/users/{user_id}/survey", json=changed)
    got = client.get(f"/users/{user_id}/survey").json()
    assert got["tracks"] == ["caffeine"] and got["guidance"] == "minimal"


def test_duplicate_choices_are_collapsed(make_client):
    client = make_client()
    user_id = sign_up(client, "a@example.com")
    body = {**FULL, "tracks": ["skincare", "skincare", "medications"]}
    r = client.put(f"/users/{user_id}/survey", json=body)
    assert r.json()["tracks"] == ["skincare", "medications"]


@pytest.mark.parametrize(
    "patch",
    [
        {"tracks": ["medications", "snake oil"]},
        {"goals": ["win"]},
        {"experience": "expert"},
        {"guidance": "loud"},
        {"status": "pending"},
        {"status": "done"},
        {"tracks": "medications"},
        {"tracks": ["medications"] * 6},
        {"notes": "free text is not allowed"},
        {"diagnosis": "none"},
    ],
)
def test_unknown_values_and_extra_fields_are_422(make_client, patch):
    client = make_client()
    user_id = sign_up(client, "a@example.com")
    r = client.put(f"/users/{user_id}/survey", json={**FULL, **patch})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
    assert client.get(f"/users/{user_id}/survey").json()["status"] == "pending"


def test_a_completed_survey_needs_every_answer(make_client):
    client = make_client()
    user_id = sign_up(client, "a@example.com")
    for missing in ("tracks", "goals", "experience", "guidance"):
        body = {**FULL, missing: [] if missing in ("tracks", "goals") else None}
        r = client.put(f"/users/{user_id}/survey", json=body)
        assert r.status_code == 422, missing


def test_a_missing_required_field_is_422(make_client):
    client = make_client()
    user_id = sign_up(client, "a@example.com")
    body = {k: v for k, v in FULL.items() if k != "status"}
    assert client.put(f"/users/{user_id}/survey", json=body).status_code == 422


def test_surveys_are_per_user(make_client):
    a = make_client()
    a_id = sign_up(a, "a@example.com")
    b = make_client()
    b_id = sign_up(b, "b@example.com")
    a.put(f"/users/{a_id}/survey", json=FULL)
    assert b.get(f"/users/{b_id}/survey").json()["status"] == "pending"
    assert b.get(f"/users/{b_id}/survey").json()["tracks"] == []
    assert b.get("/auth/me").json()["survey_status"] == "pending"
    assert a.get("/auth/me").json()["survey_status"] == "completed"


def test_user_cannot_read_or_write_another_users_survey(make_client):
    a = make_client()
    sign_up(a, "a@example.com")
    b = make_client()
    b_id = sign_up(b, "b@example.com")
    assert a.get(f"/users/{b_id}/survey").status_code == 403
    assert a.put(f"/users/{b_id}/survey", json=FULL).status_code == 403
    assert b.get(f"/users/{b_id}/survey").json()["status"] == "pending"


def test_logged_out_requests_are_401(make_client):
    client = make_client()
    assert client.get("/users/x/survey").status_code == 401
    assert client.put("/users/x/survey", json=FULL).status_code == 401


def test_a_user_without_a_row_reads_as_pending(make_client, maker):
    client = make_client()
    user_id = sign_up(client, "a@example.com")
    with maker() as s:
        s.delete(s.get(UserSurvey, user_id))
        s.commit()
    assert client.get("/auth/me").json()["survey_status"] == "pending"
    assert client.get(f"/users/{user_id}/survey").json()["status"] == "pending"
    assert client.put(f"/users/{user_id}/survey", json=FULL).status_code == 200
