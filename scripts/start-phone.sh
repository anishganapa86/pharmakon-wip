#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

dotenv_value() {
  [ -f .env ] || return 0
  grep -E "^[[:space:]]*$1[[:space:]]*=" .env | tail -n 1 \
    | sed -E "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//; s/^[\"']//; s/[\"']?[[:space:]]*\$//" || true
}

EMAIL="${PHARMAKON_DEMO_EMAIL:-$(dotenv_value PHARMAKON_DEMO_EMAIL)}"
PASSWORD="${PHARMAKON_DEMO_PASSWORD:-$(dotenv_value PHARMAKON_DEMO_PASSWORD)}"
if [ -z "$EMAIL" ] || [ -z "$PASSWORD" ]; then
  echo "PHARMAKON_DEMO_EMAIL and PHARMAKON_DEMO_PASSWORD are not set. Add both to .env (see .env.example) or set them in the environment, then run this again." >&2
  exit 1
fi

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared not found. Install it with: brew install cloudflared" >&2
  exit 1
fi

echo "Stopping old servers and tunnels..."
for port in 5173 5174 8000; do
  pids="$(lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  [ -n "$pids" ] && kill -9 $pids 2>/dev/null || true
done
pkill -x cloudflared 2>/dev/null || true
sleep 1

LOG="$(mktemp -t pharmakon-tunnel.XXXXXX)"
echo "Starting the tunnel..."
cloudflared tunnel --url http://localhost:5173 >/dev/null 2>"$LOG" &
TUNNEL_PID=$!

cleanup() {
  echo "Stopping the tunnel..."
  kill "$TUNNEL_PID" 2>/dev/null || true
  for port in 5173 5174 8000; do
    pids="$(lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null || true)"
    [ -n "$pids" ] && kill -9 $pids 2>/dev/null || true
  done
  rm -f "$LOG"
}
trap cleanup EXIT

URL=""
for _ in $(seq 1 60); do
  URL="$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -n 1 || true)"
  [ -n "$URL" ] && break
  sleep 1
done
if [ -z "$URL" ]; then
  echo "The tunnel did not start. Check your internet connection. Log: $LOG" >&2
  exit 1
fi

export PHARMAKON_COOKIE_SECURE=1
export VITE_API_BASE=/api
export PHARMAKON_ALLOWED_HOSTS=.trycloudflare.com
export PHARMAKON_CORS_ORIGINS="$URL"
export PHARMAKON_DEMO_EMAIL="$EMAIL"
export PHARMAKON_DEMO_PASSWORD="$PASSWORD"

PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python

echo "Seeding the demo user..."
"$PY" scripts/seed_demo_user.py --food-demo

command -v pbcopy >/dev/null 2>&1 && printf '%s' "$URL" | pbcopy || true

echo
echo "======================================================="
echo " Open this in Safari on your iPhone:"
echo "   $URL"
echo " Log in with: $EMAIL  (the demo password from .env)"
echo " (The address is also copied to your clipboard.)"
echo " Press Ctrl+C in this window to stop everything."
echo "======================================================="
echo

./run.sh
