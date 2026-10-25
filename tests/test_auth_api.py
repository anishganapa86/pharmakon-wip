from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import auth, main
from app.api.db import AuthSession, Base, User

START = datetime(2026, 9, 13, 18, 0, tzinfo=UTC)
PASSWORD = "correct horse battery"


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


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
def client(maker, clock):
    def session_override():
        with maker() as s:
            yield s

    main.app.dependency_overrides[main.get_session] = session_override
    main.app.dependency_overrides[main.get_now] = clock
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def signup(client, email="person@example.com", password=PASSWORD):
    return client.post("/auth/signup", json={"email": email, "password": password})


def login(client, email="person@example.com", password=PASSWORD):
    return client.post("/auth/login", json={"email": email, "password": password})


def set_cookie_header(response):
    return response.headers["set-cookie"].lower()


def test_signup_creates_account_and_session(client):
    r = signup(client)
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"id", "email"}
    assert body["email"] == "person@example.com"
    assert client.get("/auth/me").json() == body


def test_signup_never_echoes_password_or_hash(client):
    r = signup(client)
    assert PASSWORD not in r.text
    assert "argon2" not in r.text


def test_signup_lowercases_email(client):
    assert (
        signup(client, " Person@Example.COM ").json()["email"] == "person@example.com"
    )


def test_signup_duplicate_email_is_409_and_case_insensitive(client):
    assert signup(client).status_code == 201
    r = signup(TestClient(main.app), "PERSON@example.com")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "email_taken"


@pytest.mark.parametrize("email", ["", "nope", "a@b", "a b@example.com"])
def test_signup_rejects_bad_email(client, email):
    r = signup(client, email)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize("password", ["x" * 9, "x" * 129, ""])
def test_signup_rejects_bad_password_length(client, password):
    r = signup(client, password=password)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_signup_accepts_boundary_passwords(client):
    assert signup(client, "a@example.com", "x" * 10).status_code == 201
    assert signup(TestClient(main.app), "b@example.com", "x" * 128).status_code == 201


def test_cookie_flags(client):
    header = set_cookie_header(signup(client))
    assert header.startswith(f"{auth.COOKIE_NAME}=")
    assert "httponly" in header
    assert "samesite=lax" in header
    assert "path=/" in header
    assert f"max-age={7 * 24 * 3600}" in header
    assert "secure" not in header


def test_cookie_secure_flag_follows_env(client, monkeypatch):
    monkeypatch.setenv("PHARMAKON_COOKIE_SECURE", "true")
    assert "secure" in set_cookie_header(signup(client)).split("; ")


@pytest.mark.parametrize("value", ["1", "TRUE", "Yes", "on"])
def test_cookie_secure_truthy_values(client, monkeypatch, value):
    monkeypatch.setenv("PHARMAKON_COOKIE_SECURE", value)
    assert "secure" in set_cookie_header(signup(client)).split("; ")


@pytest.mark.parametrize("value", ["", "0", "false", "off", "nope"])
def test_cookie_secure_off_values(client, monkeypatch, value):
    monkeypatch.setenv("PHARMAKON_COOKIE_SECURE", value)
    assert "secure" not in set_cookie_header(signup(client)).split("; ")


def test_raw_token_is_not_stored(client, maker):
    r = signup(client)
    token = r.cookies[auth.COOKIE_NAME]
    with maker() as s:
        rows = s.scalars(select(AuthSession)).all()
    assert [row.token_hash for row in rows] == [auth.hash_token(token)]
    assert token not in str(rows[0].__dict__)


def test_me_requires_login(client):
    r = client.get("/auth/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_me_returns_only_id_and_email(client):
    signup(client)
    assert set(client.get("/auth/me").json()) == {"id", "email"}


def test_me_rejects_garbage_cookie(client):
    client.cookies.set(auth.COOKIE_NAME, "garbage")
    assert client.get("/auth/me").status_code == 401


def test_login_success(client):
    signup(client)
    other = TestClient(main.app)
    r = login(other)
    assert r.status_code == 200
    assert r.json()["email"] == "person@example.com"
    assert auth.COOKIE_NAME in r.cookies
    assert other.get("/auth/me").status_code == 200


def test_login_email_is_case_insensitive(client):
    signup(client)
    assert login(TestClient(main.app), " PERSON@example.com ").status_code == 200


def test_login_failures_are_identical_for_unknown_email_and_wrong_password(client):
    signup(client)
    wrong_password = login(TestClient(main.app), password="not the password")
    unknown_email = login(TestClient(main.app), email="nobody@example.com")
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()
    assert wrong_password.json()["error"]["code"] == "invalid_credentials"
    assert "set-cookie" not in wrong_password.headers
    assert "set-cookie" not in unknown_email.headers


def test_login_issues_a_fresh_token_and_retires_the_presented_one(client, maker):
    signup(client)
    old = client.cookies[auth.COOKIE_NAME]
    r = login(client)
    new = r.cookies[auth.COOKIE_NAME]
    assert new != old
    with maker() as s:
        hashes = {row.token_hash for row in s.scalars(select(AuthSession))}
    assert auth.hash_token(old) not in hashes
    assert auth.hash_token(new) in hashes


def test_login_ignores_a_planted_cookie(client):
    signup(TestClient(main.app))
    client.cookies.set(auth.COOKIE_NAME, "attacker-chosen-token")
    r = login(client)
    assert r.cookies[auth.COOKIE_NAME] != "attacker-chosen-token"


def test_logout_deletes_session_and_clears_cookie(client, maker):
    signup(client)
    token = client.cookies[auth.COOKIE_NAME]
    r = client.post("/auth/logout")
    assert r.status_code == 204
    header = set_cookie_header(r)
    assert f"{auth.COOKIE_NAME}=" in header
    assert "max-age=0" in header
    with maker() as s:
        assert s.get(AuthSession, auth.hash_token(token)) is None
    client.cookies.set(auth.COOKIE_NAME, token)
    assert client.get("/auth/me").status_code == 401


def test_logout_requires_login(client):
    assert client.post("/auth/logout").status_code == 401


def test_session_expires_after_seven_days(client, clock):
    signup(client)
    clock.now = START + timedelta(days=7, seconds=-1)
    assert client.get("/auth/me").status_code == 200
    clock.now = START + timedelta(days=14)
    assert client.get("/auth/me").status_code == 401


def test_session_expiry_slides_and_refreshes_cookie(client, clock):
    signup(client)
    clock.now = START + timedelta(days=6)
    r = client.get("/auth/me")
    assert r.status_code == 200
    assert f"{auth.COOKIE_NAME}=" in set_cookie_header(r)
    clock.now = START + timedelta(days=12)
    assert client.get("/auth/me").status_code == 200


def test_expired_session_row_is_removed(client, clock, maker):
    signup(client)
    clock.now = START + timedelta(days=8)
    assert client.get("/auth/me").status_code == 401
    with maker() as s:
        assert s.scalars(select(AuthSession)).all() == []


def test_user_row_holds_hash_not_password(client, maker):
    signup(client)
    with maker() as s:
        user = s.scalar(select(User))
    assert user.password_hash.startswith("$argon2id$")
    assert PASSWORD not in user.password_hash


def test_sessions_are_independent_per_user(client):
    signup(client, "a@example.com")
    other = TestClient(main.app)
    signup(other, "b@example.com")
    assert client.get("/auth/me").json()["email"] == "a@example.com"
    assert other.get("/auth/me").json()["email"] == "b@example.com"
