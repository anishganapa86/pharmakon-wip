import re
import uuid

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import main
from app.api.db import Base

pytestmark = pytest.mark.real_auth

ALLOWLIST = frozenset(
    {("POST", "/auth/signup"), ("POST", "/auth/login"), ("GET", "/health")}
)
PASSWORD = "correct horse battery"
PARAM = re.compile(r"\{[^}]+\}")


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


def all_cases():
    cases = []
    for route in main.app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in sorted(route.methods):
            cases.append((method, route.path))
    return cases


def protected_cases():
    return [c for c in all_cases() if c not in ALLOWLIST]


def user_cases():
    return [c for c in protected_cases() if c[1].startswith("/users/{id}")]


def concrete(path, user_id="x"):
    return PARAM.sub("x", path.replace("{id}", user_id))


def sign_up(client, email):
    r = client.post("/auth/signup", json={"email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_the_sweep_sees_every_route():
    cases = all_cases()
    assert len(cases) >= 35
    assert ("GET", "/health") in cases
    assert ("GET", "/auth/me") in cases
    assert ("GET", "/users/{id}/curve") in cases
    assert ("POST", "/symptom-check") in cases


def test_allowlist_is_exactly_the_public_routes():
    assert main.PUBLIC_ROUTES == ALLOWLIST
    assert all(case in all_cases() for case in ALLOWLIST)


@pytest.mark.parametrize("method,path", protected_cases())
def test_every_protected_route_returns_401_when_logged_out(make_client, method, path):
    client = make_client()
    r = client.request(method, concrete(path))
    assert r.status_code == 401, f"{method} {path} -> {r.status_code}"
    assert r.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("method,path", protected_cases())
def test_a_garbage_cookie_does_not_unlock_a_route(make_client, method, path):
    client = make_client()
    client.cookies.set("pharmakon_session", "not-a-real-session")
    r = client.request(method, concrete(path))
    assert r.status_code == 401, f"{method} {path} -> {r.status_code}"


def test_public_routes_do_not_require_login(make_client):
    client = make_client()
    assert client.get("/health").json() == {"status": "ok"}
    assert client.post("/auth/signup", json={}).status_code == 422
    assert client.post("/auth/login", json={}).status_code == 422


def test_a_new_route_is_protected_by_default(make_client):
    @main.app.get("/zz-added-later")
    def added_later() -> dict:
        return {"ok": True}

    try:
        client = make_client()
        assert client.get("/zz-added-later").status_code == 401
        sign_up(client, "a@example.com")
        assert client.get("/zz-added-later").status_code == 200
    finally:
        main.app.router.routes = [
            r for r in main.app.router.routes if r.path != "/zz-added-later"
        ]


def test_there_are_user_scoped_routes_to_check():
    assert len(user_cases()) >= 20


@pytest.mark.parametrize("method,path", user_cases())
def test_user_a_gets_403_on_every_route_of_user_b(make_client, method, path):
    a = make_client()
    sign_up(a, "a@example.com")
    b = make_client()
    b_id = sign_up(b, "b@example.com")
    r = a.request(method, concrete(path, b_id))
    assert r.status_code == 403, f"{method} {path} -> {r.status_code}"
    assert r.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize("method,path", user_cases())
def test_a_made_up_user_id_is_403_not_404(make_client, method, path):
    a = make_client()
    sign_up(a, "a@example.com")
    r = a.request(method, concrete(path, str(uuid.uuid4())))
    assert r.status_code == 403


def test_a_user_can_reach_their_own_routes(make_client):
    client = make_client()
    user_id = sign_up(client, "a@example.com")
    assert client.get(f"/users/{user_id}/doses").status_code == 200
    assert client.get(f"/users/{user_id}/alerts").status_code == 200
    assert client.get("/products/search", params={"q": "tylenol"}).status_code == 200


def test_the_old_default_user_id_is_not_special(make_client):
    client = make_client()
    sign_up(client, "a@example.com")
    assert client.get("/users/default/doses").status_code == 403


def test_logging_out_locks_the_routes_again(make_client):
    client = make_client()
    user_id = sign_up(client, "a@example.com")
    cookie = client.cookies["pharmakon_session"]
    assert client.get(f"/users/{user_id}/doses").status_code == 200
    client.post("/auth/logout")
    client.cookies.set("pharmakon_session", cookie)
    assert client.get(f"/users/{user_id}/doses").status_code == 401


def test_new_users_start_with_no_doses_and_no_symptom_matches(make_client):
    a = make_client()
    a_id = sign_up(a, "a@example.com")
    b = make_client()
    sign_up(b, "b@example.com")
    assert a.get(f"/users/{a_id}/doses").json() == []
    r = b.post("/symptom-check", json={"symptom": "headache"})
    assert r.status_code == 200


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_need_login_even_in_dev(make_client, monkeypatch, path):
    monkeypatch.setenv("PHARMAKON_DEV_DOCS", "1")
    assert make_client().get(path).status_code == 401


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_are_off_by_default(make_client, monkeypatch, path):
    monkeypatch.delenv("PHARMAKON_DEV_DOCS", raising=False)
    client = make_client()
    sign_up(client, "a@example.com")
    assert client.get(path).status_code == 404


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", "On"])
@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_on_for_truthy_values(make_client, monkeypatch, path, value):
    monkeypatch.setenv("PHARMAKON_DEV_DOCS", value)
    client = make_client()
    sign_up(client, "a@example.com")
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no", "enabled"])
def test_docs_stay_off_for_other_values(make_client, monkeypatch, value):
    monkeypatch.setenv("PHARMAKON_DEV_DOCS", value)
    client = make_client()
    sign_up(client, "a@example.com")
    assert client.get("/openapi.json").status_code == 404


def test_docs_flag_is_read_per_request(make_client, monkeypatch):
    client = make_client()
    sign_up(client, "a@example.com")
    monkeypatch.setenv("PHARMAKON_DEV_DOCS", "1")
    assert client.get("/openapi.json").status_code == 200
    monkeypatch.setenv("PHARMAKON_DEV_DOCS", "0")
    assert client.get("/openapi.json").status_code == 404
    monkeypatch.setenv("PHARMAKON_DEV_DOCS", "on")
    assert client.get("/openapi.json").status_code == 200


def test_openapi_schema_lists_the_api_but_not_the_docs_routes(make_client, monkeypatch):
    monkeypatch.setenv("PHARMAKON_DEV_DOCS", "1")
    client = make_client()
    sign_up(client, "a@example.com")
    paths = client.get("/openapi.json").json()["paths"]
    assert "/auth/login" in paths
    assert "/docs" not in paths
    assert "/openapi.json" not in paths
