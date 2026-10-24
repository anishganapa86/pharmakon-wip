from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest

from app.api import food_guidance, interactions, label_cache, side_effects

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "warm_label_cache.py"


@pytest.fixture
def warm_module():
    spec = importlib.util.spec_from_file_location("warm_label_cache", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def served(monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["search"])
        label = {
            "openfda": {"route": ["ORAL"]},
            "directions": ["Take with food."],
            "adverse_reactions": ["Nausea."],
            "drug_interactions": ["Avoid other NSAIDs."],
        }
        return httpx.Response(200, json={"results": [label]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    for module in (food_guidance, side_effects, interactions):
        monkeypatch.setattr(module, "_http", client)
        module.clear_cache()
    yield calls
    for module in (food_guidance, side_effects, interactions):
        module.clear_cache()


def test_warming_caches_all_three_lookups_per_substance(warm_module, served, tmp_path):
    label_cache.set_cache_dir(tmp_path)

    results = warm_module.warm(["Ibuprofen", "Naproxen"])

    assert warm_module.failures(results) == []
    assert len(served) == 6
    assert len(list(tmp_path.rglob("*.json"))) == 6


def test_a_second_warm_makes_no_requests(warm_module, served, tmp_path):
    label_cache.set_cache_dir(tmp_path)
    warm_module.warm(["Ibuprofen"])
    for module in (food_guidance, side_effects, interactions):
        module.clear_cache()
    served.clear()

    warm_module.warm(["Ibuprofen"])

    assert served == []


def test_unreachable_openfda_is_reported_not_cached(warm_module, monkeypatch, tmp_path):
    label_cache.set_cache_dir(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    for module in (food_guidance, side_effects, interactions):
        monkeypatch.setattr(module, "_http", client)
        module.clear_cache()

    problems = warm_module.failures(warm_module.warm(["Ibuprofen"]))

    assert len(problems) == 3
    assert list(tmp_path.rglob("*.json")) == []


def test_main_refuses_to_run_with_the_cache_disabled(warm_module, capsys):
    label_cache.set_cache_dir(None)

    assert warm_module.main([]) == 2
    assert "disabled" in capsys.readouterr().out


def test_limiter_spaces_requests(warm_module, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(warm_module.time, "sleep", slept.append)
    limiter = warm_module.Limiter(1.0)

    limiter.wait()
    limiter.wait()
    limiter.wait()

    assert slept[0] == pytest.approx(1.0, abs=0.2)
    assert slept[1] == pytest.approx(2.0, abs=0.2)


def test_combination_products_are_warmed_by_brand_name(warm_module, served, tmp_path):
    from app.registry.loader import load_registry

    label_cache.set_cache_dir(tmp_path)
    names = warm_module.combination_product_names(load_registry())

    assert "DayQuil" in names
    assert "NyQuil" in names
    assert "Advil" not in names

    results = warm_module.warm([], ["DayQuil"])

    assert warm_module.failures(results) == []
    assert len(served) == 1
    assert 'brand_name.exact:"DAYQUIL"' in served[0]
