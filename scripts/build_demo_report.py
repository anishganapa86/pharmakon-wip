from __future__ import annotations

import argparse
import json
import subprocess
from collections import defaultdict
from pathlib import Path

SLOW_REQUEST_COUNT = 5
API_PORT_MARKER = ":8000/"


def load_json(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text())


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def run_label_sort_key(path: Path) -> tuple[int, str]:
    name = path.name
    if name.startswith("r") and name[1:].isdigit():
        return (0, f"{int(name[1:]):04d}")
    return (1, name)


def md_cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def table(headers: list[str], rows: list[list[object]]) -> str:
    if not rows:
        return "_none_\n"
    out = ["| " + " | ".join(headers) + " |"]
    out.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        out.append("| " + " | ".join(md_cell(c) for c in row) + " |")
    return "\n".join(out) + "\n"


def git_branch() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def outcome(row: dict) -> str:
    if row["status"] == row["expected"]:
        return "known bug" if row["expected"] == "failed" else "pass"
    if row["status"] == "skipped":
        return "skipped"
    return "FAIL"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", default="reports/runs")
    parser.add_argument("--out", default="reports/demo_test.md")
    args = parser.parse_args()

    runs_root = Path(args.runs)
    run_dirs = sorted(
        [p for p in runs_root.iterdir() if p.is_dir() and p.name != "local"]
        if runs_root.exists()
        else [],
        key=run_label_sort_key,
    )
    labels = [p.name for p in run_dirs]

    results: dict[str, dict[str, dict]] = {}
    findings: dict[str, list[dict]] = {}
    smoke: dict[str, list[dict]] = {}
    for run_dir in run_dirs:
        data = load_json(run_dir / "pw_results.json")
        results[run_dir.name] = {
            f"{t['project']} > {t['id']}": t for t in (data or {}).get("tests", [])
        }
        findings[run_dir.name] = load_jsonl(run_dir / "findings.jsonl")
        smoke[run_dir.name] = load_json(run_dir / "api_smoke.json") or []

    lines: list[str] = []
    lines.append("# Demo test report\n")
    lines.append(f"- branch: `{git_branch()}`")
    lines.append(f"- runs: {', '.join(labels) if labels else 'none'}")
    lines.append(
        "- Not a medical device. This report describes an automated check of an "
        "educational demo.\n"
    )

    test_ids = sorted({tid for r in results.values() for tid in r})
    rows = []
    for tid in test_ids:
        per_run = [
            outcome(results[label][tid]) if tid in results[label] else "-"
            for label in labels
        ]
        seen = {o for o in per_run if o != "-"}
        flaky = "FLAKY" if len(seen) > 1 else ""
        durations = ", ".join(
            f"{results[label][tid]['durationMs'] / 1000:.1f}s"
            if tid in results[label]
            else "-"
            for label in labels
        )
        rows.append([tid, *per_run, flaky, durations])
    lines.append("## Pass/fail\n")
    lines.append(table(["test", *labels, "flaky", "duration"], rows))

    bugs: dict[str, str] = {}
    for label in labels:
        for tid, row in results[label].items():
            if row["expected"] == "failed":
                notes = [
                    a["description"] for a in row["annotations"] if a["type"] == "bug"
                ]
                bugs[tid] = " ".join(notes)
    lines.append("### Known bugs\n")
    lines.append(
        "These tests are marked test.fail: the app currently misbehaves as noted. "
        "A known-bug test that starts passing, or fails for another reason, "
        "is reported as FAIL.\n"
    )
    lines.append(
        table(["test", "note"], [[tid, note] for tid, note in sorted(bugs.items())])
    )

    failures = []
    for label in labels:
        for tid, row in results[label].items():
            if outcome(row) == "FAIL" and row.get("error"):
                failures.append([label, tid, row["error"].splitlines()[0][:300]])
    lines.append("### Failure messages\n")
    lines.append(table(["run", "test", "first line"], failures))

    lines.append("## API smoke\n")
    for label in labels:
        entries = smoke[label]
        if not entries:
            continue
        lines.append(f"### {label}\n")
        rows = []
        for e in entries:
            rows.append(
                [
                    e["name"],
                    f"{e['method']} {e['path']}",
                    e["status"],
                    e.get("expected_status", ""),
                    f"{e['latency_ms']:.0f}",
                    e.get("body_shape", ""),
                    "; ".join(e.get("flags", [])),
                ]
            )
        lines.append(
            table(
                [
                    "check",
                    "request",
                    "status",
                    "expected",
                    "ms",
                    "body shape",
                    "flags",
                ],
                rows,
            )
        )

    lines.append("## Console errors per screen\n")
    console: dict[tuple[str, str], set[str]] = defaultdict(set)
    for label in labels:
        for f in findings[label]:
            if f["kind"] == "console" and f.get("level") in ("error", "pageerror"):
                console[(f["test"], f["screen"])].add(f["text"][:300])
    rows = [
        [t, s, " / ".join(sorted(texts))] for (t, s), texts in sorted(console.items())
    ]
    lines.append(table(["test", "screen", "errors"], rows))

    lines.append("## Failed network requests\n")
    failed: dict[tuple, int] = defaultdict(int)
    for label in labels:
        for f in findings[label]:
            if f["kind"] == "network" and (f.get("failure") or f["status"] >= 400):
                key = (
                    f["test"],
                    f["screen"],
                    f["method"],
                    f["url"],
                    f["status"],
                    f.get("failure") or "",
                    bool(f.get("expected")),
                )
                failed[key] += 1
    rows = [
        [t, s, m, u, st, fl, "expected" if ex else "UNEXPECTED", n]
        for (t, s, m, u, st, fl, ex), n in sorted(failed.items())
    ]
    lines.append(
        table(
            ["test", "screen", "method", "url", "status", "failure", "kind", "count"],
            rows,
        )
    )

    lines.append("## Forbidden-word hits\n")
    hits: dict[tuple, int] = defaultdict(int)
    for label in labels:
        for f in findings[label]:
            if f["kind"] == "forbidden":
                hits[(f["screen"], f["word"], f["context"])] += 1
    rows = [[s, w, f"…{c}…"] for (s, w, c), _ in sorted(hits.items())]
    lines.append(table(["screen", "word", "context"], rows))

    lines.append("## Text and layout rule failures\n")
    problems: dict[tuple, set[str]] = defaultdict(set)
    for label in labels:
        for f in findings[label]:
            if f["kind"] == "check":
                for p in f["problems"]:
                    problems[
                        (f["test"], f["step"], f["screen"], p["type"], p["detail"])
                    ].add(label)
    rows = [
        [t, st, s, ty, d, ", ".join(sorted(ls))]
        for (t, st, s, ty, d), ls in sorted(problems.items())
    ]
    lines.append(table(["test", "step", "screen", "rule", "detail", "runs"], rows))

    lines.append("## Steps slower than 3 seconds\n")
    slow: dict[tuple, list[float]] = defaultdict(list)
    for label in labels:
        for f in findings[label]:
            if f["kind"] == "slow":
                slow[(f["test"], f["step"])].append(f["ms"])
    rows = [
        [t, s, ", ".join(f"{ms / 1000:.1f}s" for ms in v)]
        for (t, s), v in sorted(slow.items())
    ]
    lines.append(table(["test", "step", "durations per run"], rows))

    lines.append("## Slowest requests\n")
    reqs = []
    for label in labels:
        for f in findings[label]:
            if (
                f["kind"] == "network"
                and API_PORT_MARKER in f["url"]
                and f.get("ms") is not None
            ):
                reqs.append((f["ms"], label, f))
    for e in smoke.values():
        for s in e:
            reqs.append(
                (
                    s["latency_ms"],
                    "smoke",
                    {
                        "method": s["method"],
                        "url": s["path"],
                        "status": s["status"],
                        "test": s["name"],
                    },
                )
            )
    reqs.sort(key=lambda r: -r[0])
    rows = [
        [f"{ms:.0f}", label, f["method"], f["url"], f["status"], f["test"]]
        for ms, label, f in reqs[:SLOW_REQUEST_COUNT]
    ]
    lines.append(table(["ms", "run", "method", "url", "status", "during"], rows))

    lines.append("## Recorded bugs\n")
    bugs: dict[tuple, set[str]] = defaultdict(set)
    for label in labels:
        for f in findings[label]:
            if f["kind"] == "bug":
                bugs[
                    (f["test"], f["screen"], f["expected"], f["saw"], f.get("shot", ""))
                ].add(label)
    rows = [
        [t, s, e, saw, shot, ", ".join(sorted(ls))]
        for (t, s, e, saw, shot), ls in sorted(bugs.items())
    ]
    lines.append(
        table(["test", "screen", "expected", "saw", "screenshot", "runs"], rows)
    )

    lines.append("## Screenshots\n")
    shots = []
    for label in labels:
        for f in findings[label]:
            if f["kind"] == "shot":
                shots.append([label, f["test"], f["step"], f["path"]])
    lines.append(table(["run", "test", "step", "path"], shots))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
