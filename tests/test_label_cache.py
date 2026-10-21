from __future__ import annotations

import threading
import time
import types
from pathlib import Path

import httpx
import pytest

from app.api import food_guidance, label_cache

REPO = Path(__file__).resolve().parent.parent


def oral_directions_label() -> dict:
    return {
        "openfda": {"route": ["ORAL"]},
        "directions": ["Take with food to reduce stomach upset."],
    }


def empty_curated(monkeypatch, tmp_path) -> None:
    path = tmp_path / "none.json"
    path.write_text('{"entries": []}', encoding="utf-8")
    monkeypatch.setattr(food_guidance, "DATA_PATH", path)
    food_guidance.clear_curated_cache()


def test_ttl_is_seven_days():
    assert label_cache.TTL_SECONDS == 7 * 24 * 60 * 60


def test_default_directory_is_gitignored():
    assert label_cache.DEFAULT_DIR.name == ".label_cache"
    lines = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".label_cache/" in [line.strip() for line in lines]


def test_cache_is_off_when_no_directory_is_configured():
    calls = []

    def loader():
        calls.append(1)
        return "v"

    label_cache.cached("ns", "k", loader)
    label_cache.cached("ns", "k", loader)

    assert len(calls) == 2


def test_environment_variable_selects_directory_and_empty_disables(
    monkeypatch, tmp_path
):
    label_cache.reset_cache_dir()
    monkeypatch.setenv(label_cache.ENV_VAR, str(tmp_path))
    assert label_cache.cache_dir() == tmp_path
    monkeypatch.setenv(label_cache.ENV_VAR, "")
    assert label_cache.cache_dir() is None
    monkeypatch.delenv(label_cache.ENV_VAR)
    assert label_cache.cache_dir() == label_cache.DEFAULT_DIR


def test_second_lookup_is_served_from_disk(tmp_path):
    label_cache.set_cache_dir(tmp_path)
    calls = []

    def loader():
        calls.append(1)
        return {"text": "label"}

    assert label_cache.cached("ns", "k", loader) == {"text": "label"}
    assert label_cache.cached("ns", "k", loader) == {"text": "label"}
    assert len(calls) == 1


def test_none_means_no_label_and_is_cached(tmp_path):
    label_cache.set_cache_dir(tmp_path)
    calls = []

    def loader():
        calls.append(1)

    assert label_cache.cached("ns", "k", loader) is None
    assert label_cache.cached("ns", "k", loader) is None
    assert len(calls) == 1


def test_keys_and_namespaces_do_not_collide(tmp_path):
    label_cache.set_cache_dir(tmp_path)

    assert label_cache.cached("a", "k", lambda: 1) == 1
    assert label_cache.cached("b", "k", lambda: 2) == 2
    assert label_cache.cached("a", "other", lambda: 3) == 3
    assert label_cache.cached("a", "k", lambda: 99) == 1


def test_entry_expires_after_seven_days(tmp_path, monkeypatch):
    label_cache.set_cache_dir(tmp_path)
    clock = [1_000_000.0]
    monkeypatch.setattr(
        label_cache,
        "time",
        types.SimpleNamespace(time=lambda: clock[0], monotonic_ns=time.monotonic_ns),
    )
    values = iter(["old", "new"])

    first = label_cache.cached("ns", "k", lambda: next(values))
    clock[0] += label_cache.TTL_SECONDS - 1
    still_fresh = label_cache.cached("ns", "k", lambda: next(values))
    clock[0] += 2
    expired = label_cache.cached("ns", "k", lambda: next(values))

    assert (first, still_fresh, expired) == ("old", "old", "new")


def test_a_failing_loader_is_not_cached(tmp_path):
    label_cache.set_cache_dir(tmp_path)

    def boom():
        raise RuntimeError("openFDA down")

    with pytest.raises(RuntimeError):
        label_cache.cached("ns", "k", boom)

    assert label_cache.cached("ns", "k", lambda: "recovered") == "recovered"


def test_corrupt_cache_file_is_reloaded_not_fatal(tmp_path):
    label_cache.set_cache_dir(tmp_path)
    path = label_cache._path_for(tmp_path, "ns", "k")
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    assert label_cache.cached("ns", "k", lambda: "fresh") == "fresh"
    assert label_cache.cached("ns", "k", lambda: "ignored") == "fresh"


def test_unwritable_cache_directory_does_not_break_the_lookup(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    label_cache.set_cache_dir(blocker / "sub")

    assert label_cache.cached("ns", "k", lambda: "v") == "v"


def test_fetch_concurrently_runs_keys_in_parallel():
    barrier = threading.Barrier(3, timeout=5)

    def fetch(key):
        barrier.wait()
        return key.upper()

    assert label_cache.fetch_concurrently(["a", "b", "c"], fetch) == {
        "a": "A",
        "b": "B",
        "c": "C",
    }


def test_fetch_concurrently_returns_a_failure_per_key_without_losing_the_rest():
    def fetch(key):
        if key == "bad":
            raise ValueError("nope")
        return key

    result = label_cache.fetch_concurrently(["ok", "bad", "ok"], fetch)

    assert result["ok"] == "ok"
    assert isinstance(result["bad"], ValueError)
    assert list(result) == ["ok", "bad"]


def test_fetch_concurrently_with_no_keys_is_empty():
    assert label_cache.fetch_concurrently([], lambda key: key) == {}


def test_food_label_fetch_is_cached_on_disk_and_trimmed(tmp_path, monkeypatch):
    label_cache.set_cache_dir(tmp_path)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["search"])
        label = {
            "set_id": "abc",
            "directions": ["Take with food."],
            "spl_unclassified_section": ["not needed"],
            "openfda": {"route": ["ORAL"], "manufacturer_name": ["Acme"]},
        }
        return httpx.Response(200, json={"results": [label]})

    monkeypatch.setattr(
        food_guidance, "_http", httpx.Client(transport=httpx.MockTransport(handler))
    )
    food_guidance.clear_cache()

    first = food_guidance.fetch_labels_for_substance("Ibuprofen")
    food_guidance.clear_cache()
    second = food_guidance.fetch_labels_for_substance("Ibuprofen")
    food_guidance.clear_cache()

    assert len(calls) == 1
    assert first == second
    assert first[0]["directions"] == ["Take with food."]
    assert first[0]["set_id"] == "abc"
    assert first[0]["openfda"] == {"route": ["ORAL"]}
    assert "spl_unclassified_section" not in first[0]


def test_unreachable_openfda_is_never_written_to_the_disk_cache(tmp_path, monkeypatch):
    label_cache.set_cache_dir(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    monkeypatch.setattr(
        food_guidance, "_http", httpx.Client(transport=httpx.MockTransport(handler))
    )
    food_guidance.clear_cache()

    with pytest.raises(food_guidance.OpenFDAUnavailable):
        food_guidance.fetch_labels_for_substance("Ibuprofen")

    assert list(tmp_path.rglob("*.json")) == []
    food_guidance.clear_cache()


def test_regimen_labels_are_fetched_concurrently(monkeypatch, tmp_path):
    empty_curated(monkeypatch, tmp_path)
    barrier = threading.Barrier(2, timeout=5)

    def fake(name, **kw):
        barrier.wait()
        return [oral_directions_label()]

    monkeypatch.setattr(food_guidance, "fetch_labels_for_substance", fake)
    items = [
        food_guidance.RegimenItem("ibuprofen", "Ibuprofen", False, "Ibuprofen"),
        food_guidance.RegimenItem("naproxen", "Naproxen", False, "Naproxen"),
    ]

    body, _ = food_guidance._respond(items, [])
    food_guidance.clear_curated_cache()

    assert {f["name"] for f in body["guidance"]} == {"Ibuprofen", "Naproxen"}


def test_one_unreachable_substance_does_not_hide_the_others(monkeypatch, tmp_path):
    empty_curated(monkeypatch, tmp_path)

    def fake(name, **kw):
        if name == "Naproxen":
            raise food_guidance.OpenFDAUnavailable("down")
        return [oral_directions_label()]

    monkeypatch.setattr(food_guidance, "fetch_labels_for_substance", fake)
    items = [
        food_guidance.RegimenItem("ibuprofen", "Ibuprofen", False, "Ibuprofen"),
        food_guidance.RegimenItem("naproxen", "Naproxen", False, "Naproxen"),
    ]

    body, _ = food_guidance._respond(items, [])
    food_guidance.clear_curated_cache()

    assert {f["name"] for f in body["guidance"]} == {"Ibuprofen"}
    assert any("unreachable" in r and "Naproxen" in r for r in body["unchecked_reason"])
