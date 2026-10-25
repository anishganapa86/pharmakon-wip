"""FastAPI app: POST /doses, GET /curve, GET /alerts (docs/api-contract.md), plus
the medication schedule (app/api/schedule.py).

Every route except POST /auth/signup, POST /auth/login and GET /health needs a
logged-in session (app/api/auth.py). A /users/{id}/... route also needs {id}
to be the logged-in user.

Pharmakon is not a medical device. Outputs are estimates from a
one-compartment, population-average model, for educational use only.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..registry.loader import Registry, load_registry
from . import (
    ai,
    alternatives,
    auth,
    food_guidance,
    interactions,
    logger,
    my_products,
    openfda,
    schedule,
    service,
    settings,
    side_effects,
    skincare,
    survey,
)
from .db import Base, User, UserSurvey, make_engine
from .products import PRODUCTS, decompose, search
from .throttle import throttles

_engine = make_engine()
_SessionLocal = sessionmaker(bind=_engine)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(_engine)
    auth.warm_dummy_hash()
    yield


# --- Dependencies (overridden in tests) ---------------------------------------


def get_session() -> Iterator[Session]:
    with _SessionLocal() as session:
        yield session


def get_now() -> datetime:
    return datetime.now(UTC)


def get_registry() -> Registry:
    return load_registry()


def get_generator() -> ai.Generator:
    return ai.GeminiGenerator()


Db = Annotated[Session, Depends(get_session)]
Reg = Annotated[Registry, Depends(get_registry)]
Now = Annotated[datetime, Depends(get_now)]
Gen = Annotated[ai.Generator, Depends(get_generator)]


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        auth.COOKIE_NAME,
        token,
        max_age=int(auth.SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure(),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        auth.COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure(),
        path="/",
    )


def current_user_or_none(
    request: Request, response: Response, session: Db, now: Now
) -> User | None:
    token = request.cookies.get(auth.COOKIE_NAME)
    if not token:
        return None
    resolved = auth.resolve_session(session, token, now)
    if resolved is None:
        session.commit()
        return None
    user, slid = resolved
    if slid:
        session.commit()
        set_session_cookie(response, token)
    return user


OptionalUser = Annotated[User | None, Depends(current_user_or_none)]


def current_user(user: OptionalUser) -> User:
    if user is None:
        raise HTTPException(401, "authentication required")
    return user


CurrentUser = Annotated[User, Depends(current_user)]

PUBLIC_ROUTES = frozenset(
    {("POST", "/auth/signup"), ("POST", "/auth/login"), ("GET", "/health")}
)


def require_login(request: Request, user: OptionalUser) -> None:
    route = request.scope.get("route")
    path = route.path if route is not None else request.url.path
    if (request.method, path) in PUBLIC_ROUTES:
        return
    if user is None:
        raise HTTPException(401, "authentication required")


def require_user(id: str, user: CurrentUser) -> str:
    if id != user.id:
        raise HTTPException(403, "you can only access your own data")
    return id


UserId = Annotated[str, Depends(require_user)]

app = FastAPI(
    title="Pharmakon",
    lifespan=lifespan,
    dependencies=[Depends(require_login)],
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type"],
    expose_headers=["Retry-After"],
)

STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@app.middleware("http")
async def reject_foreign_origin(request: Request, call_next):
    origin = request.headers.get("origin")
    if (
        request.method in STATE_CHANGING_METHODS
        and origin is not None
        and origin.rstrip("/") not in settings.cors_origins()
    ):
        return _error(403, "forbidden", "Request origin is not allowed.")
    return await call_next(request)


def require_dev_docs() -> None:
    if not settings.env_flag("PHARMAKON_DEV_DOCS"):
        raise HTTPException(404, "not found")


@app.get(
    "/openapi.json", include_in_schema=False, dependencies=[Depends(require_dev_docs)]
)
def openapi_schema() -> JSONResponse:
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False, dependencies=[Depends(require_dev_docs)])
def swagger_docs() -> HTMLResponse:
    return get_swagger_ui_html(openapi_url="/openapi.json", title="Pharmakon API")


@app.get("/redoc", include_in_schema=False, dependencies=[Depends(require_dev_docs)])
def redoc_docs() -> HTMLResponse:
    return get_redoc_html(openapi_url="/openapi.json", title="Pharmakon API")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# --- Errors: {"error": {"code", "message"}} on every non-2xx -----------------


def _error(
    status: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
        headers=headers,
    )


@app.exception_handler(StarletteHTTPException)
async def http_error(_req: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = {401: "unauthorized", 403: "forbidden", 404: "not_found"}.get(
        exc.status_code, "http_error"
    )
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


class SignupRequest(BaseModel):
    email: str = Field(max_length=1000)
    password: str = Field(max_length=1000)


class LoginRequest(BaseModel):
    email: str = Field(max_length=1000)
    password: str = Field(max_length=1000)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def throttle_pair(ip: str, email: str) -> str:
    return f"{ip}|{email.strip().lower()[: auth.EMAIL_MAX_LENGTH]}"


def rate_limited(retry_after: int) -> JSONResponse:
    return _error(
        429,
        "rate_limited",
        "Too many attempts. Try again later.",
        headers={"Retry-After": str(retry_after)},
    )


def user_json(user: User) -> dict:
    return {"id": user.id, "email": user.email}


@app.post("/auth/signup", status_code=201)
def signup(
    body: SignupRequest, request: Request, response: Response, session: Db, now: Now
) -> dict:
    ip = client_ip(request)
    pair = throttle_pair(ip, body.email)
    retry = throttles.signup_retry_after(ip, pair)
    if retry:
        return rate_limited(retry)
    throttles.signup_attempted(ip)
    try:
        user = auth.create_user(session, body.email, body.password, now)
    except (auth.InvalidEmailError, auth.InvalidPasswordError) as exc:
        throttles.signup_failed(pair)
        return _error(422, "validation_error", str(exc))
    except auth.DuplicateEmailError:
        throttles.signup_failed(pair)
        return _error(409, "email_taken", "An account with that email already exists.")
    previous = request.cookies.get(auth.COOKIE_NAME)
    if previous:
        auth.end_session(session, previous)
    token = auth.start_session(session, user.id, now)
    survey.create_pending(session, user.id, now)
    session.commit()
    set_session_cookie(response, token)
    return user_json(user)


@app.post("/auth/login")
def login(
    body: LoginRequest, request: Request, response: Response, session: Db, now: Now
) -> dict:
    ip = client_ip(request)
    pair = throttle_pair(ip, body.email)
    retry = throttles.login_retry_after(ip, pair)
    if retry:
        return rate_limited(retry)
    user = auth.authenticate(session, body.email, body.password)
    if user is None:
        throttles.login_failed(ip, pair)
        return _error(401, "invalid_credentials", "Incorrect email or password.")
    throttles.login_succeeded(pair)
    previous = request.cookies.get(auth.COOKIE_NAME)
    if previous:
        auth.end_session(session, previous)
    token = auth.start_session(session, user.id, now)
    session.commit()
    set_session_cookie(response, token)
    return user_json(user)


@app.post("/auth/logout", status_code=204)
def logout(
    request: Request, response: Response, session: Db, user: CurrentUser
) -> None:
    token = request.cookies.get(auth.COOKIE_NAME)
    if token:
        auth.end_session(session, token)
        session.commit()
    clear_session_cookie(response)


@app.get("/auth/me")
def me(user: CurrentUser, session: Db) -> dict:
    return {**user_json(user), "survey_status": survey.status_for(session, user.id)}


@app.get("/users/{id}/survey")
def get_survey(user_id: UserId, session: Db) -> dict:
    return survey.survey_json(session.get(UserSurvey, user_id))


@app.put("/users/{id}/survey")
def put_survey(
    body: survey.SurveyUpdate, user_id: UserId, session: Db, now: Now
) -> dict:
    row = survey.save(session, user_id, body, now)
    session.commit()
    return survey.survey_json(row)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=1000)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage] = Field(min_length=1, max_length=40)


CHAT_TURNS = 10
CHAT_MAX_OUTPUT_TOKENS = 400


def ai_gate(
    generator: ai.Generator, user_id: str, per_hour: int
) -> JSONResponse | None:
    if isinstance(generator, ai.GeminiGenerator) and not ai.has_key():
        return _error(503, "ai_unavailable", ai.UNAVAILABLE_MESSAGE)
    blocked = ai.usage.try_use(user_id, per_hour)
    if blocked is None:
        return None
    kind, wait = blocked
    if kind == "daily":
        return _error(
            429,
            "ai_daily_cap",
            "The demo assistant has reached its daily limit. Try again tomorrow.",
            headers={"Retry-After": str(wait)},
        )
    return _error(
        429,
        "rate_limited",
        "You've reached the hourly limit for the demo assistant. Try again later.",
        headers={"Retry-After": str(wait)},
    )


def ai_failure(exc: Exception) -> JSONResponse:
    if isinstance(exc, ai.AIUnavailable):
        return _error(503, "ai_unavailable", ai.UNAVAILABLE_MESSAGE)
    if isinstance(exc, ai.AIQuotaExceeded):
        return _error(429, "ai_busy", ai.BUSY_MESSAGE)
    return _error(502, "ai_failed", ai.FAILED_MESSAGE)


@app.post("/users/{id}/chat")
def chat(body: ChatRequest, user_id: UserId, session: Db, generator: Gen):
    recent = body.messages[-CHAT_TURNS:]
    if recent[-1].role != "user":
        return _error(
            422, "validation_error", "messages: the last message must be from the user"
        )
    blocked = ai_gate(generator, user_id, ai.chat_per_hour())
    if blocked is not None:
        return blocked
    context = ai.survey_context(survey.survey_json(session.get(UserSurvey, user_id)))
    contents = [
        {
            "role": "user" if m.role == "user" else "model",
            "parts": [{"text": m.content}],
        }
        for m in recent
    ]
    try:
        text = generator.generate(
            system=ai.load_system_prompt(context),
            contents=contents,
            schema=ai.ChatAnswer,
            max_output_tokens=CHAT_MAX_OUTPUT_TOKENS,
        )
        return ai.parse_chat_answer(text)
    except (ai.AIUnavailable, ai.AIQuotaExceeded, ai.AIFailure) as exc:
        return ai_failure(exc)


# --- 1. POST /users/{id}/doses -------------------------------------------------


class DoseRequest(BaseModel):
    product_id: str
    taken_at: AwareDatetime
    quantity: float = Field(gt=0)


def _find_product(product_id: str, session: Session, user_id: str):
    """The synthetic catalog, a product discovered via openFDA search, a
    curated skincare entry (app/api/alternatives.py), one the user saved to
    their own library (app/api/my_products.py), or one they pasted an
    ingredient list for in the search box (app/api/skincare.py)."""
    product = PRODUCTS.get(product_id)
    if product is not None:
        return product
    product = openfda.get_discovered(product_id)
    if product is not None:
        return product
    product = alternatives.get_curated_product(product_id)
    if product is not None:
        return product
    product = my_products.get_loggable(session, user_id, product_id)
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
    product = _find_product(body.product_id, session, user_id)
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
            "topical": bool(ev.topical_ingredients),
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
    product = _find_product(body.product_id, session, user_id)
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
        "parameters_verified": bool(entry.get("verified", False)),
        "reason": result.reason,
        "unit": result.unit,
        # "full" is a concentration curve; "approximate" is the amount left in
        # the body, drawn when Vd or ka are missing (see registry.Modelability).
        "fidelity": result.fidelity,
        "points": result.points,
        # A concentration limit can't be compared against an amount in mg.
        "threshold_concentration_mg_per_l": (
            registry.threshold_concentration_mg_per_l(substance_id)
            if result.fidelity == "full"
            else None
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


CURATED_SKINCARE_LABEL = "From our curated skincare set"
CURATED_SKINCARE_UNVERIFIED_NOTE = (
    "Ingredient list copied from packaging; not yet independently verified"
)
MY_PRODUCT_LABEL = "My product (entered by you)"


@app.get("/products/search")
def search_products(q: str, registry: Reg, session: Db, user: CurrentUser) -> dict:
    """Synthetic catalog first, then curated skincare entries, then the
    user's own saved products, then openFDA by name, then by what it treats.

    People search for what they HAVE ("eczema"), not just what they TAKE
    ("hydrocortisone"), so a name-only search misses them. When the catalog
    and name search together return fewer than
    `openfda.MIN_RESULTS_BEFORE_CONDITION_FALLBACK` hits, this also searches
    the label's `indications_and_usage` and `purpose` fields. Every result
    carries `source` ("catalog" | "skincare_catalog" | "my_product" |
    "openfda") so the UI can mark the synthetic catalog's unverified
    strengths, and a curated/saved product's own verification state,
    distinctly from real label data, and every ingredient's
    `unmodeled_reason` already says whether it can be modeled or is only
    logged (see docs/api-contract.md).

    The response is capped at `openfda.RESULT_LIMIT` (10) results, with
    `total` carrying the full pre-cap count so the client can show "Showing X
    of Y results" and let continued typing narrow the query instead.
    """
    if not q.strip():
        return _error(422, "validation_error", "q: must not be empty")

    results: list[dict] = []
    seen: set[str] = set()

    def add(
        product,
        source: str,
        label: str | None = None,
        verification_note: str | None = None,
    ) -> None:
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
                "label": label,
                "verification_note": verification_note,
            }
        )

    for product in search(q, registry):
        add(product, "catalog")

    for curated in alternatives.search_curated(q):
        add(
            curated,
            "skincare_catalog",
            label=CURATED_SKINCARE_LABEL,
            verification_note=(
                None if curated.verified else CURATED_SKINCARE_UNVERIFIED_NOTE
            ),
        )

    for record in my_products.search(my_products.list_for_user(session, user.id), q):
        add(
            my_products.as_loggable(record),
            "my_product",
            label=MY_PRODUCT_LABEL,
            verification_note=my_products.UNVERIFIED_NOTE,
        )

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
    body: SymptomCheckRequest, session: Db, registry: Reg, now: Now, user: CurrentUser
) -> dict:
    """Checks the symptom against the logged-in user's own doses."""
    if len(side_effects.normalize_symptom(body.symptom)) < 3:
        return _error(422, "validation_error", "symptom: at least 3 letters")
    return side_effects.check_symptom(session, user.id, registry, now, body.symptom)


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
                "topical": bool(ev.topical_ingredients),
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
    ingredients: list[str] | None = None
    ingredients_text: str | None = None

    @model_validator(mode="after")
    def _resolve_ingredients(self) -> SkincareProductRequest:
        if (self.ingredients is None) == (self.ingredients_text is None):
            raise ValueError("provide exactly one of ingredients or ingredients_text")
        if self.ingredients_text is not None:
            self.ingredients = skincare.split_ingredients(self.ingredients_text)
        if not self.ingredients:
            raise ValueError("no ingredients found")
        return self


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


# --- Skincare alternatives -----------------------------------------------------
#
# Curated only: app/data/skincare_products.json, hand-sourced from
# manufacturer pages, never generated or fetched live. See
# app/api/alternatives.py.


def _medication_rejection(names: list[str], registry: Registry) -> JSONResponse | None:
    bad = alternatives.medication_ingredient_names(names, registry)
    if not bad:
        return None
    return _error(
        422,
        "validation_error",
        f"{', '.join(sorted(set(bad)))} looks like a medication ingredient, "
        "not a skincare ingredient. This checker covers topical/skincare "
        "products only.",
    )


@app.get("/users/{id}/skincare-alternatives")
def get_skincare_alternatives(
    user_id: UserId, session: Db, registry: Reg, now: Now, product_id: str
) -> dict:
    """Alternatives for one logged product's flagged ingredients (see
    app/api/alternatives.py), checked against everything else the user has
    logged topically in the last 24 hours."""
    events = service.load_doses(session, user_id)

    target_ev = next(
        (
            ev
            for ev in sorted(events, key=lambda e: e.taken_at, reverse=True)
            if ev.product_id == product_id and ev.topical_ingredients
        ),
        None,
    )
    if target_ev is None:
        if any(ev.product_id == product_id for ev in events):
            return _error(
                422,
                "validation_error",
                f"product {product_id!r} isn't a topical/skincare product; "
                "alternatives only apply to skincare products.",
            )
        raise HTTPException(404, f"unknown product {product_id!r}")

    rejection = _medication_rejection(list(target_ev.topical_ingredients), registry)
    if rejection is not None:
        return rejection

    target = skincare.Product(
        target_ev.product_name, tuple(target_ev.topical_ingredients)
    )
    regimen = []
    for ev in events:
        if ev.product_id == product_id or not ev.topical_ingredients:
            continue
        taken_at = datetime.fromisoformat(ev.taken_at)
        if -24.0 <= service.hours_since(taken_at, now) <= 0.0:
            regimen.append(
                skincare.Product(ev.product_name, tuple(ev.topical_ingredients))
            )

    return alternatives.check_alternatives_for_product(target, regimen)


class SkincareAlternativesRequest(BaseModel):
    products: list[SkincareProductRequest] = Field(min_length=1)


@app.post("/skincare-alternatives")
def post_skincare_alternatives(
    body: SkincareAlternativesRequest, registry: Reg
) -> dict:
    all_ingredients = [ing for p in body.products for ing in p.ingredients]
    rejection = _medication_rejection(all_ingredients, registry)
    if rejection is not None:
        return rejection
    products = [
        skincare.Product(name=p.name, ingredients=tuple(p.ingredients))
        for p in body.products
    ]
    return alternatives.check_alternatives_manual(products)


# --- Medication interaction checker ---------------------------------------------
#
# Informational only: this never creates or feeds an app.detect alert. See
# app/api/interactions.py and app/data/drug_interactions.json.


class InteractionCheckRequest(BaseModel):
    names: list[str] = Field(min_length=1)


@app.post("/interaction-check")
def interaction_check(body: InteractionCheckRequest, registry: Reg, now: Now) -> dict:
    return interactions.check_manual(body.names, registry, now)


@app.get("/users/{id}/interaction-check")
def get_interaction_check(
    user_id: UserId, session: Db, registry: Reg, now: Now
) -> dict:
    """All active scheduled medications plus doses logged in the last 24h,
    checked pairwise (see app/api/interactions.py)."""
    return interactions.check_regimen(session, user_id, registry, now)


# --- Food, drink, and timing guidance --------------------------------------------
#
# Informational only, oral medications only, never an alert. See
# app/api/food_guidance.py and app/data/food_drug_guidance.json.


class FoodGuidanceRequest(BaseModel):
    names: list[str] = Field(min_length=1)


@app.post("/food-guidance")
def post_food_guidance(body: FoodGuidanceRequest, registry: Reg) -> dict:
    return food_guidance.check_manual(body.names, registry)


@app.get("/users/{id}/food-guidance")
def get_food_guidance(user_id: UserId, session: Db, registry: Reg, now: Now) -> dict:
    """Active scheduled medications plus doses logged in the last 24h (see
    app/api/interactions.py's identical regimen definition)."""
    return food_guidance.check_regimen(session, user_id, registry, now)


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


# --- "My products": a persistent per-user skincare library ----------------------
#
# See app/api/my_products.py. Distinct from POST /products/user-entered above:
# that one is in-process-only and unscoped by user; these rows persist in the
# DB, are searchable later, and are never offered to anyone -- including the
# user who entered them -- as an alternative candidate (app/api/alternatives.py
# reads only app/data/skincare_products.json).


class MyProductRequest(BaseModel):
    brand: str = Field(min_length=1)
    name: str = Field(min_length=1)
    category: Literal["cleanser", "toner", "serum", "moisturizer", "sunscreen", "other"]
    raw_text: str = Field(min_length=1)


@app.post("/users/{id}/my-products", status_code=201)
def create_my_product(
    body: MyProductRequest, user_id: UserId, session: Db, now: Now
) -> dict:
    try:
        record = my_products.create(
            session,
            user_id,
            brand=body.brand,
            name=body.name,
            category=body.category,
            raw_text=body.raw_text,
            created_at=now,
        )
    except my_products.EmptyIngredientListError:
        return _error(
            422,
            "validation_error",
            "raw_text: no ingredients found after splitting",
        )
    session.commit()
    return my_products.product_json(record)


@app.get("/users/{id}/my-products")
def list_my_products(user_id: UserId, session: Db) -> list[dict]:
    return [
        my_products.product_json(r) for r in my_products.list_for_user(session, user_id)
    ]


@app.delete("/users/{id}/my-products/{product_id}", status_code=204)
def delete_my_product(product_id: str, user_id: UserId, session: Db) -> None:
    if not my_products.delete(session, user_id, product_id):
        raise HTTPException(404, f"unknown product {product_id!r}")
    session.commit()
