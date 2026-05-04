#!/usr/bin/env bash
# Start the Finance Tracker MCP server via stdio transport.
# Usage: ./run.sh
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PY="${PROJECT_ROOT}/.venv/bin/python"

# Ensure mcp SDK is installed in the venv (declared in mcp-server/pyproject but
# not auto-installed unless someone ran `pip install -e mcp-server/`).
if ! "${VENV_PY}" -c "import mcp" >/dev/null 2>&1; then
  echo "[finance-mcp] Installing mcp[cli] into venv..." >&2
  "${VENV_PY}" -m pip install --quiet "mcp[cli]>=1.0"
fi

export PYTHONPATH="${PROJECT_ROOT}/backend:${PROJECT_ROOT}/mcp-server/src:${PYTHONPATH:-}"

exec "${VENV_PY}" -m finance_mcp.server
