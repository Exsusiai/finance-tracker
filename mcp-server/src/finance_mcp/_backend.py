"""Async backend access for MCP **read** tools.

Read tools reuse the backend's async services and serialize helpers
(``compute_net_worth``, the cashflow-engine SQL fragments, ``_account_to_out``,
``_tx_to_out`` …) so MCP numbers match the REST / Web app **by construction** —
there is no hand-copied SQL to drift out of sync (the recurring V6/V7/V8 bug).

Write tools keep their separate sync ``sqlite3`` path (see ``server.py``); this
module is import-only for the read path.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

# ─── Inject backend into sys.path (mirrors server.py) ────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_BACKEND_DIR = _PROJECT_ROOT / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.db.session import async_session_factory, engine  # noqa: E402

settings = get_settings()

_schema_ready = False


async def _ensure_schema() -> None:
    """Idempotently ensure ``v_account_balance`` exists.

    The backend lifespan normally creates the view; this lets MCP run standalone
    (backend not booted) too. We only CREATE when missing — never DROP — so we
    don't yank a view the running backend may be mid-query on.
    """
    global _schema_ready
    if _schema_ready:
        return
    from sqlalchemy import text

    from app.main import _BALANCE_VIEW_SQL

    async with engine.begin() as conn:
        exists = (
            await conn.execute(
                text(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='view' AND name='v_account_balance'"
                )
            )
        ).first()
        if not exists:
            await conn.execute(text(_BALANCE_VIEW_SQL))
    _schema_ready = True


@asynccontextmanager
async def session():
    """Yield an async DB session (read path). Ensures schema on first use."""
    await _ensure_schema()
    async with async_session_factory() as db:
        yield db


def base_currency(override: str | None = None) -> str:
    """Resolve the target currency: explicit override or configured base."""
    return (override or settings.base_currency).upper()


def dec_str(val) -> str:
    """Normalize Decimal/number to a clean string (no scientific notation)."""
    if val is None:
        return "0"
    d = val if isinstance(val, Decimal) else Decimal(str(val))
    n = d.normalize()
    _, _, exponent = n.as_tuple()
    return str(int(n)) if exponent >= 0 else format(n, "f")
