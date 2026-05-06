"""Valuation service — placeholder.

The previous ``compute_holding_value`` helper here was buggy (review V1 §P2-5:
inverted FX direction) and unreferenced — the real portfolio aggregation lives
in ``backend/app/api/v1/holdings.py`` and ``mcp-server/src/finance_mcp/server.py``,
both of which use ``_convert_to_base`` with proper direct → inverse → triangulate
fall-back.

If you find yourself reaching for a helper here, extract the logic from one of
those callers rather than re-implementing.

Sprint 2 FIX-12 (review V1 §P2-5): dead code removed.
"""

from __future__ import annotations

# Intentionally empty.
