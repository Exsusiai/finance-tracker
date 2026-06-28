#!/usr/bin/env bash
# Pull latest code from GitHub and bring the deployment up to date.
#
# Run on the server after pushing changes from your dev machine:
#   bash deploy/update.sh
#
# Steps: fast-forward pull → reinstall backend deps → apply DB migrations →
# reinstall + rebuild frontend → restart both tmux services.
# Untracked host files (data/, .env, frontend/.env.local, .mcp.json) are never
# touched by git pull.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "[update] git pull --ff-only origin master"
git pull --ff-only origin master

echo "[update] backend deps"
.venv/bin/pip install -q -e "backend/[dev]"

echo "[update] DB migrations (idempotent)"
( cd backend && "$ROOT/.venv/bin/alembic" upgrade head ) || echo "[update] alembic skipped/none"

echo "[update] frontend deps + build (NEXT_PUBLIC_API_URL from frontend/.env.local)"
( cd frontend && npm install --no-audit --no-fund && npm run build )

echo "[update] restart services"
bash deploy/start.sh

echo "[update] done."
