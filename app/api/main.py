"""FastAPI app: POST /doses, GET /curve, GET /alerts (docs/api-contract.md), plus
the medication schedule (app/api/schedule.py).

v1 constraints, from the contract: a single hardcoded user ("default"), no
authentication, and the deployment runs on seeded synthetic data only.

Pharmakon is not a medical device. Outputs are estimates from a
one-compartment, population-average model, for educational use only.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..registry.loader import Registry, load_registry
from . import logger, openfda, schedule, service, side_effects, skincare
from .db import Base, make_engine
from .products import PRODUCTS, decompose, search

DEFAULT_USER = "default"

_engine = make_engine()
_SessionLocal = sessionmaker(bind=_engine)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(_engine)
    yield


app = FastAPI(title="Pharmakon", lifespan=lifespan)

# Vite's dev server. No credentials: v1 has no auth.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type"],
)


# --- Dependencies (overridden in tests) ---------------------------------------


def get_session() -> Iterator[Session]:
    with _SessionLocal() as session:
        yield session


def get_now() -> datetime:
    return datetime.now(UTC)


def get_registry() -> Registry:
    return load_registry()


def require_user(id: str) -> str:
    if id != DEFAULT_USER:
        raise HTTPException(404, f"unknown user {id!r}")
    return id


UserId = Annotated[str, Depends(require_user)]
Db = Annotated[Session, Depends(get_session)]
Reg = Annotated[Registry, Depends(get_registry)]
Now = Annotated[datetime, Depends(get_now)]


# --- Errors: {"error": {"code", "message"}} on every non-2xx -----------------


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message}}
    )


@app.exception_handler(StarletteHTTPException)
async def http_error(_req: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = "not_found" if exc.status_code == 404 else "http_error"
    return _error(exc.status_code, code, str(exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_error(_req: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    code = (
        "invalid_timestamp"
        if any(e["type"] == "timezone_aware" for e in errors)
        else "validation_error"
    )
    message = "; ".join(
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in errors
    )
    return _error(422, code, message)


# --- 1. POST /users/{id}/doses -------------------------------------------------


class DoseRequest(BaseModel):
    product_id: str
    taken_at: AwareDatetime
    quantity: float = Field(gt=0)


def _find_product(product_id: str):
    """The synthetic catalog, a product discovered via openFDA search, or one
    the user pasted an ingredient list for (app/api/skincare.py)."""
    product = PRODUCTS.get(product_id)
    if product is not None:
        return product
    product = openfda.get_discovered(product_id)
    if product is not None:
        return product
    return skincare.get_user_product(product_id)


def _topical_ingredients(product) -> list[str] | None:
    """Full ingredient list to store for the skincare checker, or None when
    this product isn't one it applies to. Never derived from the PK
    ingredient decomposition below -- these are unresolved label strings."""
    names = getattr(product, "topical_ingredients", None)
    if names:
        return list(names)
    if isinstance(product, skincare.UserEnteredProduct):
        return [ing.name for ing in product.ingredients]
    return None


@app.post("/users/{id}/doses", status_code=201)
def log_dose(
    body: DoseRequest,
    user_id: UserId,
    session: Db,
    registry: Reg,
    now: Now,
) -> dict:
    product = _find_product(body.product_id)
    if product is None:
        raise HTTPException(404, f"unknown product {body.product_id!r}")

    event, triggered = service.record_dose(
        session,
        registry,
        user_id,
        product_id=product.id,
        product_name=product.brand_name,
        taken_at=body.taken_at,
        quantity=body.quantity,
        ingredients=decompose(product, registry),
        topical_ingredients=_topical_ingredients(product),
        now=now,
    )
    session.commit()
    return {
        "dose_event_id": event.id,
        "triggered_alerts": [service.alert_json(r, registry) for r in triggered],
    }


@app.get("/users/{id}/doses")
def list_doses(user_id: UserId, session: Db, registry: Reg) -> list[dict]:
    """Every logged dose, oldest first, as decomposed at logging time."""
    events = sorted(
        service.load_doses(session, user_id),
        key=lambda ev: datetime.fromisoformat(ev.taken_at),
    )
    return [
        {
            "dose_event_id": ev.id,
            "product_id": ev.product_id,
            "product_name": ev.product_name,
            "taken_at": ev.taken_at,
            "quantity": ev.quantity,
            "ingredients": [
                service.ingredient_json(ing, registry) for ing in ev.ingredients
            ],
        }
        for ev in events
    ]


# --- Medication schedule ---------------------------------------------------------
#
# A plan, not a record: see app/api/schedule.py. Nothing here is a dose until
# it goes through .../take, which uses the same path as POST /doses.


class ScheduleRequest(BaseModel):
    product_id: str
    frequency: str
    times: list[str] = []
    days_of_week: list[str] | None = None
    notes: str | None = None
    # IANA name the times are read in, e.g. "America/Los_Angeles".
    timezone: str | None = None


class ScheduleUpdate(BaseModel):
    frequency: str | None = None
    times: list[str] | None = None
    days_of_week: list[str] | None = None
    notes: str | None = None
    active: Literal[False] | None = None  # deactivate only; no reactivation


class TakeRequest(BaseModel):
    taken_at: AwareDatetime | None = None  # default: now


def _get_schedule(session: Session, user_id: str, schedule_id: str):
    s = schedule.get(session, user_id, schedule_id)
    if s is None or not s.active:
        raise HTTPException(404, f"unknown schedule {schedule_id!r}")
    return s


@app.post("/users/{id}/schedule", status_code=201)
def create_schedule(
    body: ScheduleRequest, user_id: UserId, session: Db, registry: Reg, now: Now
) -> dict:
    product = _find_product(body.product_id)
    if product is None:
        raise HTTPException(404, f"unknown product {body.product_id!r}")
    ingredients = decompose(product, registry)
    try:
        record = schedule.create(
            session,
            user_id,
            product_id=product.id,
            product_name=product.brand_name,
            entries=schedule.snapshot(ingredients, registry),
            topical_ingredients=_topical_ingredients(product),
            frequency=body.frequency,
            times=body.times,
            days_of_week=body.days_of_week,
            notes=body.notes,
            timezone=body.timezone,
            now=now,
        )
    except schedule.InvalidSchedule as exc:
        return _error(422, "validation_error", str(exc))
    session.commit()
    return schedule.schedule_json(
        record, service.load_doses(session, user_id), registry, now
    )


@app.get("/users/{id}/schedule")
def list_schedule(user_id: UserId, session: Db, registry: Reg, now: Now) -> list[dict]:
    """Active scheduled medications, each with its status, next due time, when
    it was last logged (by any route) and the current concentration of each
    modeled substance in it."""
    events = service.load_doses(session, user_id)
    return [
        schedule.schedule_json(s, events, registry, now)
        for s in schedule.list_for_user(session, user_id)
    ]


@app.put("/users/{id}/schedule/{schedule_id}")
def update_schedule(
    schedule_id: str,
    body: ScheduleUpdate,
    user_id: UserId,
    session: Db,
    registry: Reg,
    now: Now,
) -> dict:
    s = _get_schedule(session, user_id, schedule_id)
    sent = body.model_fields_set
    try:
        schedule.update(
            s,
            frequency=body.frequency,
            times=body.times,
            days_of_week=body.days_of_week,
            days_given="days_of_week" in sent,
            notes=body.notes,
            notes_given="notes" in sent,
        )
    except schedule.InvalidSchedule as exc:
        session.rollback()
        return _error(422, "validation_error", str(exc))
    if body.active is False:
        schedule.deactivate(s, now)
    session.commit()
    return schedule.schedule_json(
        s, service.load_doses(session, user_id), registry, now
    )


@app.delete("/users/{id}/schedule/{schedule_id}", status_code=204)
def delete_schedule(schedule_id: str, user_id: UserId, session: Db, now: Now) -> None:
    """Soft delete: the schedule stops being active but its history stays."""
    s = schedule.get(session, user_id, schedule_id)
    if s is None:
        raise HTTPException(404, f"unknown schedule {schedule_id!r}")
    schedule.deactivate(s, now)
    session.commit()


@app.post("/users/{id}/schedule/{schedule_id}/take", status_code=201)
def take_scheduled(
    schedule_id: str,
    user_id: UserId,
    session: Db,
    registry: Reg,
    now: Now,
    body: TakeRequest | None = None,
) -> dict:
    """Log one unit of the scheduled product without searching. A real
    dose_event, made by the same code as POST /doses, so it is alerted on and
    charted like any other."""
    s = _get_schedule(session, user_id, schedule_id)
    event, triggered = service.record_dose(
        session,
        registry,
        user_id,
        product_id=s.product_id,
        product_name=s.product_name,
        taken_at=(body.taken_at if body and body.taken_at else now),
        quantity=1.0,
        ingredients=schedule.ingredients_of(s),
        topical_ingredients=s.topical_ingredients,
        now=now,
    )
    session.commit()
    return {
        "dose_event_id": event.id,
        "schedule_id": s.id,
        "triggered_alerts": [service.alert_json(r, registry) for r in triggered],
    }


# --- 2. GET /users/{id}/curve ----------------------------------------------------


@app.get("/users/{id}/curve")
def get_curve(
    substance_id: str,
    from_: Annotated[AwareDatetime, Query(alias="from")],
    to: AwareDatetime,
    user_id: UserId,
    session: Db,
    registry: Reg,
    now: Now,
    resolution: Literal["15m", "1h"] = "15m",
) -> dict:
    try:
        entry = registry.get(substance_id)
    except KeyError:
        raise HTTPException(404, f"unknown substance {substance_id!r}") from None
    if to <= from_:
        return _error(422, "validation_error", "`to` must be after `from`")

    events = service.load_doses(session, user_id)
    result = service.compute_curve(
        events,
        substance_id,
        registry,
        from_,
        to,
        resolution,
        now,
        scheduled=schedule.projected_doses(
            session, user_id, events, substance_id, now, to
        ),
    )
    return {
        "substance": {
            "id": substance_id,
            "display_name": entry["name"],
            "description": entry.get("description"),
        },
        "modelable": result.modelable,
        "reason": result.reason,
        "unit": "mg/L",
        "points": result.points,
        "threshold_concentration_mg_per_l": registry.threshold_concentration_mg_per_l(
            substance_id
        ),
        "peak": result.peak,
        "current": result.current,
        # Same curve plus future scheduled doses, from now on; null when no
        # schedule contributes. Never counted in `points`, `peak` or alerts.
        "projected_points": result.projected_points,
        "notes": result.notes,
    }


# --- 3. GET /users/{id}/alerts ----------------------------------------------------


@app.get("/users/{id}/alerts")
def get_alerts(
    user_id: UserId,
    session: Db,
    registry: Reg,
    now: Now,
    active: Literal["true", "false", "all"] = "true",
) -> list[dict]:
    # Recompute first: an alert goes inactive only when recomputation stops
    # producing it (e.g. the 24h window rolled past), never on a timer.
    service.recompute_alerts(session, user_id, registry, now)
    session.commit()
    return [
        service.alert_json(r, registry)
        for r in service.list_alerts(session, user_id, active)
    ]


# --- 4. GET /products/search -------------------------------------------------------


@app.get("/products/search")
def search_products(q: str, registry: Reg) -> dict:
    """Synthetic catalog first, then openFDA by name, then by what it treats.

    People search for what they HAVE ("eczema"), not just what they TAKE
    ("hydrocortisone"), so a name-only search misses them. When the catalog
    and name search together return fewer than
    `openfda.MIN_RESULTS_BEFORE_CONDITION_FALLBACK` hits, this also searches
    the label's `indications_and_usage` and `purpose` fields. Every result
    carries `source` ("catalog" | "openfda") so the UI can mark the synthetic
    catalog's unverified strengths as distinct from real label data, and every
    ingredient's `unmodeled_reason` already says whether it can be modeled or
    is only logged (see docs/api-contract.md).

    The response is capped at `openfda.RESULT_LIMIT` (10) results, with
    `total` carrying the full pre-cap count so the client can show "Showing X
    of Y results" and let continued typing narrow the query instead.
    """
    if not q.strip():
        return _error(422, "validation_error", "q: must not be empty")

    results: list[dict] = []
    seen: set[str] = set()

    def add(product, source: str) -> None:
        key = product.brand_name.strip().lower()
        if key in seen:
            return
        seen.add(key)
        ingredients = [
            {
                "substance_id": ing.substance_id,
                "name": ing.name,
                "strength_mg": ing.strength_mg,
                "unmodeled_reason": ing.unmodeled_reason,
            }
            for ing in decompose(product, registry)
        ]
        results.append(
            {
                "id": product.id,
                "brand_name": product.brand_name,
                "ingredients": ingredients,
                "has_unmodeled_ingredients": any(
                    i["unmodeled_reason"] is not None for i in ingredients
                ),
                "source": source,
                # The synthetic catalog doesn't track route.
                "route": getattr(product, "route", None),
            }
        )

    for product in search(q, registry):
        add(product, "catalog")

    try:
        for product in openfda.search_by_name(q):
            add(product, "openfda")
        if len(results) < openfda.MIN_RESULTS_BEFORE_CONDITION_FALLBACK:
            for product in openfda.search_by_condition_fields(q):
                add(product, "openfda")
    except openfda.OpenFDAUnavailable:
        # openFDA being down is not a client error; degrade to whatever the
        # synthetic catalog already found rather than failing the search.
        pass

    return {"results": results[: openfda.RESULT_LIMIT], "total": len(results)}


# --- POST /symptom-check ------------------------------------------------------------


class SymptomCheckRequest(BaseModel):
    symptom: str = Field(max_length=100)


@app.post("/symptom-check")
def symptom_check(
    body: SymptomCheckRequest, session: Db, registry: Reg, now: Now
) -> dict:
    """v1 has one user, so this checks DEFAULT_USER's doses."""
    if len(side_effects.normalize_symptom(body.symptom)) < 3:
        return _error(422, "validation_error", "symptom: at least 3 letters")
    return side_effects.check_symptom(
        session, DEFAULT_USER, registry, now, body.symptom
    )


# --- GET /users/{id}/side-effect-profile --------------------------------------


@app.get("/users/{id}/side-effect-profile")
def get_side_effect_profile(
    user_id: UserId, session: Db, registry: Reg, now: Now
) -> dict:
    """Every documented side effect of every substance logged in the last 24
    hours, grouped by substance (see app/api/side_effects.py)."""
    return side_effects.side_effect_profile(session, user_id, registry, now)


# --- 5. Daily log --------------------------------------------------------------


class DailyLogRequest(BaseModel):
    category: str = Field(min_length=1)  # freeform; see app/api/logger.py
    value: str = Field(min_length=1)  # freeform
    body_location: str | None = None
    notes: str | None = None
    # When taken, not when logged; defaults to now. Same convention as
    # DoseRequest.taken_at.
    logged_at: AwareDatetime | None = None


def _log_range(
    from_: datetime | None, to: datetime | None, now: datetime
) -> tuple[datetime, datetime] | JSONResponse:
    range_from = from_ or (now - timedelta(days=logger.DEFAULT_RANGE_DAYS))
    range_to = to or now
    if range_to <= range_from:
        return _error(422, "validation_error", "`to` must be after `from`")
    return range_from, range_to


@app.post("/users/{id}/log", status_code=201)
def create_log(body: DailyLogRequest, user_id: UserId, session: Db, now: Now) -> dict:
    record = logger.create_log(
        session,
        user_id,
        category=body.category,
        value=body.value,
        logged_at=body.logged_at or now,
        created_at=now,
        body_location=body.body_location,
        notes=body.notes,
    )
    session.commit()
    return logger.log_json(record)


@app.get("/users/{id}/log")
def list_log(
    user_id: UserId,
    session: Db,
    now: Now,
    from_: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    to: AwareDatetime | None = None,
    category: str | None = None,
) -> list[dict]:
    span = _log_range(from_, to, now)
    if isinstance(span, JSONResponse):
        return span
    records = logger.list_logs(session, user_id, span[0], span[1], category)
    return [logger.log_json(r) for r in records]


@app.delete("/users/{id}/log/{log_id}", status_code=204)
def delete_log(log_id: str, user_id: UserId, session: Db) -> None:
    if not logger.delete_log(session, user_id, log_id):
        raise HTTPException(404, f"unknown log entry {log_id!r}")
    session.commit()


# --- 6. Timeline -----------------------------------------------------------------


@app.get("/users/{id}/timeline")
def get_timeline(
    user_id: UserId,
    session: Db,
    registry: Reg,
    now: Now,
    from_: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    to: AwareDatetime | None = None,
) -> list[dict]:
    """Doses and daily log entries merged onto one chronological axis, oldest
    first (same convention as GET /doses; the client reverses for display).

    This is a record, not an analysis: no correlation or causal claim is
    computed or attached here. Each entry carries a `type` ("dose" | "log")
    so the client can render them differently; nothing about the merge itself
    implies one affected the other.
    """
    span = _log_range(from_, to, now)
    if isinstance(span, JSONResponse):
        return span
    range_from, range_to = span

    entries: list[dict] = []
    for ev in service.load_doses(session, user_id):
        taken_at = datetime.fromisoformat(ev.taken_at)
        if not (range_from <= taken_at <= range_to):
            continue
        entries.append(
            {
                "type": "dose",
                "timestamp": ev.taken_at,
                "id": ev.id,
                "product_id": ev.product_id,
                "product_name": ev.product_name,
                "quantity": ev.quantity,
                "ingredients": [
                    service.ingredient_json(ing, registry) for ing in ev.ingredients
                ],
            }
        )
    for record in logger.list_logs(session, user_id, range_from, range_to):
        entries.append(
            {
                "type": "log",
                "timestamp": record.logged_at,
                "id": record.id,
                "category": record.category,
                "value": record.value,
                "body_location": record.body_location,
                "notes": record.notes,
            }
        )
    entries += schedule.timeline_entries(
        session,
        user_id,
        service.load_doses(session, user_id),
        now,
        range_from,
        range_to,
    )
    entries.sort(key=lambda e: datetime.fromisoformat(e["timestamp"]))
    return entries


# --- Skincare ingredient interaction checker -----------------------------------
#
# Not PK modeling: no curve, no concentration, no risk score. See
# app/api/skincare.py and app/data/ingredient_interactions.json.


class SkincareProductRequest(BaseModel):
    name: str = Field(min_length=1)
    ingredients: list[str] = Field(min_length=1)


class SkincareCheckRequest(BaseModel):
    products: list[SkincareProductRequest] = Field(min_length=1)


@app.post("/skincare-check")
def skincare_check(body: SkincareCheckRequest) -> dict:
    products = [
        skincare.Product(name=p.name, ingredients=tuple(p.ingredients))
        for p in body.products
    ]
    return skincare.check_ingredients(products)


# How far back to look for an eczema mention in the daily log when
# auto-checking logged skincare products.
ECZEMA_LOOKBACK_DAYS = 14


@app.get("/users/{id}/skincare-check")
def get_skincare_check(user_id: UserId, session: Db, now: Now) -> dict:
    """Automatically checks every topical product logged in the last 24
    hours against the same rules as POST /skincare-check, so the user doesn't
    have to re-enter ingredients for products they've already logged.
    Products with no stored ingredient list are skipped, not guessed at --
    see DoseEvent.topical_ingredients (app/api/db.py).
    """
    products = []
    for ev in service.load_doses(session, user_id):
        taken_at = datetime.fromisoformat(ev.taken_at)
        if not (-24.0 <= service.hours_since(taken_at, now) <= 0.0):
            continue
        if ev.topical_ingredients:
            products.append(
                skincare.Product(
                    name=ev.product_name, ingredients=tuple(ev.topical_ingredients)
                )
            )
    result = skincare.check_ingredients(products)

    recent_logs = logger.list_logs(
        session, user_id, now - timedelta(days=ECZEMA_LOOKBACK_DAYS), now
    )
    recent_skin_texts = [
        text for r in recent_logs for text in (r.category, r.value, r.notes) if text
    ]
    return skincare.annotate_eczema_context(result, recent_skin_texts)


# --- POST /products/user-entered -----------------------------------------------


class UserEnteredProductRequest(BaseModel):
    name: str = Field(min_length=1)
    ingredients: list[str] = Field(min_length=1)


@app.post("/products/user-entered", status_code=201)
def create_user_entered_product(body: UserEnteredProductRequest, registry: Reg) -> dict:
    """For products openFDA has no label for -- most cosmetics, since FDA
    regulates drugs, not cosmetics (app/api/openfda.py). The user pastes the
    ingredient list from the packaging; it's stored and made loggable exactly
    like a catalog or openFDA-discovered product, and checked against the
    skincare ingredient database, never against the PK model.
    """
    product = skincare.register_user_product(body.name, body.ingredients)
    ingredients = [
        {
            "substance_id": ing.substance_id,
            "name": ing.name,
            "strength_mg": ing.strength_mg,
            "unmodeled_reason": ing.unmodeled_reason,
        }
        for ing in decompose(product, registry)
    ]
    return {
        "id": product.id,
        "brand_name": product.brand_name,
        "ingredients": ingredients,
        "has_unmodeled_ingredients": any(
            i["unmodeled_reason"] is not None for i in ingredients
        ),
        "source": "user_entered",
        "route": "TOPICAL",
    }
