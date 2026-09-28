"""Medication schedule: recurring doses a person plans to take.

A schedule is a plan, not a record. Nothing in a ScheduledDose is ever counted
as taken. A slot is "taken" only when a real DoseEvent for the same product
falls inside its window, and only DoseEvents reach the alert detectors. That
is deliberate: an alert must trace to specific logged doses, and staying quiet
beats alerting on a dose nobody confirmed. The schedule does feed the curve,
but only as a separate, labelled projection of *future* slots (see
`projected_doses`); it never changes the actual curve.

Times are "HH:MM" wall-clock in the schedule's timezone. Everything is turned
into aware datetimes here, at the API boundary, before it reaches the engine.

Slots and windows
-----------------
Each slot owns a window, [slot - EARLY_WINDOW, next slot - EARLY_WINDOW),
capped at MAX_LATE after the slot. A dose of that product inside the window
satisfies that slot, and one dose satisfies at most one slot, so the slots of a
twice-daily schedule are tracked independently. A dose outside every window is
an ordinary dose, not a fulfilment.

A slot before the schedule was created, or after it was deactivated, does not
exist, so a new schedule is never born "overdue". `every_other_day` counts
from the day the schedule was created. Editing a schedule re-evaluates its
history against the new times.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..registry.loader import Registry
from . import service
from .db import DoseEvent, ScheduledDose
from .products import DecomposedIngredient

# frequency -> how many times a day (a scheduled day, for weekly) it is due.
SLOTS_PER_DAY = {
    "daily": 1,
    "twice_daily": 2,
    "three_times_daily": 3,
    "every_other_day": 1,
    "weekly": 1,
    "as_needed": 0,
}
WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

EARLY_WINDOW = timedelta(hours=2)  # a dose this early still counts for the slot
MAX_LATE = timedelta(hours=24)  # ...and one this late, at most
DUE_BEFORE = timedelta(minutes=30)  # "due" opens this long before the slot
DUE_AFTER = timedelta(minutes=60)  # ...and turns "overdue" this long after it
HORIZON = timedelta(days=8)  # far enough to see the next slot of any frequency

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class InvalidSchedule(ValueError):
    """The requested schedule is malformed; the message is shown to the user."""


# --- Validation ------------------------------------------------------------------


def validate(
    frequency: str, times: list[str], days_of_week: list[str] | None
) -> tuple[list[str], list[str] | None]:
    """Canonical (sorted times, ordered days) or InvalidSchedule."""
    if frequency not in SLOTS_PER_DAY:
        raise InvalidSchedule(f"frequency: must be one of {sorted(SLOTS_PER_DAY)}")

    expected = SLOTS_PER_DAY[frequency]
    if len(times) != expected:
        raise InvalidSchedule(
            f"times: {frequency} takes {expected} time(s), got {len(times)}"
            if expected
            else "times: as_needed has no scheduled times"
        )
    for t in times:
        if not _HHMM.match(t):
            raise InvalidSchedule(f"times: {t!r} is not a valid HH:MM time")
    if len(set(times)) != len(times):
        raise InvalidSchedule("times: each time must be different")

    if days_of_week is None:
        days = None
    else:
        days = [d.strip().lower() for d in days_of_week]
        bad = [d for d in days if d not in WEEKDAYS]
        if bad:
            raise InvalidSchedule(f"days_of_week: unknown day(s) {bad}")
        if not days:
            raise InvalidSchedule("days_of_week: give at least one day, or omit it")
        days = [d for d in WEEKDAYS if d in days]
    if days is not None and frequency in ("every_other_day", "as_needed"):
        raise InvalidSchedule(
            f"days_of_week: does not apply to {frequency} "
            "(every_other_day counts from the day it was created)"
        )
    return sorted(times), days


def resolve_timezone(name: str | None) -> str | None:
    if name is None:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise InvalidSchedule(f"timezone: unknown IANA timezone {name!r}") from None
    return name


# --- Slots -----------------------------------------------------------------------


@dataclass
class Slot:
    at: datetime
    window_end: datetime
    status: str = "upcoming"  # upcoming | due | overdue | taken | missed
    taken_at: datetime | None = None
    dose_event_id: str | None = None


def _tz(s: ScheduledDose, now: datetime) -> tzinfo:
    return ZoneInfo(s.timezone) if s.timezone else (now.tzinfo or ZoneInfo("UTC"))


def _instants(
    s: ScheduledDose, tz: tzinfo, start: datetime, end: datetime
) -> list[datetime]:
    """Scheduled instants in [start, end], inside the schedule's lifetime."""
    if s.frequency == "as_needed":
        return []
    created = datetime.fromisoformat(s.created_at)
    stop = datetime.fromisoformat(s.deactivated_at) if s.deactivated_at else None
    anchor: date = created.astimezone(tz).date()
    clock = [time.fromisoformat(t) for t in s.time_of_day]

    out = []
    day = start.astimezone(tz).date() - timedelta(days=1)
    last = end.astimezone(tz).date() + timedelta(days=1)
    while day <= last:
        on_day = (
            s.days_of_week is None or WEEKDAYS[day.weekday()] in s.days_of_week
        ) and (s.frequency != "every_other_day" or (day - anchor).days % 2 == 0)
        if on_day:
            for t in clock:
                at = datetime.combine(day, t, tzinfo=tz)
                if start <= at <= end and at >= created and (stop is None or at < stop):
                    out.append(at)
        day += timedelta(days=1)
    return sorted(out)


def evaluate(
    s: ScheduledDose,
    doses: list[DoseEvent],
    now: datetime,
    start: datetime,
    end: datetime,
) -> list[Slot]:
    """Every slot with `start <= at <= end`, statused as of `now`.

    `doses` are the user's dose events; only this schedule's product, and only
    those already taken (at or before `now`), can fulfil a slot.
    """
    tz = _tz(s, now)
    instants = _instants(s, tz, start, end + HORIZON)
    slots = []
    for i, at in enumerate(instants):
        following = instants[i + 1] - EARLY_WINDOW if i + 1 < len(instants) else None
        cap = at + MAX_LATE
        slots.append(Slot(at, min(following, cap) if following else cap))

    taken = sorted(
        (
            (datetime.fromisoformat(ev.taken_at), ev.id)
            for ev in doses
            if ev.product_id == s.product_id
            and datetime.fromisoformat(ev.taken_at) <= now
        ),
        key=lambda pair: pair[0],
    )
    used: set[str] = set()
    for slot in slots:
        for t, dose_id in taken:
            if dose_id not in used and slot.at - EARLY_WINDOW <= t < slot.window_end:
                slot.status, slot.taken_at, slot.dose_event_id = "taken", t, dose_id
                used.add(dose_id)
                break
        else:
            slot.status = _open_status(slot, now)
    return [sl for sl in slots if sl.at <= end]


def _open_status(slot: Slot, now: datetime) -> str:
    if now >= slot.window_end:
        return "missed"
    if now > slot.at + DUE_AFTER:
        return "overdue"
    if now >= slot.at - DUE_BEFORE:
        return "due"
    return "upcoming"


def summarize(
    s: ScheduledDose, doses: list[DoseEvent], now: datetime
) -> tuple[str, Slot | None, list[Slot]]:
    """(status, next slot to act on, today's slots) as of `now`.

    Status is overdue > due > taken_today > upcoming. A `missed` slot (its
    window has closed) no longer counts as overdue, so a lapsed morning dose
    does not keep the whole schedule red until midnight.
    """
    if s.frequency == "as_needed":
        return "as_needed", None, []
    slots = evaluate(s, doses, now, now - MAX_LATE - EARLY_WINDOW, now + HORIZON)
    tz = _tz(s, now)
    today = now.astimezone(tz).date()
    todays = [sl for sl in slots if sl.at.astimezone(tz).date() == today]
    pending = [sl for sl in slots if sl.status != "taken" and sl.status != "missed"]

    if any(sl.status == "overdue" for sl in pending):
        status = "overdue"
    elif any(sl.status == "due" for sl in pending):
        status = "due"
    elif any(sl.status == "taken" for sl in todays) and not any(
        sl.at.astimezone(tz).date() == today for sl in pending
    ):
        status = "taken_today"
    else:
        status = "upcoming"
    return status, (pending[0] if pending else None), todays


def last_taken(s: ScheduledDose, doses: list[DoseEvent], now: datetime):
    times = [
        datetime.fromisoformat(ev.taken_at)
        for ev in doses
        if ev.product_id == s.product_id and datetime.fromisoformat(ev.taken_at) <= now
    ]
    return max(times) if times else None


# --- Persistence -----------------------------------------------------------------


def snapshot(ingredients: list[DecomposedIngredient], registry: Registry) -> list[dict]:
    """substance_entries: one per label ingredient, unmodeled ones included."""
    return [
        {
            "name": ing.name,
            "substance_id": ing.substance_id,
            "amount_mg": ing.strength_mg,
            "display_name": (
                registry.get(ing.substance_id)["name"] if ing.substance_id else None
            ),
            "unmodeled_reason": ing.unmodeled_reason,
        }
        for ing in ingredients
    ]


def ingredients_of(s: ScheduledDose) -> list[DecomposedIngredient]:
    return [
        DecomposedIngredient(
            e["name"], e["substance_id"], e["amount_mg"], e["unmodeled_reason"]
        )
        for e in s.substance_entries
    ]


def create(
    session: Session,
    user_id: str,
    *,
    product_id: str,
    product_name: str,
    entries: list[dict],
    topical_ingredients: list[str] | None,
    frequency: str,
    times: list[str],
    days_of_week: list[str] | None,
    notes: str | None,
    timezone: str | None,
    now: datetime,
) -> ScheduledDose:
    times, days = validate(frequency, times, days_of_week)
    timezone = resolve_timezone(timezone)
    if frequency == "weekly" and days is None:
        # "weekly" with no day named means the day it was set up.
        tz = ZoneInfo(timezone) if timezone else (now.tzinfo or ZoneInfo("UTC"))
        days = [WEEKDAYS[now.astimezone(tz).weekday()]]
    record = ScheduledDose(
        id=f"sched_{uuid.uuid4().hex[:12]}",
        user_id=user_id,
        product_id=product_id,
        product_name=product_name,
        substance_entries=entries,
        frequency=frequency,
        time_of_day=times,
        days_of_week=days,
        notes=notes,
        active=True,
        created_at=now.isoformat(),
        timezone=timezone,
        topical_ingredients=topical_ingredients,
    )
    session.add(record)
    session.flush()
    return record


def update(
    s: ScheduledDose,
    *,
    frequency: str | None,
    times: list[str] | None,
    days_of_week: list[str] | None,
    days_given: bool,
    notes: str | None,
    notes_given: bool,
) -> None:
    """Apply a partial update. Changing frequency needs its times too, and
    drops the old days unless new ones are given."""
    new_frequency = frequency or s.frequency
    if frequency and frequency != s.frequency and times is None:
        raise InvalidSchedule("times: changing frequency needs the new times")
    new_times = times if times is not None else list(s.time_of_day)
    if days_given:
        new_days = days_of_week
    elif frequency and frequency != s.frequency:
        new_days = None
    else:
        new_days = s.days_of_week
    if new_frequency == "weekly" and new_days is None:
        raise InvalidSchedule("days_of_week: weekly needs at least one day")
    s.time_of_day, s.days_of_week = validate(new_frequency, new_times, new_days)
    s.frequency = new_frequency
    if notes_given:
        s.notes = notes


def deactivate(s: ScheduledDose, now: datetime) -> None:
    if s.active:
        s.active = False
        s.deactivated_at = now.isoformat()


def get(session: Session, user_id: str, schedule_id: str) -> ScheduledDose | None:
    s = session.get(ScheduledDose, schedule_id)
    return s if s is not None and s.user_id == user_id else None


def list_for_user(
    session: Session, user_id: str, *, active_only: bool = True
) -> list[ScheduledDose]:
    q = select(ScheduledDose).where(ScheduledDose.user_id == user_id)
    if active_only:
        q = q.where(ScheduledDose.active.is_(True))
    return list(session.scalars(q.order_by(ScheduledDose.created_at)))


# --- Presentation ----------------------------------------------------------------


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t else None


def _current_concentration(
    events: list[DoseEvent], substance_id: str, registry: Registry, now: datetime
) -> float | None:
    """mg/L from every logged dose of the substance (scheduled product or not).

    None for anything the engine can't model; never an estimate of our own.
    """
    result = service.compute_curve(
        events, substance_id, registry, now, now + timedelta(minutes=15), "15m", now
    )
    if not result.modelable or result.current is None:
        return None
    return result.current["concentration"]


def schedule_json(
    s: ScheduledDose, events: list[DoseEvent], registry: Registry, now: datetime
) -> dict:
    status, nxt, todays = summarize(s, events, now)
    substances = []
    for e in s.substance_entries:
        modeled = e["substance_id"] is not None and e["unmodeled_reason"] is None
        substances.append(
            {
                "substance_id": e["substance_id"],
                "display_name": e["display_name"],
                "name": e["name"],
                "amount_mg": e["amount_mg"],
                "unmodeled_reason": e["unmodeled_reason"],
                "current_concentration": (
                    _current_concentration(events, e["substance_id"], registry, now)
                    if modeled
                    else None
                ),
                "unit": "mg/L",
            }
        )
    return {
        "id": s.id,
        "product_id": s.product_id,
        "product_name": s.product_name,
        "frequency": s.frequency,
        "times": list(s.time_of_day),
        "days_of_week": s.days_of_week,
        "timezone": s.timezone,
        "notes": s.notes,
        "active": s.active,
        "created_at": s.created_at,
        "status": status,
        "next_due": _iso(nxt.at) if nxt else None,
        "last_taken": _iso(last_taken(s, events, now)),
        "today": [
            {
                "scheduled_at": sl.at.isoformat(),
                "status": sl.status,
                "taken_at": _iso(sl.taken_at),
                "dose_event_id": sl.dose_event_id,
            }
            for sl in todays
        ],
        "substances": substances,
    }


def projected_doses(
    session: Session,
    user_id: str,
    events: list[DoseEvent],
    substance_id: str,
    now: datetime,
    until: datetime,
) -> list[tuple[datetime, float]]:
    """(instant, mg) for every *future* slot of an active schedule.

    Only slots after `now` and not already fulfilled: a past slot nobody logged
    is not assumed taken, and a fulfilled one is already in the real curve.
    """
    out = []
    for s in list_for_user(session, user_id):
        mg = sum(
            e["amount_mg"]
            for e in s.substance_entries
            if e["substance_id"] == substance_id and e["unmodeled_reason"] is None
        )
        if mg <= 0:
            continue
        for slot in evaluate(s, events, now, now, until):
            if slot.at > now and slot.status != "taken":
                out.append((slot.at, mg))
    return sorted(out)


def timeline_entries(
    session: Session,
    user_id: str,
    events: list[DoseEvent],
    now: datetime,
    range_from: datetime,
    range_to: datetime,
) -> list[dict]:
    """Closed slots in the range, as "taken" or "missed". Slots still open
    (upcoming, due, overdue) aren't outcomes yet and are left out. Deactivated
    schedules are included: the history is kept."""
    out = []
    for s in list_for_user(session, user_id, active_only=False):
        for slot in evaluate(s, events, now, range_from, min(range_to, now)):
            if slot.status not in ("taken", "missed"):
                continue
            out.append(
                {
                    "type": "scheduled",
                    "timestamp": slot.at.isoformat(),
                    "id": f"{s.id}@{slot.at.isoformat()}",
                    "schedule_id": s.id,
                    "product_id": s.product_id,
                    "product_name": s.product_name,
                    "status": slot.status,
                    "dose_event_id": slot.dose_event_id,
                }
            )
    return out
