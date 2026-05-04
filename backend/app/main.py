"""Finance Tracker — FastAPI application entry point."""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timezone

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.db import Base, async_session_factory, engine
from app.api.v1 import api_router

settings = get_settings()

# ─── Logging ────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(message)s",
    stream=sys.stdout,
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
)
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        getattr(logging, settings.log_level.upper(), logging.INFO)
    ),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)


# ─── Lifespan ───────────────────────────────────────────────────────────────

_BALANCE_VIEW_DROP_SQL = "DROP VIEW IF EXISTS v_account_balance"
# `transactions.amount` is stored as a positive magnitude across PDF imports,
# but historically also accepted signed values for `manual` rows. We derive the
# direction from `type` so all writers behave consistently and the view stays
# correct regardless of sign convention.
_BALANCE_VIEW_SQL = """
CREATE VIEW v_account_balance AS
SELECT
    a.id              AS account_id,
    a.name            AS account_name,
    a.currency        AS currency,
    a.initial_balance + COALESCE(SUM(
        CASE t.type
            WHEN 'expense'    THEN -ABS(t.amount)
            WHEN 'income'     THEN  ABS(t.amount)
            WHEN 'transfer'   THEN -ABS(t.amount)  -- single-row outflow; reverse leg adds back
            WHEN 'adjustment' THEN  t.amount       -- adjustment carries its own sign
            ELSE 0
        END
    ), 0) AS balance
FROM accounts a
LEFT JOIN transactions t
    ON t.account_id = a.id
    AND t.deleted_at IS NULL
WHERE a.deleted_at IS NULL
GROUP BY a.id;
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    # Ensure data directories exist
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.pdf_storage_dir.mkdir(parents=True, exist_ok=True)
    settings.backup_dir.mkdir(parents=True, exist_ok=True)

    # Create all tables (dev mode — in prod, use Alembic migrations)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Create the balance view (drop first so updated definitions actually take effect)
    async with engine.begin() as conn:
        from sqlalchemy import text
        await conn.execute(text(_BALANCE_VIEW_DROP_SQL))
        await conn.execute(text(_BALANCE_VIEW_SQL))

    # Seed default expense categories + starter matching rules (idempotent)
    from app.services.categorizer.seed import seed_categories
    async with async_session_factory() as seed_db:
        await seed_categories(seed_db)
        await seed_db.commit()

    # Start background market-data scheduler
    from app.services.market_data.scheduler import start_scheduler, shutdown_scheduler
    start_scheduler()

    logger.info("finance_tracker_started", version="0.1.0")

    yield

    shutdown_scheduler()
    await engine.dispose()
    logger.info("finance_tracker_stopped")


# ─── App ────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Finance Tracker API",
    description="Personal finance & bookkeeping REST API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# CORS — allow all origins in local dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Error handlers
register_exception_handlers(app)


# ─── Unauthenticated public endpoints ──────────────────────────────────────

@app.get("/api/v1/health", tags=["System"])
async def health_check():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


@app.get("/api/v1/version", tags=["System"])
async def version_info():
    return {"version": "0.1.0", "name": "finance-tracker-backend"}


# ─── Authenticated API routes ─────────────────────────────────────────────

app.include_router(api_router, prefix="/api/v1")


# ─── CLI ────────────────────────────────────────────────────────────────────

def run_cli():
    """Entry point for `finance-tracker` CLI command."""
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.backend_host,
        port=settings.backend_port,
        reload=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run_cli()
