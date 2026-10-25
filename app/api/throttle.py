from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

MAX_TRACKED_KEYS = 10_000


@dataclass
class _Entry:
    failures: int = 0
    last_failure_at: float = 0.0
    locked_until: float = 0.0
    level: int = 0


class Limiter:
    def __init__(
        self,
        threshold: int,
        window: float,
        base_lockout: float,
        max_lockout: float,
        clock: Callable[[], float],
    ) -> None:
        self.threshold = threshold
        self.window = window
        self.base_lockout = base_lockout
        self.max_lockout = max_lockout
        self.clock = clock
        self.entries: dict[str, _Entry] = {}

    def retry_after(self, key: str) -> int:
        entry = self.entries.get(key)
        if entry is None:
            return 0
        remaining = entry.locked_until - self.clock()
        return math.ceil(remaining) if remaining > 0 else 0

    def record_failure(self, key: str) -> None:
        now = self.clock()
        self._prune(now)
        entry = self.entries.setdefault(key, _Entry())
        idle = now - entry.last_failure_at > self.window
        if idle and now >= entry.locked_until:
            entry.failures = 0
            entry.level = 0
        entry.failures += 1
        entry.last_failure_at = now
        if entry.failures >= self.threshold and now >= entry.locked_until:
            entry.level += 1
            entry.failures = 0
            lockout = min(self.base_lockout * 2 ** (entry.level - 1), self.max_lockout)
            entry.locked_until = now + lockout

    def record_success(self, key: str) -> None:
        self.entries.pop(key, None)

    def _prune(self, now: float) -> None:
        if len(self.entries) < MAX_TRACKED_KEYS:
            return
        stale = [
            key
            for key, entry in self.entries.items()
            if now >= entry.locked_until and now - entry.last_failure_at > self.window
        ]
        for key in stale:
            del self.entries[key]


@dataclass
class Throttles:
    clock: Callable[[], float] = time.monotonic
    login_pair: Limiter = field(init=False)
    login_ip: Limiter = field(init=False)
    signup_pair: Limiter = field(init=False)
    signup_ip: Limiter = field(init=False)

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.login_pair = Limiter(5, 900, 30, 900, self._now)
        self.login_ip = Limiter(20, 900, 60, 900, self._now)
        self.signup_pair = Limiter(5, 3600, 60, 3600, self._now)
        self.signup_ip = Limiter(10, 3600, 120, 3600, self._now)

    def _now(self) -> float:
        return self.clock()

    def login_retry_after(self, ip: str, pair: str) -> int:
        return max(self.login_pair.retry_after(pair), self.login_ip.retry_after(ip))

    def login_failed(self, ip: str, pair: str) -> None:
        self.login_pair.record_failure(pair)
        self.login_ip.record_failure(ip)

    def login_succeeded(self, pair: str) -> None:
        self.login_pair.record_success(pair)

    def signup_retry_after(self, ip: str, pair: str) -> int:
        return max(self.signup_pair.retry_after(pair), self.signup_ip.retry_after(ip))

    def signup_attempted(self, ip: str) -> None:
        self.signup_ip.record_failure(ip)

    def signup_failed(self, pair: str) -> None:
        self.signup_pair.record_failure(pair)


throttles = Throttles()
