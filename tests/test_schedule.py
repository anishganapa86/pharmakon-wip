"""Medication schedule: statuses, slots, take-now, history, and projection.

Dates are pinned through the get_now dependency. 2026-09-14 is a Monday. Each
test gets a fresh in-memory SQLite database. A schedule is created at 07:00
unless a test says otherwise, so that day's 08:00 slot exists.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import main, service
from app.api.db import Base, DoseEvent, DoseIngredient
from app.registry.loader import load_registry

NODOZ = "prod_nodoz_200"  # caffeine 200 mg
TYLENOL = "prod_tylenol_es_500"  # acetaminophen 500 mg


def at(day: int, hh_mm: str) -> datetime:
    """Sept `day` 2026 (Sept 14 is a Monday), local -07:00."""
    return datetime.fromisoformat(f"2026-09-{day:02d}T{hh_mm}:00-07:00")


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock():
    return Clock(at(14, "07:00"))


@pytest.fixture
def client(clock):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    def session_override():
        with SessionLocal() as s:
            yield s

    main.app.dependency_overrides[main.get_session] = session_override
    main.app.dependency_overrides[main.get_now] = clock
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def add(client, frequency="daily", times=("08:00",), product=NODOZ, **extra):
    r = client.post(
        "/users/default/schedule",
        json={
            "product_id": product,
            "frequency": frequency,
            "times": list(times),
            **extra,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def get_one(client, schedule_id):
    rows = client.get("/users/default/schedule").json()
    return next(r for r in rows if r["id"] == schedule_id)


def take(client, schedule_id):
    r = client.post(f"/users/default/schedule/{schedule_id}/take")
    assert r.status_code == 201, r.text
    return r.json()


def slot_statuses(row):
    return [s["status"] for s in row["today"]]


# --- Create and read -----------------------------------------------------------


def test_create_then_retrieve_has_the_right_next_due(client, clock):
    clock.now = at(13, "18:00")  # Sunday evening
    created = add(client)
    # Today's 08:00 is before the schedule existed: not a missed dose.
    assert created["status"] == "upcoming"
    assert created["next_due"] == at(14, "08:00").isoformat()
    assert created["last_taken"] is None
    assert get_one(client, created["id"])["next_due"] == created["next_due"]


def test_next_due_is_todays_slot_when_created_before_it(client):
    row = add(client)
    assert row["next_due"] == at(14, "08:00").isoformat()
    assert row["status"] == "upcoming"


def test_substances_carry_the_product_snapshot(client):
    row = add(client, product="prod_excedrin_es", frequency="daily")
    by_id = {s["substance_id"]: s for s in row["substances"]}
    assert set(by_id) == {"acetaminophen", "aspirin", "caffeine"}
    assert by_id["caffeine"]["amount_mg"] == 65
    assert by_id["caffeine"]["display_name"] == "Caffeine"
    assert by_id["caffeine"]["current_concentration"] == 0.0


def test_unknown_product_is_404(client):
    r = client.post(
        "/users/default/schedule",
        json={"product_id": "nope", "frequency": "daily", "times": ["08:00"]},
    )
    assert r.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"frequency": "hourly", "times": ["08:00"]},
        {"frequency": "daily", "times": ["08:00", "20:00"]},
        {"frequency": "twice_daily", "times": ["08:00"]},
        {"frequency": "daily", "times": ["8am"]},
        {"frequency": "daily", "times": ["25:00"]},
        {"frequency": "twice_daily", "times": ["08:00", "08:00"]},
        {"frequency": "as_needed", "times": ["08:00"]},
        {"frequency": "daily", "times": ["08:00"], "days_of_week": ["someday"]},
        {"frequency": "daily", "times": ["08:00"], "days_of_week": []},
        {
            "frequency": "every_other_day",
            "times": ["08:00"],
            "days_of_week": ["monday"],
        },
        {"frequency": "daily", "times": ["08:00"], "timezone": "Mars/Olympus"},
    ],
)
def test_malformed_schedules_are_rejected(client, body):
    r = client.post("/users/default/schedule", json={"product_id": NODOZ, **body})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "validation_error"


def test_as_needed_has_no_next_due_and_its_own_status(client):
    row = add(client, frequency="as_needed", times=())
    assert row["status"] == "as_needed"
    assert row["next_due"] is None


def test_weekly_defaults_to_the_day_it_was_created(client):
    row = add(client, frequency="weekly")
    assert row["days_of_week"] == ["monday"]


# --- Status --------------------------------------------------------------------


def test_status_walks_upcoming_due_overdue(client, clock):
    sid = add(client)["id"]
    clock.now = at(14, "07:15")
    assert get_one(client, sid)["status"] == "upcoming"
    clock.now = at(14, "07:45")  # inside the 30 minutes before
    assert get_one(client, sid)["status"] == "due"
    clock.now = at(14, "08:50")  # inside the hour after
    assert get_one(client, sid)["status"] == "due"
    clock.now = at(14, "09:30")
    assert get_one(client, sid)["status"] == "overdue"


def test_scheduled_for_8am_at_2pm_is_overdue(client, clock):
    row = add(client)
    clock.now = at(14, "14:00")
    row = get_one(client, row["id"])
    assert row["status"] == "overdue"
    assert row["next_due"] == at(14, "08:00").isoformat()  # what was missed


def test_taking_a_dose_makes_it_taken_today(client, clock):
    sid = add(client)["id"]
    clock.now = at(14, "08:05")
    take(client, sid)
    row = get_one(client, sid)
    assert row["status"] == "taken_today"
    assert row["last_taken"] == at(14, "08:05").isoformat()
    assert row["next_due"] == at(15, "08:00").isoformat()


def test_a_manually_logged_dose_also_satisfies_the_slot(client, clock):
    sid = add(client)["id"]
    clock.now = at(14, "08:10")
    r = client.post(
        "/users/default/doses",
        json={
            "product_id": NODOZ,
            "taken_at": at(14, "08:00").isoformat(),
            "quantity": 1,
        },
    )
    assert r.status_code == 201
    assert get_one(client, sid)["status"] == "taken_today"


def test_a_dose_of_a_different_product_does_not_count(client, clock):
    sid = add(client)["id"]
    clock.now = at(14, "14:00")
    client.post(
        "/users/default/doses",
        json={"product_id": TYLENOL, "taken_at": clock.now.isoformat(), "quantity": 1},
    )
    row = get_one(client, sid)
    assert row["status"] == "overdue"
    assert row["last_taken"] is None


def test_twice_daily_slots_are_tracked_independently(client, clock):
    sid = add(client, "twice_daily", ("08:00", "20:00"))["id"]
    clock.now = at(14, "08:10")
    take(client, sid)

    clock.now = at(14, "14:00")
    row = get_one(client, sid)
    assert slot_statuses(row) == ["taken", "upcoming"]
    assert row["status"] == "upcoming"
    assert row["next_due"] == at(14, "20:00").isoformat()

    clock.now = at(14, "21:30")  # evening dose is now late; morning is settled
    row = get_one(client, sid)
    assert slot_statuses(row) == ["taken", "overdue"]
    assert row["status"] == "overdue"

    take(client, sid)
    row = get_one(client, sid)
    assert slot_statuses(row) == ["taken", "taken"]
    assert row["status"] == "taken_today"


def test_one_dose_never_satisfies_two_slots(client, clock):
    sid = add(client, "twice_daily", ("08:00", "20:00"))["id"]
    clock.now = at(14, "19:00")
    take(client, sid)  # 19:00 is inside the evening window, not the morning
    clock.now = at(14, "19:30")
    assert slot_statuses(get_one(client, sid)) == ["missed", "taken"]


def test_a_lapsed_slot_is_missed_and_does_not_keep_the_schedule_red(client, clock):
    sid = add(client, "twice_daily", ("08:00", "20:00"))["id"]
    clock.now = at(14, "19:40")  # morning window closed at 18:00, untaken
    row = get_one(client, sid)
    assert slot_statuses(row) == ["missed", "due"]
    take(client, sid)
    assert get_one(client, sid)["status"] == "taken_today"


def test_three_times_daily_has_three_slots(client):
    row = add(client, "three_times_daily", ("08:00", "14:00", "20:00"))
    assert len(row["today"]) == 3


def test_every_other_day_skips_the_off_days(client, clock):
    sid = add(client, "every_other_day")["id"]  # Monday the 14th is a dose day
    clock.now = at(14, "08:05")
    take(client, sid)

    clock.now = at(15, "12:00")  # Tuesday: nothing due, and nothing missed
    row = get_one(client, sid)
    assert row["today"] == []
    assert row["status"] == "upcoming"
    assert row["next_due"] == at(16, "08:00").isoformat()

    clock.now = at(16, "09:30")  # Wednesday: due again, and late
    assert get_one(client, sid)["status"] == "overdue"


def test_weekly_on_chosen_days(client, clock):
    sid = add(client, "weekly", days_of_week=["wednesday", "monday"])["id"]
    assert get_one(client, sid)["days_of_week"] == ["monday", "wednesday"]
    clock.now = at(14, "08:05")
    take(client, sid)
    clock.now = at(14, "12:00")
    assert get_one(client, sid)["next_due"] == at(16, "08:00").isoformat()  # Wed


def test_daily_can_be_limited_to_some_days(client):
    row = add(client, days_of_week=["tuesday"])  # created Monday 07:00
    assert row["today"] == []
    assert row["next_due"] == at(15, "08:00").isoformat()


def test_times_are_read_in_the_schedules_own_timezone(client, clock):
    clock.now = datetime.fromisoformat("2026-09-14T14:00:00+00:00")  # 07:00 in LA
    row = add(client, timezone="America/Los_Angeles")
    assert row["next_due"] == at(14, "08:00").isoformat()  # 08:00 -07:00 = 15:00Z


# --- Take now ------------------------------------------------------------------


def test_take_creates_a_real_dose_event(client, clock):
    sid = add(client, product="prod_excedrin_es")["id"]
    clock.now = at(14, "08:05")
    result = take(client, sid)
    assert result["schedule_id"] == sid

    doses = client.get("/users/default/doses").json()
    assert [d["dose_event_id"] for d in doses] == [result["dose_event_id"]]
    dose = doses[0]
    assert dose["product_id"] == "prod_excedrin_es"
    assert dose["quantity"] == 1
    assert dose["taken_at"] == at(14, "08:05").isoformat()
    amounts = {i["substance_id"]: i["amount_mg"] for i in dose["ingredients"]}
    assert amounts == {"acetaminophen": 250, "aspirin": 250, "caffeine": 65}


def test_take_goes_through_alerting_like_a_manual_dose(client, clock):
    sid = add(client, product=TYLENOL)["id"]
    body = {"product_id": TYLENOL, "quantity": 5}  # 2500 mg, under 75% of 4000
    r = client.post(
        "/users/default/doses", json={**body, "taken_at": at(14, "07:00").isoformat()}
    )
    assert r.json()["triggered_alerts"] == []
    clock.now = at(14, "08:00")
    result = take(client, sid)  # +500 mg reaches 3000 mg
    assert [a["kind"] for a in result["triggered_alerts"]] == ["threshold"]
    assert client.get("/users/default/alerts").json()[0]["contributing_doses"]


def test_take_keeps_unmodeled_ingredients(client, clock):
    sid = add(client, product="prod_example_cold_multi")["id"]  # unknown strength
    clock.now = at(14, "08:00")
    take(client, sid)
    dose = client.get("/users/default/doses").json()[0]
    reasons = {i["name"]: i["unmodeled_reason"] for i in dose["ingredients"]}
    assert reasons["ACETAMINOPHEN"] == "unknown_strength"


def test_take_on_an_unknown_or_inactive_schedule_is_404(client):
    assert client.post("/users/default/schedule/nope/take").status_code == 404
    sid = add(client)["id"]
    client.delete(f"/users/default/schedule/{sid}")
    assert client.post(f"/users/default/schedule/{sid}/take").status_code == 404


# --- Update and deactivate ------------------------------------------------------


def test_deactivated_schedule_is_not_in_the_active_list(client):
    keep = add(client)["id"]
    drop = add(client, product=TYLENOL)["id"]
    assert client.delete(f"/users/default/schedule/{drop}").status_code == 204
    assert [r["id"] for r in client.get("/users/default/schedule").json()] == [keep]


def test_put_active_false_also_deactivates(client):
    sid = add(client)["id"]
    r = client.put(f"/users/default/schedule/{sid}", json={"active": False})
    assert r.status_code == 200
    assert r.json()["active"] is False
    assert client.get("/users/default/schedule").json() == []


def test_put_changes_times_and_notes(client):
    sid = add(client)["id"]
    r = client.put(
        f"/users/default/schedule/{sid}", json={"times": ["09:30"], "notes": "food"}
    )
    assert r.status_code == 200
    assert r.json()["times"] == ["09:30"]
    assert r.json()["notes"] == "food"
    assert r.json()["next_due"] == at(14, "09:30").isoformat()


def test_put_changing_frequency_needs_matching_times(client):
    sid = add(client)["id"]
    bad = client.put(
        f"/users/default/schedule/{sid}", json={"frequency": "twice_daily"}
    )
    assert bad.status_code == 422
    ok = client.put(
        f"/users/default/schedule/{sid}",
        json={"frequency": "twice_daily", "times": ["08:00", "20:00"]},
    )
    assert ok.status_code == 200
    assert len(ok.json()["today"]) == 2
    # The rejected edit left nothing behind.
    assert get_one(client, sid)["frequency"] == "twice_daily"


def test_put_and_delete_unknown_schedule_are_404(client):
    assert client.put("/users/default/schedule/nope", json={}).status_code == 404
    assert client.delete("/users/default/schedule/nope").status_code == 404


# --- Concentration ---------------------------------------------------------------


def curve(client, clock, hours_ahead=30, substance="caffeine"):
    r = client.get(
        "/users/default/curve",
        params={
            "substance_id": substance,
            "from": (clock.now - timedelta(hours=1)).isoformat(),
            "to": (clock.now + timedelta(hours=hours_ahead)).isoformat(),
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def caffeine_event(dose_id, taken_at):
    ev = DoseEvent(
        id=dose_id,
        user_id="default",
        product_id=NODOZ,
        product_name="NoDoz",
        taken_at=taken_at.isoformat(),
        quantity=1,
    )
    ev.ingredients.append(
        DoseIngredient(
            ingredient_name="CAFFEINE", substance_id="caffeine", amount_mg=200
        )
    )
    return ev


def engine_concentration(events, now):
    """What the engine says for these doses alone, bypassing the API."""
    result = service.compute_curve(
        events,
        "caffeine",
        load_registry(),
        now,
        now + timedelta(hours=1),
        "15m",
        now,
    )
    return result.current["concentration"]


def test_schedule_and_manual_dose_both_contribute_to_concentration(client, clock):
    sid = add(client)["id"]
    client.post(  # a manual dose of the same substance, not from the schedule
        "/users/default/doses",
        json={
            "product_id": NODOZ,
            "taken_at": at(14, "07:00").isoformat(),
            "quantity": 1,
        },
    )
    clock.now = at(14, "08:00")
    take(client, sid)
    clock.now = at(14, "09:00")

    both = engine_concentration(
        [caffeine_event("a", at(14, "07:00")), caffeine_event("b", at(14, "08:00"))],
        clock.now,
    )
    scheduled_only = engine_concentration(
        [caffeine_event("b", at(14, "08:00"))], clock.now
    )

    shown = get_one(client, sid)["substances"][0]["current_concentration"]
    assert shown == pytest.approx(both)
    assert shown > scheduled_only

    from_curve = curve(client, clock, hours_ahead=1)["current"]["concentration"]
    assert from_curve == pytest.approx(both)


def test_curve_projects_future_scheduled_doses_apart_from_the_real_curve(client, clock):
    sid = add(client, "twice_daily", ("08:00", "20:00"))["id"]
    clock.now = at(14, "08:00")
    take(client, sid)
    clock.now = at(14, "19:00")

    body = curve(client, clock, hours_ahead=4)
    projected = {p["t"]: p["concentration"] for p in body["projected_points"]}
    actual = {p["t"]: p["concentration"] for p in body["points"]}

    assert min(projected) == clock.now.isoformat()  # forward-looking only
    assert projected[clock.now.isoformat()] == pytest.approx(
        actual[clock.now.isoformat()]
    )
    later = at(14, "22:00").isoformat()
    assert projected[later] > actual[later]  # the 20:00 dose is added
    assert body["peak"]["concentration"] == pytest.approx(  # real peak unchanged
        max(p["concentration"] for p in body["points"])
    )


def test_a_dose_not_taken_is_never_assumed_taken(client, clock):
    add(client)  # daily 08:00, never taken
    clock.now = at(14, "14:00")
    body = curve(client, clock, hours_ahead=2)
    assert all(p["concentration"] == 0 for p in body["points"])
    assert body["current"]["concentration"] == 0
    # The missed 08:00 dose is in the past, so it is not projected either.
    assert body["projected_points"] is None


def test_curve_without_a_schedule_has_no_projection(client, clock):
    body = curve(client, clock)
    assert body["projected_points"] is None


def test_scheduled_but_unlogged_doses_raise_no_alerts(client, clock):
    add(
        client,
        product=TYLENOL,
        frequency="three_times_daily",
        times=("08:00", "14:00", "20:00"),
    )
    clock.now = at(14, "21:00")
    assert client.get("/users/default/alerts").json() == []


# --- Timeline ----------------------------------------------------------------------


def scheduled_entries(client, clock, days=5):
    r = client.get(
        "/users/default/timeline",
        params={
            "from": (clock.now - timedelta(days=days)).isoformat(),
            "to": clock.now.isoformat(),
        },
    )
    return [e for e in r.json() if e["type"] == "scheduled"]


def test_timeline_shows_scheduled_slots_as_taken_or_missed(client, clock):
    sid = add(client)["id"]
    clock.now = at(14, "08:05")
    take(client, sid)
    clock.now = at(16, "12:00")  # the 15th was never taken
    entries = scheduled_entries(client, clock)
    assert [(e["timestamp"], e["status"]) for e in entries] == [
        (at(14, "08:00").isoformat(), "taken"),
        (at(15, "08:00").isoformat(), "missed"),
        # 16th 08:00 is overdue, not settled: left out.
    ]
    assert entries[0]["dose_event_id"] is not None
    assert entries[1]["dose_event_id"] is None


def test_deactivating_keeps_history_and_stops_future_misses(client, clock):
    sid = add(client)["id"]
    clock.now = at(15, "12:00")
    client.delete(f"/users/default/schedule/{sid}")
    clock.now = at(18, "12:00")
    entries = scheduled_entries(client, clock, days=6)
    assert [(e["timestamp"], e["status"]) for e in entries] == [
        (at(14, "08:00").isoformat(), "missed"),
        (at(15, "08:00").isoformat(), "missed"),
    ]


def test_slots_before_the_schedule_existed_are_not_missed(client, clock):
    clock.now = at(14, "15:00")
    add(client)
    assert scheduled_entries(client, clock) == []
