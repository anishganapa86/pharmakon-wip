from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import auth
from app.api.db import AuthSession, Base, User

NOW = datetime(2026, 9, 13, 18, 0, tzinfo=UTC)
GOOD_PASSWORD = "correct horse battery"


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


def test_hash_round_trip():
    hashed = auth.hash_password(GOOD_PASSWORD)
    assert hashed.startswith("$argon2id$")
    assert GOOD_PASSWORD not in hashed
    assert auth.verify_password(hashed, GOOD_PASSWORD)
    assert not auth.verify_password(hashed, GOOD_PASSWORD + "x")


def test_hashes_are_salted():
    assert auth.hash_password(GOOD_PASSWORD) != auth.hash_password(GOOD_PASSWORD)


def test_verify_rejects_malformed_hash():
    assert not auth.verify_password("not-a-hash", GOOD_PASSWORD)


def test_create_user_stores_hash_not_password(db):
    user = auth.create_user(db, "Person@Example.com", GOOD_PASSWORD, NOW)
    db.commit()
    stored = db.scalar(select(User).where(User.id == user.id))
    assert stored.email == "person@example.com"
    assert stored.password_hash.startswith("$argon2id$")
    assert GOOD_PASSWORD not in stored.password_hash
    assert stored.created_at == NOW.isoformat()


def test_email_is_trimmed_and_lowercased(db):
    user = auth.create_user(db, "  MiXeD@Example.COM  ", GOOD_PASSWORD, NOW)
    assert user.email == "mixed@example.com"


@pytest.mark.parametrize(
    "variant", ["dup@example.com", "DUP@example.com", " Dup@Example.Com "]
)
def test_duplicate_email_is_case_insensitive(db, variant):
    auth.create_user(db, "dup@example.com", GOOD_PASSWORD, NOW)
    db.commit()
    with pytest.raises(auth.DuplicateEmailError):
        auth.create_user(db, variant, GOOD_PASSWORD, NOW)
    assert len(db.scalars(select(User)).all()) == 1


@pytest.mark.parametrize(
    "email",
    ["", "plain", "a@b", "@example.com", "a b@example.com", "a@" + "b" * 250 + ".com"],
)
def test_invalid_email_rejected(db, email):
    with pytest.raises(auth.InvalidEmailError):
        auth.create_user(db, email, GOOD_PASSWORD, NOW)


def test_password_length_bounds(db):
    with pytest.raises(auth.InvalidPasswordError):
        auth.create_user(db, "a@example.com", "x" * 9, NOW)
    auth.create_user(db, "b@example.com", "x" * 10, NOW)
    auth.create_user(db, "c@example.com", "x" * 128, NOW)
    with pytest.raises(auth.InvalidPasswordError):
        auth.create_user(db, "d@example.com", "x" * 129, NOW)


def test_no_composition_rules(db):
    auth.create_user(db, "a@example.com", "aaaaaaaaaa", NOW)
    auth.create_user(db, "b@example.com", "          ", NOW)
    auth.create_user(db, "c@example.com", "1234567890", NOW)


def test_authenticate(db):
    user = auth.create_user(db, "a@example.com", GOOD_PASSWORD, NOW)
    assert auth.authenticate(db, "A@Example.com", GOOD_PASSWORD).id == user.id
    assert auth.authenticate(db, "a@example.com", "wrong password!") is None
    assert auth.authenticate(db, "nobody@example.com", GOOD_PASSWORD) is None
    assert auth.authenticate(db, "not an email", GOOD_PASSWORD) is None


def test_authenticate_burns_a_verification_for_unknown_email(db, monkeypatch):
    calls = []
    real = auth.verify_password

    def spy(stored_hash, password):
        calls.append(stored_hash)
        return real(stored_hash, password)

    monkeypatch.setattr(auth, "verify_password", spy)
    assert auth.authenticate(db, "nobody@example.com", GOOD_PASSWORD) is None
    assert len(calls) == 1
    assert calls[0].startswith("$argon2id$")


def test_session_stores_only_a_hash(db):
    user = auth.create_user(db, "a@example.com", GOOD_PASSWORD, NOW)
    token = auth.start_session(db, user.id, NOW)
    db.commit()
    rows = db.scalars(select(AuthSession)).all()
    assert len(rows) == 1
    assert rows[0].token_hash == auth.hash_token(token)
    assert token not in (rows[0].token_hash, rows[0].user_id)
    assert len(rows[0].token_hash) == 64


def test_session_tokens_are_unique(db):
    user = auth.create_user(db, "a@example.com", GOOD_PASSWORD, NOW)
    tokens = {auth.start_session(db, user.id, NOW) for _ in range(20)}
    assert len(tokens) == 20


def test_resolve_session(db):
    user = auth.create_user(db, "a@example.com", GOOD_PASSWORD, NOW)
    token = auth.start_session(db, user.id, NOW)
    resolved = auth.resolve_session(db, token, NOW + timedelta(seconds=5))
    assert resolved[0].id == user.id
    assert resolved[1] is False
    assert auth.resolve_session(db, "unknown-token", NOW) is None


def test_session_expires_after_seven_days(db):
    user = auth.create_user(db, "a@example.com", GOOD_PASSWORD, NOW)
    token = auth.start_session(db, user.id, NOW)
    assert auth.resolve_session(db, token, NOW + timedelta(days=7, seconds=-1))
    token2 = auth.start_session(db, user.id, NOW)
    assert auth.resolve_session(db, token2, NOW + timedelta(days=7)) is None
    assert db.get(AuthSession, auth.hash_token(token2)) is None


def test_session_expiry_slides(db):
    user = auth.create_user(db, "a@example.com", GOOD_PASSWORD, NOW)
    token = auth.start_session(db, user.id, NOW)
    day6 = NOW + timedelta(days=6)
    resolved = auth.resolve_session(db, token, day6)
    assert resolved[1] is True
    assert auth.resolve_session(db, token, day6 + timedelta(days=6))


def test_end_session(db):
    user = auth.create_user(db, "a@example.com", GOOD_PASSWORD, NOW)
    token = auth.start_session(db, user.id, NOW)
    auth.end_session(db, token)
    assert auth.resolve_session(db, token, NOW) is None
    auth.end_session(db, token)
