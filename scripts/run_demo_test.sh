#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

gate_off=0
repeat=1
skip_phone=0
while [ $# -gt 0 ]; do
  case "$1" in
    --gate-off) gate_off=1 ;;
    --repeat) repeat="$2"; shift ;;
    --no-phone) skip_phone=1 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ ! -f .venv/bin/activate ]; then
  echo "No .venv found. See docs/SETUP.md." >&2
  exit 1
fi
source .venv/bin/activate

mkdir -p reports/runs
SCRATCH="$(mktemp -d)"
API_PID=""
WEB_PID=""
OVERALL=0

free_ports() {
  for port in 8000 5173; do
    pids="$(lsof -ti "tcp:$port" 2>/dev/null || true)"
    if [ -n "$pids" ]; then
      echo "freeing port $port (pids: $(echo $pids | tr '\n' ' '))"
      kill $pids 2>/dev/null || true
      sleep 1
      pids="$(lsof -ti "tcp:$port" 2>/dev/null || true)"
      if [ -n "$pids" ]; then kill -9 $pids 2>/dev/null || true; fi
    fi
  done
}

stop_servers() {
  if [ -n "$API_PID" ]; then kill "$API_PID" 2>/dev/null; wait "$API_PID" 2>/dev/null; fi
  if [ -n "$WEB_PID" ]; then kill "$WEB_PID" 2>/dev/null; wait "$WEB_PID" 2>/dev/null; fi
  API_PID=""
  WEB_PID=""
  free_ports >/dev/null 2>&1
}

cleanup() {
  stop_servers
  rm -rf "$SCRATCH"
}
trap cleanup EXIT

wait_for() {
  local url="$1" name="$2"
  for _ in $(seq 1 120); do
    if curl -sf -o /dev/null "$url"; then
      return 0
    fi
    sleep 0.5
  done
  echo "$name did not respond at $url" >&2
  return 1
}

start_api() {
  local tag="$1"
  export PHARMAKON_DB_URL="sqlite:///$SCRATCH/demo_$tag.db"
  if [ "$gate_off" = 1 ]; then
    export SHOW_UNVERIFIED_RULES=0
  else
    export SHOW_UNVERIFIED_RULES=1
  fi
  uvicorn app.api.main:app --port 8000 --log-level warning >"$SCRATCH/api_$tag.log" 2>&1 &
  API_PID=$!
  wait_for "http://localhost:8000/products/search?q=nodoz" "API"
}

start_web() {
  (cd client && exec npx vite preview --port 5173 --strictPort >"$SCRATCH/web.log" 2>&1) &
  WEB_PID=$!
  wait_for "http://localhost:5173/" "client preview"
}

free_ports

if [ -f scripts/warm_label_cache.py ]; then
  if [ -z "$(ls -A .label_cache 2>/dev/null)" ]; then
    echo "label cache empty; warming"
    python scripts/warm_label_cache.py || echo "label cache warm-up failed; continuing"
  fi
else
  echo "scripts/warm_label_cache.py not present on this branch; skipping label cache warm-up"
fi

echo "building client"
if ! (cd client && npm run build >"$SCRATCH/build.log" 2>&1); then
  echo "client build failed"
  tail -30 "$SCRATCH/build.log"
  exit 1
fi

if [ "$gate_off" = 1 ]; then
  labels=("gate-off")
  rm -rf reports/runs/gate-off
else
  labels=()
  for i in $(seq 1 "$repeat"); do labels+=("r$i"); done
  rm -rf reports/runs/r[0-9]*
fi

for label in "${labels[@]}"; do
  RUN_DIR="$ROOT/reports/runs/$label"
  mkdir -p "$RUN_DIR/screenshots"
  export DEMO_RUN_DIR="$RUN_DIR"
  echo "=== run $label (SHOW_UNVERIFIED_RULES=$([ "$gate_off" = 1 ] && echo 0 || echo 1)) ==="

  if [ "$gate_off" = 0 ]; then
    start_api "${label}_smoke" || OVERALL=1
    python scripts/api_smoke.py --base http://localhost:8000 --out "$RUN_DIR/api_smoke.json" || OVERALL=1
    stop_servers
  fi

  start_api "$label" || { OVERALL=1; stop_servers; continue; }
  start_web || { OVERALL=1; stop_servers; continue; }

  if [ "$gate_off" = 1 ]; then
    (cd client && DEMO_GATE_ONLY=1 npx playwright test -c e2e/playwright.config.ts --project=desktop) || OVERALL=1
  else
    (cd client && npx playwright test -c e2e/playwright.config.ts --project=desktop) || OVERALL=1
    if [ "$skip_phone" = 0 ]; then
      (cd client && npx playwright test -c e2e/playwright.config.ts --project=phone) || OVERALL=1
    fi
  fi
  stop_servers
done

if [ -f "reports/runs/${labels[0]}/api_smoke.json" ]; then
  cp "reports/runs/${labels[0]}/api_smoke.json" reports/api_smoke.json
fi

python scripts/build_demo_report.py --runs reports/runs --out reports/demo_test.md
echo "report: reports/demo_test.md"
exit "$OVERALL"
