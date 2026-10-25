import secrets
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import auth, demo_seed
from app.api.db import AlertRecord, Base, DoseEvent, ScheduledDose, User, UserSurvey
from app.registry.loader import load_registry

NOW = datetime(2026, 9, 13, 18, 0, tzinfo=UTC)
PASSWORD = secrets.token_urlsafe(16)
OTHER_PASSWORD = secrets.token_urlsafe(16)
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        yield session


@pytest.fixture
def registry():
    return load_registry()


def test_seed_creates_the_user_and_a_small_regimen(db, registry):
    result = demo_seed.seed(db, registry, "Demo@Example.com", PASSWORD, NOW)
    assert result.created_user is True
    assert result.doses_added == 6
    assert result.schedules_added == 1
    user = db.scalar(select(User))
    assert user.email == "demo@example.com"
    assert auth.authenticate(db, "demo@example.com", PASSWORD).id == result.user_id
    assert len(db.scalars(select(DoseEvent)).all()) == 6
    assert len(db.scalars(select(ScheduledDose)).all()) == 1


def test_seed_marks_the_demo_survey_completed(db, registry):
    result = demo_seed.seed(db, registry, "demo@example.com", PASSWORD, NOW)
    row = db.get(UserSurvey, result.user_id)
    assert row.status == "completed"
    assert row.tracks and row.goals and row.experience and row.guidance


def test_seed_does_not_overwrite_a_changed_survey(db, registry):
    first = demo_seed.seed(db, registry, "demo@example.com", PASSWORD, NOW)
    row = db.get(UserSurvey, first.user_id)
    row.status = "skipped"
    db.commit()
    demo_seed.seed(db, registry, "demo@example.com", PASSWORD, NOW)
    assert db.get(UserSurvey, first.user_id).status == "skipped"


def test_seeded_regimen_shows_a_duplication_alert(db, registry):
    demo_seed.seed(db, registry, "demo@example.com", PASSWORD, NOW)
    alerts = db.scalars(select(AlertRecord)).all()
    assert [a.kind for a in alerts] == ["duplication"]
    assert alerts[0].substance_id == "acetaminophen"
    assert alerts[0].severity == "caution"


def test_seeded_data_belongs_to_the_demo_user_only(db, registry):
    other = auth.create_user(db, "other@example.com", PASSWORD, NOW)
    result = demo_seed.seed(db, registry, "demo@example.com", PASSWORD, NOW)
    owners = {d.user_id for d in db.scalars(select(DoseEvent))}
    assert owners == {result.user_id}
    assert other.id != result.user_id


def test_seed_is_idempotent_and_keeps_the_password(db, registry):
    first = demo_seed.seed(db, registry, "demo@example.com", PASSWORD, NOW)
    again = demo_seed.seed(db, registry, "demo@example.com", OTHER_PASSWORD, NOW)
    assert again.created_user is False
    assert again.doses_added == 0
    assert again.user_id == first.user_id
    assert len(db.scalars(select(DoseEvent)).all()) == 6
    assert auth.authenticate(db, "demo@example.com", PASSWORD) is not None
    assert auth.authenticate(db, "demo@example.com", OTHER_PASSWORD) is None


def test_seed_rejects_a_short_password(db, registry):
    with pytest.raises(auth.InvalidPasswordError):
        demo_seed.seed(db, registry, "demo@example.com", "short", NOW)
    assert db.scalars(select(User)).all() == []


def run_script(env_extra, tmp_path):
    env = {
        "PATH": "",
        "SYSTEMROOT": "C:\\Windows",
        "PHARMAKON_DB_URL": f"sqlite:///{tmp_path / 'seed.db'}",
        "PHARMAKON_LABEL_CACHE_DIR": "",
        "PHARMAKON_DEMO_EMAIL": "",
        "PHARMAKON_DEMO_PASSWORD": "",
        **env_extra,
    }
    return subprocess.run(
        [sys.executable, "-I", str(ROOT / "scripts" / "seed_demo_user.py")],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
    )


def test_script_refuses_when_the_email_is_unset(tmp_path):
    result = run_script({"PHARMAKON_DEMO_PASSWORD": PASSWORD}, tmp_path)
    assert result.returncode == 2
    assert "PHARMAKON_DEMO_EMAIL" in result.stderr
    assert not (tmp_path / "seed.db").exists()


def test_script_refuses_when_the_password_is_unset(tmp_path):
    result = run_script({"PHARMAKON_DEMO_EMAIL": "demo@example.com"}, tmp_path)
    assert result.returncode == 2
    assert "PHARMAKON_DEMO_PASSWORD" in result.stderr
    assert not (tmp_path / "seed.db").exists()


def test_script_refuses_when_both_are_empty(tmp_path):
    result = run_script(
        {"PHARMAKON_DEMO_EMAIL": " ", "PHARMAKON_DEMO_PASSWORD": ""}, tmp_path
    )
    assert result.returncode == 2


def test_script_seeds_a_database_and_never_prints_the_password(tmp_path):
    result = run_script(
        {
            "PHARMAKON_DEMO_EMAIL": "demo@example.com",
            "PHARMAKON_DEMO_PASSWORD": PASSWORD,
        },
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert PASSWORD not in result.stdout + result.stderr
    assert "demo@example.com" in result.stdout
    engine = create_engine(f"sqlite:///{tmp_path / 'seed.db'}")
    with sessionmaker(bind=engine)() as session:
        assert len(session.scalars(select(User)).all()) == 1
        assert len(session.scalars(select(DoseEvent)).all()) == 6


def test_script_rejects_a_short_password_without_echoing_it(tmp_path):
    result = run_script(
        {"PHARMAKON_DEMO_EMAIL": "demo@example.com", "PHARMAKON_DEMO_PASSWORD": "tiny"},
        tmp_path,
    )
    assert result.returncode == 2
    assert "tiny" not in result.stdout + result.stderr.replace("password must", "")
