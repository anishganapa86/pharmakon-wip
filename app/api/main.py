"""FastAPI app: POST /doses, GET /curve, GET /alerts (docs/api-contract.md).

v1 constraints, from the contract: a single hardcoded user ("default"), no
authentication, and the deployment runs on seeded synthetic data only.

Pharmakon is not a medical device. Outputs are estimates from a
one-compartment, population-average model, for educational use only.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..registry.loader import Registry, load_registry
from . import service
from .db import Base, DoseEvent, DoseIngredient, make_engine
from .products import PRODUCTS, decompose

DEFAULT_USER = "default"

_engine = make_engine()
_SessionLocal = sessionmaker(bind=_engine)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(_engine)
    yield


app = FastAPI(title="Pharmakon", lifespan=lifespan)


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


@app.post("/users/{id}/doses", status_code=201)
def log_dose(
    body: DoseRequest,
    user_id: UserId,
    session: Db,
    registry: Reg,
    now: Now,
) -> dict:
    product = PRODUCTS.get(body.product_id)
    if product is None:
        raise HTTPException(404, f"unknown product {body.product_id!r}")

    event = DoseEvent(
        id=f"dose_{uuid.uuid4().hex[:12]}",
        user_id=user_id,
        product_id=product.id,
        product_name=product.brand_name,
        taken_at=body.taken_at.isoformat(),
        quantity=body.quantity,
    )
    for ing in decompose(product, registry):
        event.ingredients.append(
            DoseIngredient(
                ingredient_name=ing.name,
                substance_id=ing.substance_id,
                amount_mg=(
                    ing.strength_mg * body.quantity
                    if ing.unmodeled_reason is None
                    else None
                ),
                unmodeled_reason=ing.unmodeled_reason,
            )
        )
    session.add(event)
    session.flush()

    triggered = service.recompute_alerts(session, user_id, registry, now)
    session.commit()
    return {
        "dose_event_id": event.id,
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

    result = service.compute_curve(
        service.load_doses(session, user_id),
        substance_id,
        registry,
        from_,
        to,
        resolution,
        now,
    )
    return {
        "substance": {"id": substance_id, "display_name": entry["name"]},
        "modelable": result.modelable,
        "reason": result.reason,
        "unit": "mg/L",
        "points": result.points,
        "threshold_concentration_mg_per_l": registry.threshold_concentration_mg_per_l(
            substance_id
        ),
        "peak": result.peak,
        "current": result.current,
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
