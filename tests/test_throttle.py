import secrets

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import main
from app.api.db import Base
from app.api.throttle import Limiter, Throttles, throttles

pytestmark = pytest.mark.real_auth

PASSWORD = secrets.token_urlsafe(16)
LEAKY = secrets.token_urlsafe(16)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_limiter(clock, threshold=3, window=100, base=10, cap=80):
    return Limiter(threshold, window, base, cap, clock)


def test_limiter_allows_until_threshold():
    clock = FakeClock()
    limiter = make_limiter(clock)
    limiter.record_failure("k")
    limiter.record_failure("k")
    assert limiter.retry_after("k") == 0
    limiter.record_failure("k")
    assert limiter.retry_after("k") == 10


def test_limiter_lock_expires():
    clock = FakeClock()
    limiter = make_limiter(clock)
    for _ in range(3):
        limiter.record_failure("k")
    clock.t += 10
    assert limiter.retry_after("k") == 0


def test_limiter_backoff_doubles_and_caps():
    clock = FakeClock()
    limiter = make_limiter(clock)
    waits = []
    for _ in range(6):
        for _ in range(3):
            limiter.record_failure("k")
        waits.append(limiter.retry_after("k"))
        clock.t += waits[-1]
    assert waits == [10, 20, 40, 80, 80, 80]


def test_limiter_retry_after_counts_down():
    clock = FakeClock()
    limiter = make_limiter(clock)
    for _ in range(3):
        limiter.record_failure("k")
    clock.t += 4.5
    assert limiter.retry_after("k") == 6


def test_limiter_success_clears_key():
    clock = FakeClock()
    limiter = make_limiter(clock)
    limiter.record_failure("k")
    limiter.record_failure("k")
    limiter.record_success("k")
    limiter.record_failure("k")
    limiter.record_failure("k")
    assert limiter.retry_after("k") == 0


def test_limiter_idle_window_forgets_failures():
    clock = FakeClock()
    limiter = make_limiter(clock)
    limiter.record_failure("k")
    limiter.record_failure("k")
    clock.t += 101
    limiter.record_failure("k")
    limiter.record_failure("k")
    assert limiter.retry_after("k") == 0


def test_limiter_keys_are_independent():
    clock = FakeClock()
    limiter = make_limiter(clock)
    for _ in range(3):
        limiter.record_failure("a")
    assert limiter.retry_after("a") > 0
    assert limiter.retry_after("b") == 0


def test_limiter_prunes_stale_entries(monkeypatch):
    from app.api import throttle

    monkeypatch.setattr(throttle, "MAX_TRACKED_KEYS", 5)
    clock = FakeClock()
    limiter = make_limiter(clock)
    for i in range(5):
        limiter.record_failure(f"k{i}")
    clock.t += 1000
    limiter.record_failure("fresh")
    assert set(limiter.entries) == {"fresh"}


def test_throttles_reset_gives_clean_state():
    state = Throttles()
    for _ in range(5):
        state.login_failed("1.1.1.1", "1.1.1.1|a@example.com")
    assert state.login_retry_after("1.1.1.1", "1.1.1.1|a@example.com") > 0
    state.reset()
    assert state.login_retry_after("1.1.1.1", "1.1.1.1|a@example.com") == 0


@pytest.fixture
def clock():
    fake = FakeClock()
    throttles.clock = fake
    return fake


@pytest.fixture
def maker():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def make_client(maker):
    def session_override():
        with maker() as s:
            yield s

    main.app.dependency_overrides[main.get_session] = session_override

    def build(ip="10.0.0.1"):
        return TestClient(main.app, client=(ip, 50000))

    yield build
    main.app.dependency_overrides.clear()


def attempt_login(client, email="person@example.com", password="wrong password"):
    return client.post("/auth/login", json={"email": email, "password": password})


def attempt_signup(client, email, password=PASSWORD):
    return client.post("/auth/signup", json={"email": email, "password": password})


def test_login_locks_after_repeated_failures(make_client, clock):
    client = make_client()
    attempt_signup(make_client("10.0.0.9"), "person@example.com")
    for _ in range(5):
        assert attempt_login(client).status_code == 401
    r = attempt_login(client)
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"
    assert int(r.headers["retry-after"]) == 30


def test_locked_login_rejects_even_the_correct_password(make_client, clock):
    client = make_client()
    attempt_signup(make_client("10.0.0.9"), "person@example.com")
    for _ in range(5):
        attempt_login(client)
    r = attempt_login(client, password=PASSWORD)
    assert r.status_code == 429
    assert "set-cookie" not in r.headers


def test_login_lock_lifts_after_retry_after(make_client, clock):
    client = make_client()
    attempt_signup(make_client("10.0.0.9"), "person@example.com")
    for _ in range(5):
        attempt_login(client)
    clock.t += 31
    assert attempt_login(client, password=PASSWORD).status_code == 200


def test_login_backoff_grows_on_repeat_offence(make_client, clock):
    client = make_client()
    for _ in range(5):
        attempt_login(client, email="x@example.com")
    clock.t += 31
    for _ in range(5):
        attempt_login(client, email="x@example.com")
    r = attempt_login(client, email="x@example.com")
    assert int(r.headers["retry-after"]) == 60


def test_one_email_lock_does_not_block_other_emails_from_same_ip(make_client, clock):
    client = make_client()
    attempt_signup(make_client("10.0.0.9"), "other@example.com")
    for _ in range(5):
        attempt_login(client, email="victim@example.com")
    assert attempt_login(client, email="victim@example.com").status_code == 429
    assert (
        attempt_login(client, email="other@example.com", password=PASSWORD).status_code
        == 200
    )


def test_lock_on_one_ip_does_not_block_the_account_owner_elsewhere(make_client, clock):
    attacker = make_client("10.0.0.66")
    owner = make_client("10.0.0.2")
    attempt_signup(owner, "person@example.com")
    for _ in range(5):
        attempt_login(attacker)
    assert attempt_login(attacker).status_code == 429
    assert attempt_login(owner, password=PASSWORD).status_code == 200


def test_ip_wide_lock_after_many_failures_across_emails(make_client, clock):
    client = make_client()
    for i in range(20):
        assert attempt_login(client, email=f"user{i}@example.com").status_code == 401
    r = attempt_login(client, email="fresh@example.com")
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) == 60


def test_successful_login_clears_the_pair_counter(make_client, clock):
    client = make_client()
    attempt_signup(make_client("10.0.0.9"), "person@example.com")
    for _ in range(4):
        attempt_login(client)
    assert attempt_login(client, password=PASSWORD).status_code == 200
    for _ in range(4):
        assert attempt_login(client).status_code == 401


def test_email_case_does_not_dodge_the_limit(make_client, clock):
    client = make_client()
    variants = ["a@example.com", "A@example.com", " a@EXAMPLE.com ", "a@Example.com"]
    for i in range(5):
        attempt_login(client, email=variants[i % 4])
    assert attempt_login(client, email="A@EXAMPLE.COM").status_code == 429


def test_signup_attempts_are_limited_per_ip(make_client, clock):
    client = make_client()
    for i in range(10):
        assert attempt_signup(client, f"user{i}@example.com").status_code == 201
    r = attempt_signup(client, "one-more@example.com")
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) == 120


def test_signup_limit_is_per_ip(make_client, clock):
    for i in range(10):
        attempt_signup(make_client(), f"user{i}@example.com")
    assert attempt_signup(make_client("10.0.0.77"), "ok@example.com").status_code == 201


def test_repeated_failed_signup_for_one_email_is_limited(make_client, clock):
    client = make_client()
    attempt_signup(make_client("10.0.0.9"), "taken@example.com")
    for _ in range(5):
        assert attempt_signup(client, "taken@example.com").status_code == 409
    assert attempt_signup(client, "taken@example.com").status_code == 429


def test_signup_limit_lifts_over_time(make_client, clock):
    client = make_client()
    for i in range(10):
        attempt_signup(client, f"user{i}@example.com")
    assert attempt_signup(client, "late@example.com").status_code == 429
    clock.t += 121
    assert attempt_signup(client, "late@example.com").status_code == 201


def test_429_does_not_log_credentials(make_client, clock, caplog):
    client = make_client()
    with caplog.at_level("DEBUG"):
        for _ in range(7):
            attempt_login(client, email="leak@example.com", password=LEAKY)
    assert LEAKY not in caplog.text
    assert "leak@example.com" not in caplog.text
