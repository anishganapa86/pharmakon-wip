"""Coverage report for the medication interaction checker's two sources.

Standalone diagnostic, NOT part of the test suite: it makes live openFDA
calls (rate-limited, and cached to disk across runs) to see where
app/api/interactions.py's curated rules and label-text search do and don't
reach, across every pair of registry substances. Never imported by app code
or by the tests -- run it by hand after the registry or the curated rule set
changes.

    python scripts/interaction_coverage.py [--out FILE] [--cache FILE]

Prints a per-substance table and a per-pair summary to stdout; writes the
full per-pair results to --out as a CSV (default: ./interaction_coverage.csv).
Neither that CSV nor the cache file is meant to be committed -- they're a
snapshot of live, external, time-varying API state.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api import interactions  # noqa: E402
from app.registry.loader import Registry, load_registry  # noqa: E402

DEFAULT_OUT_PATH = Path("interaction_coverage.csv")
DEFAULT_CACHE_PATH = Path(".interaction_coverage_cache.json")

# openFDA's public (no API key) rate limit is 40 requests/minute; this stays
# comfortably under it. Substances already in the cache skip the wait
# entirely, so a re-run after adding one new substance is fast.
MIN_SECONDS_BETWEEN_CALLS = 1.6


def load_cache(path: Path) -> dict[str, bool]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_cache(path: Path, cache: dict[str, bool]) -> None:
    path.write_text(json.dumps(cache, indent=2, sort_keys=True))


class RateLimiter:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()


def has_drug_interactions_label(
    sid: str, generic_name: str, cache: dict[str, bool], limiter: RateLimiter
) -> bool | None:
    """True/False from cache or a live lookup; None if openFDA couldn't be
    reached (reported, never silently treated as False)."""
    if sid in cache:
        return cache[sid]
    limiter.wait()
    try:
        text = interactions.fetch_drug_interactions_text(generic_name)
    except interactions.OpenFDAUnavailable as exc:
        print(f"  ! openFDA unreachable for {generic_name}: {exc}", file=sys.stderr)
        return None
    result = text is not None
    cache[sid] = result
    return result


def in_curated_rule(sid: str, rules: tuple[interactions.CuratedRule, ...]) -> bool:
    """Direct participant in a curated rule, by id or via its drug class."""
    for rule in rules:
        for term in (rule.substance_a, rule.substance_b):
            if term == sid or sid in interactions.CLASS_MEMBERS.get(term, ()):
                return True
    return False


@dataclass
class SubstanceCoverage:
    substance_id: str
    display_name: str
    resolves_to_self: bool  # registry.resolve(display_name) round-trips
    has_label_interactions: bool | None  # None: openFDA couldn't be reached
    in_curated_rule: bool
    in_class_map: bool


def build_substance_coverage(
    registry: Registry,
    rules: tuple[interactions.CuratedRule, ...],
    cache: dict[str, bool],
    limiter: RateLimiter,
) -> list[SubstanceCoverage]:
    out = []
    for entry in sorted(registry.all(), key=lambda e: e["id"]):
        sid = entry["id"]
        out.append(
            SubstanceCoverage(
                substance_id=sid,
                display_name=entry["name"],
                resolves_to_self=registry.resolve(entry["name"]) == sid,
                has_label_interactions=has_drug_interactions_label(
                    sid, entry["name"], cache, limiter
                ),
                in_curated_rule=in_curated_rule(sid, rules),
                in_class_map=sid in interactions.SUBSTANCE_CLASSES,
            )
        )
    return out


@dataclass
class PairCoverage:
    substance_a: str
    substance_b: str
    curated: bool
    label_checkable: bool  # at least one side has a drug_interactions label
    # "curated" > "label" > "uncheckable" -- what a real check-pair would do.
    status: str


def build_pair_coverage(
    substances: list[SubstanceCoverage], rules: tuple[interactions.CuratedRule, ...]
) -> list[PairCoverage]:
    by_id = {s.substance_id: s for s in substances}
    out = []
    for a, b in combinations(sorted(by_id), 2):
        sa, sb = by_id[a], by_id[b]
        curated = any(rule.matches(a, b) for rule in rules)
        label_checkable = bool(sa.has_label_interactions) or bool(
            sb.has_label_interactions
        )
        if curated:
            status = "curated"
        elif label_checkable:
            status = "label"
        else:
            status = "uncheckable"
        out.append(PairCoverage(a, b, curated, label_checkable, status))
    return out


def _yn(value: bool | None) -> str:
    return "unknown" if value is None else ("yes" if value else "no")


def print_summary(
    substances: list[SubstanceCoverage], pairs: list[PairCoverage]
) -> None:
    print(f"\n{len(substances)} substances in the registry\n")
    header = (
        f"{'id':<24}{'resolves':<10}{'openFDA interactions':<22}{'curated':<9}{'class'}"
    )
    print(header)
    print("-" * len(header))
    for s in substances:
        print(
            f"{s.substance_id:<24}"
            f"{_yn(s.resolves_to_self):<10}"
            f"{_yn(s.has_label_interactions):<22}"
            f"{_yn(s.in_curated_rule):<9}"
            f"{_yn(s.in_class_map)}"
        )

    total = len(pairs)
    counts = {
        status: sum(1 for p in pairs if p.status == status)
        for status in ("curated", "label", "uncheckable")
    }
    print(f"\n{total} substance pairs")
    for status in ("curated", "label", "uncheckable"):
        n = counts[status]
        pct = f"{n / total:.0%}" if total else "n/a"
        print(f"  {status:<12} {n:>4}  ({pct})")


def write_csv(path: Path, pairs: list[PairCoverage]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["substance_a", "substance_b", "curated", "label_checkable", "status"]
        )
        for p in pairs:
            writer.writerow(
                [p.substance_a, p.substance_b, p.curated, p.label_checkable, p.status]
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE_PATH)
    args = parser.parse_args()

    registry = load_registry()
    rules = interactions.load_rules()
    cache = load_cache(args.cache)
    limiter = RateLimiter(MIN_SECONDS_BETWEEN_CALLS)

    print(
        "Checking openFDA drug_interactions label availability per substance "
        f"(cached at {args.cache}; new lookups rate-limited to one per "
        f"{MIN_SECONDS_BETWEEN_CALLS:.1f}s)..."
    )
    substances = build_substance_coverage(registry, rules, cache, limiter)
    save_cache(args.cache, cache)

    pairs = build_pair_coverage(substances, rules)
    print_summary(substances, pairs)
    write_csv(args.out, pairs)
    print(f"\nFull pairwise results written to {args.out}")


if __name__ == "__main__":
    main()
