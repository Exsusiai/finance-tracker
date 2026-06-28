#!/usr/bin/env bash
# Stop the Finance Tracker tmux sessions (backend + frontend).
set -uo pipefail
tmux kill-session -t ft-backend 2>/dev/null && echo "[stop] ft-backend killed" || echo "[stop] ft-backend not running"
tmux kill-session -t ft-frontend 2>/dev/null && echo "[stop] ft-frontend killed" || echo "[stop] ft-frontend not running"
