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

cd "$ROOT"

echo "[start] backend → tmux ft-backend on :${BACKEND_PORT}"
tmux kill-session -t ft-backend 2>/dev/null || true
sleep 1
tmux new-session -d -s ft-backend \
  "cd '$ROOT' && .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port ${BACKEND_PORT} 2>&1 | tee /tmp/ft-backend.log"

echo "[start] frontend → tmux ft-frontend on :${FRONTEND_PORT}"
tmux kill-session -t ft-frontend 2>/dev/null || true
sleep 1
tmux new-session -d -s ft-frontend \
  "cd '$ROOT/frontend' && npx next start -H 0.0.0.0 -p ${FRONTEND_PORT} 2>&1 | tee /tmp/ft-frontend.log"

sleep 6
echo "[start] backend health: $(curl -s -o /dev/null -w '%{http_code}' http://localhost:${BACKEND_PORT}/api/v1/health)"
echo "[start] frontend:       $(curl -s -o /dev/null -w '%{http_code}' http://localhost:${FRONTEND_PORT}/dashboard)"
echo "[start] tmux sessions:"
tmux ls 2>/dev/null | sed 's/^/    /'
