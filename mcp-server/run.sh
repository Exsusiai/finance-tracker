#!/usr/bin/env bash
# Start the Finance Tracker MCP server via stdio transport.
# Usage: ./run.sh
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${PROJECT_ROOT}/backend:${PROJECT_ROOT}/mcp-server/src:${PYTHONPATH:-}"

exec "${PROJECT_ROOT}/.venv/bin/python" -m finance_mcp.server
