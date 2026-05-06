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

# Idempotent index DDL — applied in lifespan; also imported by tests.
# The partial unique index on (account_id, external_id) cannot be expressed
# cleanly as a SQLAlchemy UniqueConstraint, so it lives here as raw DDL only.
_index_migrations: list[tuple[str, str]] = [
    (
        "ix_transactions_account_id_occurred_at",
        "CREATE INDEX IF NOT EXISTS ix_transactions_account_id_occurred_at "
        "ON transactions (account_id, occurred_at)",
    ),
    (
        "ix_transactions_category_id",
        "CREATE INDEX IF NOT EXISTS ix_transactions_category_id "
        "ON transactions (category_id)",
    ),
    (
        "ix_transactions_pdf_import_id",
        "CREATE INDEX IF NOT EXISTS ix_transactions_pdf_import_id "
        "ON transactions (pdf_import_id)",
    ),
    (
        "uq_transactions_external_id_per_account",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_transactions_external_id_per_account "
        "ON transactions (account_id, external_id) "
        "WHERE deleted_at IS NULL AND external_id IS NOT NULL",
    ),
]
# `transactions.amount` is stored as a positive magnitude across PDF imports,
# but historically also accepted signed values for `manual` rows. We derive the
# direction from `type` so all writers behave consistently and the view stays
# correct regardless of sign convention.
#
# Special case for sub-account transfers (e.g. N26 main → N26 Saving Space):
# the user does NOT want sub-accounts as separate entities, so an in-bank move
# should leave the bank's overall balance unchanged. We tag those rows with
# `metadata_json LIKE %"subaccount":true%` and the view skips them.
# Sprint 4 FIX-23 (review V3 §V3-P1-6): wrap every metadata_json read in
# `json_valid()` so a single malformed row can't take down the whole balance
# view. SQLite's `json_extract` raises "malformed JSON" on bad input, which
# would surface as a 500 to every account-balance query in the system.
_BALANCE_VIEW_SQL = """
CREATE VIEW v_account_balance AS
SELECT
    a.id              AS account_id,
    a.name            AS account_name,
    a.currency        AS currency,
    a.initial_balance + COALESCE(SUM(
        CASE
            -- In-bank sub-account moves: ignore (money stays inside the bank)
            WHEN json_valid(t.metadata_json)
                 AND json_extract(t.metadata_json, '$.subaccount') = 1 THEN 0
            -- Cross-account transfer with explicit direction tag
            WHEN t.type = 'transfer'
                 AND json_valid(t.metadata_json)
                 AND json_extract(t.metadata_json, '$.transfer_direction') = 'in'
                 THEN  ABS(t.amount)
            WHEN t.type = 'transfer'
                 AND json_valid(t.metadata_json)
                 AND json_extract(t.metadata_json, '$.transfer_direction') = 'out'
                 THEN -ABS(t.amount)
            -- Untagged transfer (one-sided / unmatched / malformed metadata):
            -- default to outflow
            WHEN t.type = 'transfer'   THEN -ABS(t.amount)
            WHEN t.type = 'expense'    THEN -ABS(t.amount)
            WHEN t.type = 'income'     THEN  ABS(t.amount)
            WHEN t.type = 'adjustment' THEN  t.amount
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
    # Sprint 2 FIX-9 (review §P0-2): refuse to start if auth is disabled while
    # bound to a non-loopback interface. AUTH_DISABLED is intended *only* for
    # local single-user dev; combining it with 0.0.0.0 / public IPs exposes
    # all account / transaction / PDF routes to the LAN.
    if settings.auth_disabled and not settings.host_is_loopback:
        raise RuntimeError(
            "Refusing to start: AUTH_DISABLED=true is only allowed when "
            f"BACKEND_HOST is loopback (127.0.0.1 / ::1 / localhost). "
            f"Current host = {settings.backend_host!r}. Set "
            "AUTH_DISABLED=false or bind to localhost."
        )

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

    # Lightweight in-place schema migrations (until Alembic is wired up — P2-4).
    # Each entry: (table, column, sql_to_add). Idempotent.
    _column_migrations = [
        ("transactions", "user_note", "ALTER TABLE transactions ADD COLUMN user_note TEXT"),
        ("accounts", "iban", "ALTER TABLE accounts ADD COLUMN iban TEXT"),
    ]
    async with engine.begin() as conn:
        from sqlalchemy import text
        for table, column, ddl in _column_migrations:
            existing_cols = [
                row[1] for row in (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()
            ]
            if column not in existing_cols:
                await conn.execute(text(ddl))
                logger.info("schema_column_added", table=table, column=column)

    # Idempotent index creation (until Alembic is wired up — P2-4).
    async with engine.begin() as conn:
        from sqlalchemy import text
        for name, ddl in _index_migrations:
            try:
                await conn.execute(text(ddl))
            except Exception as e:
                logger.warning("schema_index_create_failed", name=name, error=str(e))

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

# Sprint 2 FIX-9 (review §P0-2): scope CORS to an explicit allow-list instead
# of the previous `*` (which combined with allow_credentials=True is also
# spec-invalid in browsers). Override via ALLOWED_ORIGINS env var.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
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
