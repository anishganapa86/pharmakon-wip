"""SQLite persistence for dose events and alerts, via SQLAlchemy.

Timestamps are stored as the ISO 8601 strings the client sent (offset
included), so a dose comes back with the offset it was logged in. Conversion
to engine hours happens in app.api.service, at the boundary.
"""

from __future__ import annotations

import os

from sqlalchemy import JSON, Boolean, Float, ForeignKey, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

DATABASE_URL = os.environ.get("PHARMAKON_DB_URL", "sqlite:///./pharmakon.db")


class Base(DeclarativeBase):
    pass


class DoseEvent(Base):
    __tablename__ = "dose_events"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    product_id: Mapped[str] = mapped_column(String)
    product_name: Mapped[str] = mapped_column(String)
    taken_at: Mapped[str] = mapped_column(String)  # ISO 8601 + offset
    quantity: Mapped[float] = mapped_column(Float)
    # Full ingredient list (active + inactive) as labelled, for topical/
    # cosmetic products only -- see app/api/skincare.py. Null for anything
    # not checked against the skincare ingredient database. Deliberately
    # separate from `ingredients` below: these are unresolved label strings,
    # never PK-decomposed, and never fed to app.pk or app.detect.
    topical_ingredients: Mapped[list | None] = mapped_column(JSON, nullable=True)

    ingredients: Mapped[list[DoseIngredient]] = relationship(
        back_populates="dose_event", cascade="all, delete-orphan", lazy="selectin"
    )


class DoseIngredient(Base):
    """One ingredient of a logged dose, as decomposed at logging time.

    Unmodeled ingredients are stored, never dropped: substance_id is null for
    `unmatched`, amount_mg is null for `unknown_strength`.
    """

    __tablename__ = "dose_ingredients"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    dose_event_id: Mapped[str] = mapped_column(ForeignKey("dose_events.id"))
    ingredient_name: Mapped[str] = mapped_column(String)
    substance_id: Mapped[str | None] = mapped_column(String, nullable=True)
    amount_mg: Mapped[float | None] = mapped_column(Float, nullable=True)
    unmodeled_reason: Mapped[str | None] = mapped_column(String, nullable=True)

    dose_event: Mapped[DoseEvent] = relationship(back_populates="ingredients")


class DailyLog(Base):
    """A freeform self-reported entry: skin, sleep, mood, a side effect,
    anything a person wants to track. Not tied to a dose or a substance --
    that's what lets it cover things the PK model has no opinion on.
    """

    __tablename__ = "daily_logs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    logged_at: Mapped[str] = mapped_column(String)  # ISO 8601 + offset
    category: Mapped[str] = mapped_column(String)
    value: Mapped[str] = mapped_column(String)
    body_location: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)  # ISO 8601 + offset


class ScheduledDose(Base):
    """A recurring medication the user plans to take (app/api/schedule.py).

    A schedule is a plan, not a record: nothing here is a dose. Only a
    DoseEvent (logged by hand or via POST .../schedule/{id}/take) is one, and
    only DoseEvents feed alerts.

    `substance_entries` snapshots the product's decomposition at creation, one
    entry per label ingredient, unmodeled ones included: {name, substance_id,
    amount_mg (per unit), display_name, unmodeled_reason}. Products found via
    openFDA or pasted by the user live in process memory only, so "Take now"
    could not re-resolve them after a restart. Soft-deleted rows are kept so
    the timeline can still say which past slots were taken or missed.
    """

    __tablename__ = "scheduled_doses"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    product_id: Mapped[str] = mapped_column(String)
    product_name: Mapped[str] = mapped_column(String)
    substance_entries: Mapped[list] = mapped_column(JSON)
    frequency: Mapped[str] = mapped_column(String)
    time_of_day: Mapped[list] = mapped_column(JSON)  # ["HH:MM", ...] local
    days_of_week: Mapped[list | None] = mapped_column(JSON, nullable=True)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[str] = mapped_column(String)  # ISO 8601 + offset
    # IANA name the HH:MM times are read in. Null means "the offset of the
    # request clock", which is only right for a client that never sent one.
    timezone: Mapped[str | None] = mapped_column(String, nullable=True)
    deactivated_at: Mapped[str | None] = mapped_column(String, nullable=True)
    # Same as DoseEvent.topical_ingredients, carried so a dose taken from the
    # schedule reaches the skincare checker exactly like a manually logged one.
    topical_ingredients: Mapped[list | None] = mapped_column(JSON, nullable=True)


class AlertRecord(Base):
    """A persisted alert. Active iff dismissed_at and resolved_at are null.

    Only recomputation sets resolved_at. Nothing in v1 sets dismissed_at.
    """

    __tablename__ = "alerts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String)
    severity: Mapped[str] = mapped_column(String)
    substance_id: Mapped[str | None] = mapped_column(String, nullable=True)
    message: Mapped[str] = mapped_column(String)
    notes: Mapped[list] = mapped_column(JSON)
    contributing_doses: Mapped[list] = mapped_column(JSON)
    uncounted_doses: Mapped[list] = mapped_column(JSON)
    computed_at: Mapped[str] = mapped_column(String)
    dismissed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_at: Mapped[str | None] = mapped_column(String, nullable=True)


def make_engine(url: str = DATABASE_URL):
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args)
