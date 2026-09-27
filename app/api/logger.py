"""Universal daily logger: freeform entries for anything a person wants to
track alongside doses -- skin condition, sleep quality, headaches, energy,
mood, stomach issues, a medication side effect, or anything else. `category`
and `value` are free text; `SUGGESTED_CATEGORIES` is a UI hint for the
dropdown, never an enum enforced here.

Storage only. A log entry never feeds app.pk or app.detect -- it carries no
substance, no dose, no PK parameters, so there is nothing for the engine or
the detectors to do with it. It exists so GET /users/{id}/timeline (and the
client's Timeline view) can show doses and self-reported entries on one
chronological axis. No correlation or causal inference happens here or there.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import DailyLog

DEFAULT_RANGE_DAYS = 30

# UI hint only -- category is freeform (see module docstring). A person can
# type anything; this list just seeds the dropdown with common choices.
SUGGESTED_CATEGORIES = [
    "skin",
    "sleep",
    "energy",
    "headache",
    "stomach",
    "mood",
    "side effect",
    "pain",
]


def create_log(
    session: Session,
    user_id: str,
    category: str,
    value: str,
    logged_at: datetime,
    created_at: datetime,
    body_location: str | None = None,
    notes: str | None = None,
) -> DailyLog:
    record = DailyLog(
        id=f"log_{uuid.uuid4().hex[:12]}",
        user_id=user_id,
        logged_at=logged_at.isoformat(),
        category=category,
        value=value,
        body_location=body_location,
        notes=notes,
        created_at=created_at.isoformat(),
    )
    session.add(record)
    session.flush()
    return record


def list_logs(
    session: Session,
    user_id: str,
    from_: datetime,
    to: datetime,
    category: str | None = None,
) -> list[DailyLog]:
    """Entries with `logged_at` in [from_, to] (inclusive), newest first."""
    q = select(DailyLog).where(DailyLog.user_id == user_id)
    if category is not None:
        q = q.where(DailyLog.category == category)
    records = [
        r
        for r in session.scalars(q)
        if from_ <= datetime.fromisoformat(r.logged_at) <= to
    ]
    records.sort(key=lambda r: datetime.fromisoformat(r.logged_at), reverse=True)
    return records


def delete_log(session: Session, user_id: str, log_id: str) -> bool:
    """True if a matching entry was deleted. False if it doesn't exist or
    belongs to a different user -- either way, nothing to the caller."""
    record = session.get(DailyLog, log_id)
    if record is None or record.user_id != user_id:
        return False
    session.delete(record)
    session.flush()
    return True


def log_json(record: DailyLog) -> dict:
    return {
        "id": record.id,
        "category": record.category,
        "value": record.value,
        "body_location": record.body_location,
        "notes": record.notes,
        "logged_at": record.logged_at,
        "created_at": record.created_at,
    }
