import secrets

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import main
from app.api.db import Base

pytestmark = pytest.mark.real_auth

PASSWORD = secrets.token_urlsafe(16)


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)

    def session_override():
        with maker() as s:
            yield s

    main.app.dependency_overrides[main.get_session] = session_override
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def test_authenticated_responses_are_no_store(client):
    r = client.post(
        "/auth/signup", json={"email": "a@example.com", "password": PASSWORD}
    )
    assert r.headers["cache-control"] == "no-store"
    user_id = r.json()["id"]
    for path in ("/auth/me", f"/users/{user_id}/doses", f"/users/{user_id}/survey"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store", path


def test_error_and_public_responses_are_no_store_too(client):
    assert client.get("/health").headers["cache-control"] == "no-store"
    assert client.get("/auth/me").headers["cache-control"] == "no-store"
    forbidden = client.post(
        "/auth/login",
        json={"email": "a@example.com", "password": "x" * 12},
        headers={"Origin": "https://evil.example"},
    )
    assert forbidden.status_code == 403
    assert forbidden.headers["cache-control"] == "no-store"
