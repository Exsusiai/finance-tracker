"""Parity + smoke tests for the MCP read tools.

The read tools reuse the backend's async services / SQL fragments, so the
guarantee we assert is: **MCP tool output == backend service on the SAME DB**.
We seed a private in-memory DB, point the read tools at it, then compare. This
catches MCP-side serialization bugs and guards against the read path silently
drifting away from REST (the recurring V6/V7/V8 class of bug).

Isolation: we do NOT mutate ``os.environ`` or touch the backend's production
engine/settings singletons — instead we build our own engine and monkeypatch
the names the read tools resolve at call time. So these tests are safe to
collect alongside the backend suite (``pytest`` from the repo root) without
polluting it. Each test drives its own ``asyncio.run`` (no pytest-asyncio
config needed — mcp-server is a separate package from backend).
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "backend"))
sys.path.insert(0, str(_ROOT / "mcp-server" / "src"))

import finance_mcp.read_tools as read_tools  # noqa: E402
from app.db.session import Base  # noqa: E402
from app.models import Account, Category, Transaction  # noqa: E402
from finance_mcp.server import mcp  # noqa: E402

# ── Private engine; the read tools are pointed here via monkeypatch ──────────
_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
    connect_args={"check_same_thread": False},
)
_Session = async_sessionmaker(_engine, expire_on_commit=False)
_BASE = "EUR"  # EUR base → no FX needed in the seed


@asynccontextmanager
async def _patched_session():
    async with _Session() as db:
        yield db


# The tool functions look up ``session`` / ``base_currency`` / ``dec_str`` as
# globals of the read_tools module at CALL time, so patching the module
# namespace redirects the already-registered tools onto our engine.
read_tools.session = _patched_session
read_tools.base_currency = lambda override=None: (override or _BASE).upper()

_seeded = False


def _utcnow() -> str:
    return "2026-05-15T00:00:00Z"


async def _setup_db() -> None:
    """Create schema + balance view, then seed a deterministic dataset once."""
    global _seeded
    from app.main import _BALANCE_VIEW_SQL

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        exists = (await conn.execute(text(
            "SELECT 1 FROM sqlite_master WHERE type='view' AND name='v_account_balance'"
        ))).first()
        if not exists:
            await conn.execute(text(_BALANCE_VIEW_SQL))

    if _seeded:
        return
    async with _Session() as db:
        acc = Account(name="Bank", type="bank", currency="EUR",
                      initial_balance=Decimal("100"), is_active=True,
                      include_in_total=True, sort_order=0,
                      created_at=_utcnow(), updated_at=_utcnow())
        db.add(acc)
        await db.flush()
        cat = Category(name="Groceries", kind="expense", sort_order=0,
                       is_system=False, created_at=_utcnow())
        db.add(cat)
        await db.flush()
        db.add_all([
            Transaction(account_id=acc.id, occurred_at="2026-05-02T00:00:00Z",
                        amount=Decimal("1000"), currency="EUR", type="income",
                        source="pdf_import", is_pending=False,
                        created_at=_utcnow(), updated_at=_utcnow()),
            Transaction(account_id=acc.id, occurred_at="2026-05-10T00:00:00Z",
                        amount=Decimal("300"), currency="EUR", type="expense",
                        category_id=cat.id, description="Lidl run",
                        source="pdf_import", is_pending=False,
                        created_at=_utcnow(), updated_at=_utcnow()),
        ])
        await db.commit()
    _seeded = True


def _data(result) -> dict:
    """Unwrap FastMCP call_tool → the tool's structured return dict."""
    structured = result[1] if isinstance(result, tuple) else result
    assert structured.get("success") is True, structured
    return structured["data"]


# ─── Tests ───────────────────────────────────────────────────────────────────

def test_net_worth_matches_service():
    async def _run():
        await _setup_db()
        from app.services.valuation.net_worth import compute_net_worth

        mcp_data = _data(await mcp.call_tool("get_net_worth", {}))
        async with _Session() as db:
            svc = await compute_net_worth(db, _BASE)
        # Cash = 100 initial + 1000 income − 300 expense = 800; no investments.
        assert Decimal(mcp_data["cash_total"]) == Decimal("800")
        assert Decimal(mcp_data["net_worth"]) == svc.net_worth
        assert Decimal(mcp_data["investment_total"]) == svc.investment_total

    asyncio.run(_run())


def test_cashflow_seeded_numbers():
    async def _run():
        await _setup_db()
        d = _data(await mcp.call_tool(
            "get_cashflow", {"from_period": "2026-05", "to_period": "2026-05"}))
        month = d["months"][0]
        assert Decimal(month["income"]) == Decimal("1000")
        assert Decimal(month["expense"]) == Decimal("300")
        assert Decimal(month["savings"]) == Decimal("700")
        assert month["by_category"]["Groceries"] == "300"

    asyncio.run(_run())


def test_list_accounts_includes_balance():
    async def _run():
        await _setup_db()
        accts = _data(await mcp.call_tool("list_accounts", {}))
        assert len(accts) == 1
        assert accts[0]["name"] == "Bank"
        assert Decimal(accts[0]["balance"]) == Decimal("800")

    asyncio.run(_run())


def test_transactions_list_and_detail():
    async def _run():
        await _setup_db()
        lst = _data(await mcp.call_tool("list_transactions", {}))
        assert lst["count"] == 2
        expense = next(t for t in lst["transactions"] if t["type"] == "expense")
        assert expense["category_name"] == "Groceries"
        one = _data(await mcp.call_tool("get_transaction", {"transaction_id": expense["id"]}))
        assert one["id"] == expense["id"]
        assert one["description"] == "Lidl run"

    asyncio.run(_run())


def test_categories_tree():
    async def _run():
        await _setup_db()
        tree = _data(await mcp.call_tool("list_categories", {"tree": True}))
        names = {c["name"] for c in tree}
        assert "Groceries" in names

    asyncio.run(_run())


def test_by_category_range():
    async def _run():
        await _setup_db()
        d = _data(await mcp.call_tool(
            "get_cashflow_by_category", {"from_period": "2026-05", "to_period": "2026-05"}))
        groceries = next(c for c in d["categories"] if c["category_name"] == "Groceries")
        assert Decimal(groceries["total"]) == Decimal("300")

    asyncio.run(_run())
