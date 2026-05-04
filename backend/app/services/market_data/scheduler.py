"""Market-data background scheduler (APScheduler).

Each refresh job opens its own AsyncSession and commits after writing — jobs
run independently of any FastAPI request lifecycle. Intervals are sourced from
`Settings.market_refresh_*_sec`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import get_settings
from app.db.session import async_session_factory
from app.services.market_data.engine import (
    refresh_crypto_prices,
    refresh_fx,
    refresh_stock_prices,
)

logger = structlog.get_logger(__name__)
_settings = get_settings()

# Module-level singleton so /system/scheduler/status endpoints can introspect
_scheduler: AsyncIOScheduler | None = None
_last_run: dict[str, dict] = {}


async def _run_with_session(name: str, fn) -> None:
    """Run a refresh function with a fresh AsyncSession + commit, log outcome."""
    async with async_session_factory() as db:
        try:
            result = await fn(db)
            await db.commit()
        except Exception as e:
            await db.rollback()
            logger.warning("scheduler_job_failed", job=name, error=str(e))
            _last_run[name] = {
                "ran_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": "error",
                "error": str(e),
                "result": None,
            }
            return
        logger.info("scheduler_job_ok", job=name, **{k: v for k, v in result.items() if k != "errors"})
        _last_run[name] = {
            "ran_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "ok" if not result.get("errors") else "partial",
            "error": None,
            "result": result,
        }


async def _job_crypto():
    await _run_with_session("crypto", refresh_crypto_prices)


async def _job_stocks():
    await _run_with_session("stocks", refresh_stock_prices)


async def _job_fx():
    await _run_with_session("fx", refresh_fx)


def start_scheduler() -> AsyncIOScheduler:
    """Boot the scheduler with three jobs (crypto / stocks / fx). Idempotent."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    sched = AsyncIOScheduler(timezone="UTC")

    # First run is delayed so it doesn't block startup. Subsequent runs at configured interval.
    boot_delay = datetime.now(timezone.utc) + timedelta(seconds=15)

    sched.add_job(
        _job_crypto,
        trigger="interval",
        seconds=max(60, _settings.market_refresh_crypto_sec),
        next_run_time=boot_delay,
        id="market_refresh_crypto",
        replace_existing=True,
    )
    sched.add_job(
        _job_stocks,
        trigger="interval",
        seconds=max(60, _settings.market_refresh_stock_sec),
        next_run_time=boot_delay,
        id="market_refresh_stocks",
        replace_existing=True,
    )
    sched.add_job(
        _job_fx,
        trigger="interval",
        seconds=max(60, _settings.market_refresh_fx_sec),
        next_run_time=boot_delay,
        id="market_refresh_fx",
        replace_existing=True,
    )

    sched.start()
    _scheduler = sched
    logger.info(
        "scheduler_started",
        crypto_sec=_settings.market_refresh_crypto_sec,
        stock_sec=_settings.market_refresh_stock_sec,
        fx_sec=_settings.market_refresh_fx_sec,
    )
    return sched


def shutdown_scheduler() -> None:
    """Stop the scheduler if running."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("scheduler_stopped")


def scheduler_status() -> dict:
    """Snapshot of registered jobs + their last run outcome."""
    if _scheduler is None:
        return {"running": False, "jobs": []}
    jobs = []
    for j in _scheduler.get_jobs():
        jobs.append({
            "id": j.id,
            "next_run_time": j.next_run_time.strftime("%Y-%m-%dT%H:%M:%SZ") if j.next_run_time else None,
            "interval_sec": j.trigger.interval.total_seconds() if hasattr(j.trigger, "interval") else None,
            "last_run": _last_run.get(j.id.removeprefix("market_refresh_")),
        })
    return {"running": True, "jobs": jobs}
