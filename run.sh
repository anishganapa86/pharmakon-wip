#!/usr/bin/env bash
# Dev startup: API on :8000 (background), Vite client on :5173 (foreground).
# Ctrl-C stops both.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .venv/bin/activate ]; then
  echo "No .venv found. See docs/SETUP.md." >&2
  exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate

if [ ! -d client/node_modules ]; then
  (cd client && npm ci)
fi

export SHOW_UNVERIFIED_RULES="${SHOW_UNVERIFIED_RULES:-1}"

uvicorn app.api.main:app --port 8000 --reload --log-level warning &
UVICORN_PID=$!
trap 'kill "$UVICORN_PID" 2>/dev/null || true; wait "$UVICORN_PID" 2>/dev/null || true' EXIT

cd client
npm run dev
