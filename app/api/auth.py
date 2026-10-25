from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import AuthSession, User

COOKIE_NAME = "pharmakon_session"
PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 128
EMAIL_MAX_LENGTH = 254
SESSION_TTL = timedelta(days=7)
SLIDE_AFTER = timedelta(minutes=1)

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_hasher = PasswordHasher()
_dummy_hash: str | None = None


class InvalidEmailError(ValueError):
    pass


class InvalidPasswordError(ValueError):
    pass


class DuplicateEmailError(Exception):
    pass


def normalize_email(email: str) -> str:
    cleaned = email.strip().lower()
    if len(cleaned) > EMAIL_MAX_LENGTH or not _EMAIL_PATTERN.match(cleaned):
        raise InvalidEmailError("enter a valid email address")
    return cleaned


def validate_password(password: str) -> None:
    if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        raise InvalidPasswordError(
            f"password must be {PASSWORD_MIN_LENGTH}-{PASSWORD_MAX_LENGTH} characters"
        )


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def _get_dummy_hash() -> str:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(16))
    return _dummy_hash


def burn_verification(password: str) -> None:
    verify_password(_get_dummy_hash(), password)


def create_user(session: Session, email: str, password: str, now: datetime) -> User:
    cleaned = normalize_email(email)
    validate_password(password)
    if session.scalar(select(User).where(User.email == cleaned)) is not None:
        raise DuplicateEmailError(cleaned)
    user = User(
        id=str(uuid.uuid4()),
        email=cleaned,
        password_hash=hash_password(password),
        created_at=now.isoformat(),
    )
    session.add(user)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise DuplicateEmailError(cleaned) from exc
    return user


def authenticate(session: Session, email: str, password: str) -> User | None:
    try:
        cleaned = normalize_email(email)
    except InvalidEmailError:
        burn_verification(password)
        return None
    user = session.scalar(select(User).where(User.email == cleaned))
    if user is None:
        burn_verification(password)
        return None
    if not verify_password(user.password_hash, password):
        return None
    if _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    return user


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def start_session(session: Session, user_id: str, now: datetime) -> str:
    token = secrets.token_urlsafe(32)
    session.add(
        AuthSession(
            token_hash=hash_token(token),
            user_id=user_id,
            created_at=now.isoformat(),
            expires_at=(now + SESSION_TTL).isoformat(),
            last_seen_at=now.isoformat(),
        )
    )
    session.flush()
    return token


def resolve_session(
    session: Session, token: str, now: datetime
) -> tuple[User, bool] | None:
    record = session.get(AuthSession, hash_token(token))
    if record is None:
        return None
    if datetime.fromisoformat(record.expires_at) <= now:
        session.delete(record)
        session.flush()
        return None
    user = session.get(User, record.user_id)
    if user is None:
        session.delete(record)
        session.flush()
        return None
    slid = False
    if now - datetime.fromisoformat(record.last_seen_at) >= SLIDE_AFTER:
        record.last_seen_at = now.isoformat()
        record.expires_at = (now + SESSION_TTL).isoformat()
        slid = True
    return user, slid


def end_session(session: Session, token: str) -> None:
    session.execute(
        delete(AuthSession).where(AuthSession.token_hash == hash_token(token))
    )
    session.flush()
