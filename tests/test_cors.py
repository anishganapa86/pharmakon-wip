import secrets

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.cors import CORSMiddleware

from app.api import main, settings
from app.api.db import Base, User

pytestmark = pytest.mark.real_auth

ALLOWED = "http://localhost:5173"
OTHER = "https://evil.example"
PASSWORD = secrets.token_urlsafe(16)


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
def client(maker):
    def session_override():
        with maker() as s:
            yield s

    main.app.dependency_overrides[main.get_session] = session_override
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def signup_body(email="a@example.com"):
    return {"email": email, "password": PASSWORD}


def test_defaults_are_the_vite_dev_origins(monkeypatch):
    monkeypatch.delenv("PHARMAKON_CORS_ORIGINS", raising=False)
    assert settings.cors_origins() == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


def test_origins_come_from_the_env_and_lose_trailing_slashes(monkeypatch):
    monkeypatch.setenv(
        "PHARMAKON_CORS_ORIGINS", "https://demo.example.com/, https://b.example.com"
    )
    assert settings.cors_origins() == [
        "https://demo.example.com",
        "https://b.example.com",
    ]


@pytest.mark.parametrize("raw", ["*", " * ", "*,*"])
def test_a_wildcard_alone_falls_back_to_the_defaults(monkeypatch, raw):
    monkeypatch.setenv("PHARMAKON_CORS_ORIGINS", raw)
    assert "*" not in settings.cors_origins()
    assert settings.cors_origins() == list(settings.DEFAULT_CORS_ORIGINS)


def test_a_wildcard_is_dropped_from_a_list(monkeypatch):
    monkeypatch.setenv("PHARMAKON_CORS_ORIGINS", "https://a.example.com,*")
    assert settings.cors_origins() == ["https://a.example.com"]


def test_cors_middleware_allows_credentials_and_never_a_wildcard():
    layers = [m for m in main.app.user_middleware if m.cls is CORSMiddleware]
    assert len(layers) == 1
    options = layers[0].kwargs
    assert options["allow_credentials"] is True
    assert "*" not in options["allow_origins"]
    assert options["allow_origins"]


def test_preflight_from_an_allowed_origin(client):
    r = client.options(
        "/auth/login",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == ALLOWED
    assert r.headers["access-control-allow-credentials"] == "true"


def test_preflight_from_another_origin_gets_no_cors_headers(client):
    r = client.options(
        "/auth/login",
        headers={"Origin": OTHER, "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in r.headers


def test_responses_to_an_allowed_origin_carry_credentials_headers(client):
    r = client.get("/health", headers={"Origin": ALLOWED})
    assert r.headers["access-control-allow-origin"] == ALLOWED
    assert r.headers["access-control-allow-credentials"] == "true"


def test_responses_to_another_origin_carry_no_cors_headers(client):
    r = client.get("/health", headers={"Origin": OTHER})
    assert "access-control-allow-origin" not in r.headers


def test_post_from_a_foreign_origin_is_rejected_and_does_nothing(client, maker):
    r = client.post("/auth/signup", json=signup_body(), headers={"Origin": OTHER})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"
    with maker() as s:
        assert s.scalars(select(User)).all() == []


@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_every_state_changing_method_checks_the_origin(client, method):
    signup = client.post(
        "/auth/signup", json=signup_body(), headers={"Origin": ALLOWED}
    )
    user_id = signup.json()["id"]
    r = getattr(client, method)(
        f"/users/{user_id}/schedule/x", headers={"Origin": OTHER}
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_a_logged_in_session_does_not_excuse_a_foreign_origin(client):
    client.post("/auth/signup", json=signup_body(), headers={"Origin": ALLOWED})
    r = client.post("/auth/logout", headers={"Origin": OTHER})
    assert r.status_code == 403
    assert client.get("/auth/me").status_code == 200


def test_post_from_an_allowed_origin_works(client):
    r = client.post("/auth/signup", json=signup_body(), headers={"Origin": ALLOWED})
    assert r.status_code == 201


def test_post_without_an_origin_header_works(client):
    assert client.post("/auth/signup", json=signup_body()).status_code == 201


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "http://localhost:5173.evil.example",
        "https://localhost:5173",
        "http://localhost:5174",
        "http://localhost",
    ],
)
def test_lookalike_origins_are_rejected(client, origin):
    r = client.post("/auth/login", json=signup_body(), headers={"Origin": origin})
    assert r.status_code == 403


def test_a_trailing_slash_on_the_origin_header_is_tolerated(client):
    r = client.post(
        "/auth/signup", json=signup_body(), headers={"Origin": ALLOWED + "/"}
    )
    assert r.status_code == 201


def test_get_is_not_blocked_by_the_origin_check(client):
    assert client.get("/health", headers={"Origin": OTHER}).status_code == 200


def test_env_override_is_read_per_request(client, monkeypatch):
    monkeypatch.setenv("PHARMAKON_CORS_ORIGINS", "https://demo.example.com")
    ok = client.post(
        "/auth/signup",
        json=signup_body("a@example.com"),
        headers={"Origin": "https://demo.example.com"},
    )
    assert ok.status_code == 201
    old = client.post(
        "/auth/login", json=signup_body("a@example.com"), headers={"Origin": ALLOWED}
    )
    assert old.status_code == 403


def test_retry_after_is_readable_by_browser_scripts(client):
    r = client.get("/health", headers={"Origin": ALLOWED})
    assert "retry-after" in r.headers["access-control-expose-headers"].lower()
