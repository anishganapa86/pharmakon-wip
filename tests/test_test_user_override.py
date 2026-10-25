import pytest
from fastapi.testclient import TestClient

from app.api import main


def test_unmarked_tests_run_as_the_default_test_user():
    r = TestClient(main.app).get("/auth/me")
    assert r.status_code == 200
    assert r.json() == {"id": "default", "email": "default@example.com"}


@pytest.mark.real_auth
def test_real_auth_marker_removes_the_override():
    assert main.current_user_or_none not in main.app.dependency_overrides
    assert TestClient(main.app).get("/auth/me").status_code == 401
