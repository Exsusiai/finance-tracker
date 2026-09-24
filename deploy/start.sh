#!/usr/bin/env bash
# Start Finance Tracker (backend + frontend) in detached tmux sessions.
#
# Why tmux: launching long-lived processes over a one-shot SSH command is
# unreliable (the child is tied to the SSH channel and gets killed on exit).
# A detached tmux session survives SSH disconnect. NOTE: tmux does NOT survive a
# reboot — re-run this script after reboot, or wire a systemd unit (see
# docs/DEPLOYMENT.md).
#
# Ports are overridable via env:  BACKEND_PORT (default 8000), FRONTEND_PORT
# (default 3100 — 3000 is taken by another app on the deploy host).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3100}"
ENV_FILE="$ROOT/.env"

cd "$ROOT"

# --- Resolve BACKEND_HOST / AUTH_DISABLED from the project-root .env -------
# The backend's startup guard (app/main.py) refuses to boot when
# AUTH_DISABLED=true and Settings.backend_host is non-loopback — but Settings
# reads BACKEND_HOST from this same .env, independently of what host uvicorn
# is actually told to bind to. Previously this script hardcoded
# `--host 0.0.0.0`, so an .env with BACKEND_HOST=127.0.0.1 (guard sees
# loopback → passes) + AUTH_DISABLED=true would still end up bound to
# 0.0.0.0 in reality — unauthenticated exposure on the LAN. Reading the same
# value here (and exporting it into the uvicorn process env below) keeps the
# guard's view and the actual bind in sync.
_env_var() {
  local key="$1"
  [ -f "$ENV_FILE" ] || return 0
  # `grep` exits 1 when the key is simply absent — a normal, expected outcome
  # here (not an error) — so it's wrapped in `{ ... || true; }` to keep that
  # from tripping `pipefail` under `set -e` and aborting the whole script
  # before the guard even runs.
  { grep -E "^[[:space:]]*${key}[[:space:]]*=" "$ENV_FILE" 2>/dev/null || true; } \
    | tail -n1 \
    | sed -E "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//" \
    | sed -E 's/[[:space:]]*(#.*)?$//' \
    | sed -E 's/^"//; s/"$//' \
    | sed -E "s/^'//; s/'\$//"
}

# pydantic's bool parsing (used by Settings.auth_disabled) accepts these
# values case-insensitively as true; mirror it here.
_is_truthy() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on|t|y) return 0 ;;
    *) return 1 ;;
  esac
}

# Shell env override (if the caller already exported BACKEND_HOST) wins,
# same precedence pattern as BACKEND_PORT/FRONTEND_PORT above; otherwise read
# .env; otherwise fall back to 0.0.0.0 (preserves this script's prior
# hardcoded behaviour — cortana-box's deployed .env sets AUTH_DISABLED=false
# for exactly this reason).
BACKEND_HOST="${BACKEND_HOST:-$(_env_var BACKEND_HOST)}"
BACKEND_HOST="${BACKEND_HOST:-0.0.0.0}"
AUTH_DISABLED_VAL="${AUTH_DISABLED:-$(_env_var AUTH_DISABLED)}"

if _is_truthy "$AUTH_DISABLED_VAL"; then
  case "$BACKEND_HOST" in
    127.0.0.1|::1|localhost) : ;;
    *)
      echo "[start] ERROR: AUTH_DISABLED=true (.env) but BACKEND_HOST='${BACKEND_HOST}' is not loopback." >&2
      echo "[start]        Starting would bind an unauthenticated API to a non-loopback interface." >&2
      echo "[start]        Fix: set AUTH_DISABLED=false in .env (and configure FINANCE_TRACKER_API_TOKEN)," >&2
      echo "[start]        or set BACKEND_HOST to 127.0.0.1 / ::1 / localhost." >&2
      exit 1
      ;;
  esac
fi

# Kill anything still listening on a port before starting. A stale listener
# outside our tmux sessions (e.g. an orphan from a pre-tmux launch) makes the
# new server die on EADDRINUSE while the health check below passes against the
# WRONG process — the orphan then serves an in-memory build manifest that no
# longer matches .next on disk (chunk 400s, "Application error" on every page).
_free_port() {
  local port="$1" pids
  pids=$(lsof -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
  if [ -n "$pids" ]; then
    echo "[start] WARN: port ${port} still held by pid(s) ${pids} — killing stale listener(s)"
    kill $pids 2>/dev/null || true
    sleep 2
    pids=$(lsof -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
    if [ -n "$pids" ]; then
      echo "[start] ERROR: port ${port} still occupied by pid(s) ${pids} after kill; aborting." >&2
      exit 1
    fi
  fi
}

echo "[start] backend → tmux ft-backend on ${BACKEND_HOST}:${BACKEND_PORT}"
tmux kill-session -t ft-backend 2>/dev/null || true
sleep 1
_free_port "${BACKEND_PORT}"
tmux new-session -d -s ft-backend \
  "cd '$ROOT' && BACKEND_HOST='${BACKEND_HOST}' .venv/bin/python -m uvicorn app.main:app --host '${BACKEND_HOST}' --port ${BACKEND_PORT} 2>&1 | tee /tmp/ft-backend.log"

echo "[start] frontend → tmux ft-frontend on :${FRONTEND_PORT}"
tmux kill-session -t ft-frontend 2>/dev/null || true
sleep 1
_free_port "${FRONTEND_PORT}"
tmux new-session -d -s ft-frontend \
  "cd '$ROOT/frontend' && npx next start -H 0.0.0.0 -p ${FRONTEND_PORT} 2>&1 | tee /tmp/ft-frontend.log"

sleep 6
echo "[start] backend health: $(curl -s -o /dev/null -w '%{http_code}' http://localhost:${BACKEND_PORT}/api/v1/health)"
echo "[start] frontend:       $(curl -s -o /dev/null -w '%{http_code}' http://localhost:${FRONTEND_PORT}/dashboard)"
echo "[start] tmux sessions:"
tmux ls 2>/dev/null | sed 's/^/    /'
