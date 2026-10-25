"""Shared test fixtures.

openFDA is never actually reached in tests: every test gets a stub client
that reports "no matches" for app.api.openfda, so existing tests that don't
care about openFDA behave exactly as if it were unreachable-but-degraded
(catalog-only results). Tests that care about openFDA behavior install their
own httpx.MockTransport on top of this (see tests/test_openfda_search.py).
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import httpx
import pytest

from app.api import label_cache, main, openfda, throttle


def _no_matches(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        404, json={"error": {"code": "NOT_FOUND", "message": "No matches"}}
    )


@pytest.fixture(autouse=True)
def _stub_openfda(monkeypatch):
    monkeypatch.setattr(
        openfda, "_http", httpx.Client(transport=httpx.MockTransport(_no_matches))
    )
    openfda.clear_discovered()
    yield
    openfda.clear_discovered()


@pytest.fixture(autouse=True)
def _no_label_disk_cache():
    label_cache.set_cache_dir(None)
    yield
    label_cache.reset_cache_dir()


@pytest.fixture(autouse=True)
def _show_unverified_rules(monkeypatch):
    monkeypatch.setenv("SHOW_UNVERIFIED_RULES", "1")


@pytest.fixture(autouse=True)
def _fresh_throttles():
    throttle.throttles.clock = time.monotonic
    throttle.throttles.reset()
    yield
    throttle.throttles.clock = time.monotonic
    throttle.throttles.reset()


@pytest.fixture(autouse=True)
def _signed_in_test_user(request):
    if request.node.get_closest_marker("real_auth"):
        yield
        return
    main.app.dependency_overrides[main.current_user_or_none] = lambda: SimpleNamespace(
        id="default", email="default@example.com"
    )
    yield
    main.app.dependency_overrides.pop(main.current_user_or_none, None)
