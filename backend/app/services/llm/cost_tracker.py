"""Monthly LLM cost tracking + budget gating.

Costs are accumulated in `app_settings` under per-month keys
`llm_monthly_cost_usd_YYYY_MM`. `check_budget()` returns False when the
current month is over budget; `record_cost()` UPSERTs the running total.

The KV pattern keeps things simple — no extra table. Reset is automatic
because each month gets a fresh key.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import app_settings as app_settings_svc

logger = structlog.get_logger(__name__)


def _current_month_key() -> str:
    now = datetime.now(timezone.utc)
    return f"llm_monthly_cost_usd_{now.year:04d}_{now.month:02d}"


async def get_current_cost(db: AsyncSession) -> float:
    raw = await app_settings_svc.get_setting(db, _current_month_key(), "0")
    try:
        return float(raw or "0")
    except ValueError:
        return 0.0


async def record_cost(db: AsyncSession, delta_usd: float) -> float:
    """Add `delta_usd` to the running monthly total. Returns new total."""
    if delta_usd < 0:
        return await get_current_cost(db)
    current = await get_current_cost(db)
    # Use Decimal for the addition to avoid float-drift on small deltas
    new_total = float(Decimal(str(current)) + Decimal(str(delta_usd)))
    await app_settings_svc.set_setting(db, _current_month_key(), f"{new_total:.6f}")
    return new_total


async def check_budget(db: AsyncSession) -> tuple[bool, float, float]:
    """Return (within_budget, used_usd, budget_usd)."""
    settings = await app_settings_svc.get_llm_settings(db)
    used = await get_current_cost(db)
    return used < settings.monthly_usd_budget, used, settings.monthly_usd_budget
