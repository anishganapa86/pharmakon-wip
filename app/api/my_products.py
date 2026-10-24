"""Persistent per-user skincare product library ("My products").

A person pastes a product's packaging ingredient list once, under its
brand/name/category, and can search for and log it again without re-typing.
The ingredient text is split with the same hardened splitter the curated
importer uses (app/api/skincare.split_ingredients): commas outside
parentheses, bullets, and newlines separate ingredients; a chemical name's
internal comma ("1,2-Hexanediol") and a parenthetical's internal comma both
stay whole. Nothing is reworded or reordered -- `raw_text` keeps exactly
what was pasted.

Unverified by construction: unlike app/data/skincare_products.json entries,
a user's own transcription is never checked against the product, so there
is no `verified` flag at all here, only UNVERIFIED_NOTE for display. These
rows are never read by app/api/alternatives.py's candidate pool (which loads
only app/data/skincare_products.json) -- a user-entered product must never
be offered to anyone, including that same user, as an alternative.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import UserProduct
from .products import LabelIngredient
from .skincare import split_ingredients

CATEGORIES = ("cleanser", "toner", "serum", "moisturizer", "sunscreen", "other")

UNVERIFIED_NOTE = (
    "You entered this list. We haven't verified it. Check it against your packaging."
)


class EmptyIngredientListError(ValueError):
    """Raised when the pasted text splits into no ingredients at all."""


def create(
    session: Session,
    user_id: str,
    brand: str,
    name: str,
    category: str,
    raw_text: str,
    created_at: datetime,
) -> UserProduct:
    ingredients = split_ingredients(raw_text)
    if not ingredients:
        raise EmptyIngredientListError(
            "ingredient list is empty after splitting raw_text"
        )
    record = UserProduct(
        id=f"myprod_{uuid.uuid4().hex[:12]}",
        user_id=user_id,
        brand=brand,
        name=name,
        category=category,
        ingredients=ingredients,
        raw_text=raw_text,
        created_at=created_at.isoformat(),
    )
    session.add(record)
    session.flush()
    return record


def list_for_user(session: Session, user_id: str) -> list[UserProduct]:
    """Newest first."""
    records = list(
        session.scalars(select(UserProduct).where(UserProduct.user_id == user_id))
    )
    records.sort(key=lambda r: r.created_at, reverse=True)
    return records


def get(session: Session, user_id: str, product_id: str) -> UserProduct | None:
    record = session.get(UserProduct, product_id)
    if record is None or record.user_id != user_id:
        return None
    return record


def delete(session: Session, user_id: str, product_id: str) -> bool:
    """True if a matching entry was deleted. False if it doesn't exist or
    belongs to a different user -- either way, nothing to the caller."""
    record = get(session, user_id, product_id)
    if record is None:
        return False
    session.delete(record)
    session.flush()
    return True


def search(records: list[UserProduct], q: str) -> list[UserProduct]:
    """Case-insensitive substring match on brand, name, or the two together
    ("brand name", as the product is displayed), over an already-scoped (one
    user's) list of records."""
    needle = q.strip().lower()
    if not needle:
        return []
    return [
        r
        for r in records
        if needle in r.brand.lower()
        or needle in r.name.lower()
        or needle in f"{r.brand} {r.name}".lower()
    ]


def product_json(record: UserProduct) -> dict:
    return {
        "id": record.id,
        "brand": record.brand,
        "name": record.name,
        "category": record.category,
        "ingredients": list(record.ingredients),
        "raw_text": record.raw_text,
        "created_at": record.created_at,
    }


# --- Search / logging adapter ---------------------------------------------------


@dataclass(frozen=True)
class LoggableUserProduct:
    """A saved product shaped so app.api.products.decompose() and
    app.api.main's dose-logging path can treat it like any other loggable
    product -- the same adapter pattern as app.api.openfda.OpenFDAProduct
    and app.api.skincare.UserEnteredProduct. Topical/unmodeled, never a PK
    curve, for the same reason those two never produce one."""

    id: str
    brand_name: str
    ingredients: tuple[LabelIngredient, ...]
    topical_ingredients: tuple[str, ...]
    route: str = "TOPICAL"


def as_loggable(record: UserProduct) -> LoggableUserProduct:
    return LoggableUserProduct(
        id=record.id,
        brand_name=f"{record.brand} {record.name}".strip(),
        ingredients=tuple(
            LabelIngredient(name=n, strength_mg=None) for n in record.ingredients
        ),
        topical_ingredients=tuple(record.ingredients),
    )


def get_loggable(
    session: Session, user_id: str, product_id: str
) -> LoggableUserProduct | None:
    record = get(session, user_id, product_id)
    return None if record is None else as_loggable(record)
