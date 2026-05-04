"""Finance Tracker MCP Server — exposes finance-tracker backend to AI agents via MCP stdio.

Note: do NOT eagerly import `finance_mcp.server` here — when `run.sh` launches
`python -m finance_mcp.server`, this package is initialized first, which would
double-load the `server` module (RuntimeWarning).
"""
