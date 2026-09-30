"""Coverage report for app/api/food_guidance.py's live openFDA path.

Standalone diagnostic, NOT part of the test suite: it makes live openFDA
calls (rate-limited, and cached to disk across runs) to see, for every
registry substance, whether an oral label exists and how many qualifying
(item + instruction cue) sentences it produces across every item term.
Never imported by app code or the tests -- run it by hand after the
registry or ITEM_TERMS/TEXT_FIELDS change.

    python scripts/food_guidance_coverage.py [--out FILE] [--cache FILE]

Prints a per-substance table to stdout; writes the full per-substance
results to --out as a CSV (default: ./food_guidance_coverage.csv). Neither
that CSV nor the cache file is meant to be committed -- they're a snapshot
of live, external, time-varying API state.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api import food_guidance  # noqa: E402
from app.registry.loader import load_registry  # noqa: E402

DEFAULT_OUT_PATH = Path("food_guidance_coverage.csv")
DEFAULT_CACHE_PATH = Path(".food_guidance_coverage_cache.json")

# openFDA's public (no API key) rate limit is 40 requests/minute; this stays
# comfortably under it. Substances already in the cache skip the wait
# entirely, so a re-run after adding one new substance is fast.
MIN_SECONDS_BETWEEN_CALLS = 1.6


def load_cache(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_cache(path: Path, cache: dict[str, dict]) -> None:
    path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


class RateLimiter:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()


@dataclass
class SubstanceCoverage:
    substance_id: str
    display_name: str
    has_label: bool | None  # None: openFDA couldn't be reached
    route: str | None
    is_oral: bool
    qualifying_sentence_count: int  # summed across every item term


def _coverage_for(
    sid: str, name: str, cache: dict[str, dict], limiter: RateLimiter
) -> SubstanceCoverage:
    if sid in cache:
        cached = cache[sid]
        return SubstanceCoverage(
            sid,
            name,
            cached["has_label"],
            cached["route"],
            cached["is_oral"],
            cached["qualifying_sentence_count"],
        )
    limiter.wait()
    try:
        label = food_guidance.fetch_label_for_substance(name)
    except food_guidance.OpenFDAUnavailable as exc:
        print(f"  ! openFDA unreachable for {name}: {exc}", file=sys.stderr)
        return SubstanceCoverage(sid, name, None, None, False, 0)

    if label is None:
        result = SubstanceCoverage(sid, name, False, None, False, 0)
    else:
        route = food_guidance.label_route(label)
        oral = food_guidance.is_oral(label)
        count = (
            sum(
                len(food_guidance.qualifying_matches(label, item))
                for item in food_guidance.ITEM_TERMS
            )
            if oral
            else 0
        )
        result = SubstanceCoverage(sid, name, True, route, oral, count)

    cache[sid] = {
        "has_label": result.has_label,
        "route": result.route,
        "is_oral": result.is_oral,
        "qualifying_sentence_count": result.qualifying_sentence_count,
    }
    return result


def build_coverage(
    registry, cache: dict[str, dict], limiter: RateLimiter
) -> list[SubstanceCoverage]:
    return [
        _coverage_for(entry["id"], entry["name"], cache, limiter)
        for entry in sorted(registry.all(), key=lambda e: e["id"])
    ]


def _yn(value: bool | None) -> str:
    return "unknown" if value is None else ("yes" if value else "no")


def print_summary(coverage: list[SubstanceCoverage]) -> None:
    print(f"\n{len(coverage)} substances in the registry\n")
    header = (
        f"{'id':<24}{'has label':<11}{'route':<10}{'oral':<6}{'qualifying sentences'}"
    )
    print(header)
    print("-" * len(header))
    for c in coverage:
        print(
            f"{c.substance_id:<24}"
            f"{_yn(c.has_label):<11}"
            f"{(c.route or '-'):<10}"
            f"{_yn(c.is_oral):<6}"
            f"{c.qualifying_sentence_count}"
        )

    oral_with_guidance = sum(
        1 for c in coverage if c.is_oral and c.qualifying_sentence_count > 0
    )
    oral_total = sum(1 for c in coverage if c.is_oral)
    print(
        f"\n{oral_total} oral substances, {oral_with_guidance} with at least "
        "one qualifying sentence"
    )


def write_csv(path: Path, coverage: list[SubstanceCoverage]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "substance_id",
                "has_label",
                "route",
                "is_oral",
                "qualifying_sentence_count",
            ]
        )
        for c in coverage:
            writer.writerow(
                [
                    c.substance_id,
                    c.has_label,
                    c.route,
                    c.is_oral,
                    c.qualifying_sentence_count,
                ]
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE_PATH)
    args = parser.parse_args()

    registry = load_registry()
    cache = load_cache(args.cache)
    limiter = RateLimiter(MIN_SECONDS_BETWEEN_CALLS)

    print(
        "Checking openFDA oral-label coverage per substance "
        f"(cached at {args.cache}; new lookups rate-limited to one per "
        f"{MIN_SECONDS_BETWEEN_CALLS:.1f}s)..."
    )
    coverage = build_coverage(registry, cache, limiter)
    save_cache(args.cache, cache)

    print_summary(coverage)
    write_csv(args.out, coverage)
    print(f"\nFull per-substance results written to {args.out}")


if __name__ == "__main__":
    main()
