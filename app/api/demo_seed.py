from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..registry.loader import Registry, load_registry
from . import auth, schedule, service, survey
from .db import Base, DoseEvent, User, make_engine
from .products import PRODUCTS, decompose

EMAIL_ENV = "PHARMAKON_DEMO_EMAIL"
PASSWORD_ENV = "PHARMAKON_DEMO_PASSWORD"

DEMO_DOSES = (
    ("prod_nodoz_200", 1.0, 10.0),
    ("prod_tylenol_es_500", 2.0, 9.0),
    ("prod_tylenol_es_500", 2.0, 5.0),
    ("prod_advil_200", 1.0, 4.0),
    ("prod_dayquil_liquicap", 2.0, 3.0),
    ("prod_nyquil_liquicap", 2.0, 1.0),
)
DEMO_SCHEDULE = ("prod_nodoz_200", "daily", ["08:00"])


@dataclass
class SeedResult:
    user_id: str
    created_user: bool
    doses_added: int
    schedules_added: int


def add_regimen(
    session: Session, registry: Registry, user_id: str, now: datetime
) -> tuple[int, int]:
    for product_id, quantity, hours_ago in DEMO_DOSES:
        product = PRODUCTS[product_id]
        service.record_dose(
            session,
            registry,
            user_id,
            product_id=product.id,
            product_name=product.brand_name,
            taken_at=now - timedelta(hours=hours_ago),
            quantity=quantity,
            ingredients=decompose(product, registry),
            topical_ingredients=None,
            now=now,
        )
    product_id, frequency, times = DEMO_SCHEDULE
    product = PRODUCTS[product_id]
    schedule.create(
        session,
        user_id,
        product_id=product.id,
        product_name=product.brand_name,
        entries=schedule.snapshot(decompose(product, registry), registry),
        topical_ingredients=None,
        frequency=frequency,
        times=times,
        days_of_week=None,
        notes=None,
        timezone=None,
        now=now,
    )
    return len(DEMO_DOSES), 1


FOOD_SCENARIO = (
    ("prod_tylenol_es_500", 1.0, 8.0),
    ("prod_dayquil_liquicap", 1.0, 6.0),
    ("prod_food_coffee", 1.0, 4.0),
    ("prod_example_beer", 1.0, 2.0),
    ("prod_food_chicken", 1.0, 1.0),
)


def add_food_scenario(
    session: Session, registry: Registry, user_id: str, now: datetime
) -> int:
    for product_id, quantity, hours_ago in FOOD_SCENARIO:
        product = PRODUCTS[product_id]
        service.record_dose(
            session,
            registry,
            user_id,
            product_id=product.id,
            product_name=product.brand_name,
            taken_at=now - timedelta(hours=hours_ago),
            quantity=quantity,
            ingredients=decompose(product, registry),
            topical_ingredients=None,
            now=now,
        )
    return len(FOOD_SCENARIO)


def seed(
    session: Session,
    registry: Registry,
    email: str,
    password: str,
    now: datetime,
    food_scenario: bool = False,
) -> SeedResult:
    cleaned = auth.normalize_email(email)
    existing = session.scalar(select(User).where(User.email == cleaned))
    created = existing is None
    user = existing or auth.create_user(session, cleaned, password, now)
    dose_count = session.scalar(
        select(func.count()).select_from(DoseEvent).where(DoseEvent.user_id == user.id)
    )
    doses, schedules = (0, 0)
    if food_scenario and not dose_count:
        doses = add_food_scenario(session, registry, user.id, now)
    elif not dose_count:
        doses, schedules = add_regimen(session, registry, user.id, now)
    if survey.status_for(session, user.id) == "pending":
        survey.save(
            session,
            user.id,
            survey.SurveyUpdate(
                tracks=["medications", "supplements"],
                goals=["avoid_interactions", "reminders"],
                experience="some",
                guidance="minimal",
                status="completed",
            ),
            now,
        )
    session.commit()
    return SeedResult(user.id, created, doses, schedules)


def main(argv: list[str] | None = None) -> int:
    food_scenario = "--food-demo" in (argv if argv is not None else sys.argv[1:])
    email = os.environ.get(EMAIL_ENV, "").strip()
    password = os.environ.get(PASSWORD_ENV, "")
    if not email or not password:
        print(
            f"Set {EMAIL_ENV} and {PASSWORD_ENV} before seeding the demo user.",
            file=sys.stderr,
        )
        return 2
    try:
        auth.normalize_email(email)
        auth.validate_password(password)
    except (auth.InvalidEmailError, auth.InvalidPasswordError) as exc:
        print(f"Demo account not created: {exc}.", file=sys.stderr)
        return 2
    engine = make_engine()
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        result = seed(
            session,
            load_registry(),
            email,
            password,
            datetime.now(UTC),
            food_scenario=food_scenario,
        )
    state = "created" if result.created_user else "already exists"
    print(
        f"Demo user {auth.normalize_email(email)} {state}; "
        f"added {result.doses_added} doses and {result.schedules_added} schedule."
    )
    return 0
