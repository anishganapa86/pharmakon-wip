import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import main
from app.api.db import Base


@pytest.fixture
def memory_db():
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
    yield
    main.app.dependency_overrides.pop(main.get_session, None)


def test_unmarked_tests_run_as_the_default_test_user(memory_db):
    r = TestClient(main.app).get("/auth/me")
    assert r.status_code == 200
    assert r.json() == {
        "id": "default",
        "email": "default@example.com",
        "survey_status": "pending",
    }


@pytest.mark.real_auth
def test_real_auth_marker_removes_the_override():
    assert main.current_user_or_none not in main.app.dependency_overrides
    assert TestClient(main.app).get("/auth/me").status_code == 401
