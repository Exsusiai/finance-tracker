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

    # 2026-05-06: SQLite can't ALTER a CHECK constraint. When we add a new
    # allowed value to PdfImportStatus (e.g. 'awaiting_account'), the
    # existing table still carries the old CHECK and writes fail. Detect
    # the old constraint and rebuild the table in place. Idempotent.
    async with engine.begin() as conn:
        from sqlalchemy import text
        row = (await conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='pdf_imports'")
        )).first()
        existing_sql = (row[0] if row else "") or ""
        if "awaiting_account" not in existing_sql and "pdf_imports" in existing_sql:
            logger.info("schema_check_constraint_rebuild", table="pdf_imports")
            await conn.execute(text("""
                CREATE TABLE pdf_imports_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename VARCHAR(500) NOT NULL,
                    file_hash VARCHAR(64) NOT NULL UNIQUE,
                    file_size INTEGER NOT NULL,
                    storage_path VARCHAR(500) NOT NULL,
                    detected_bank VARCHAR(50),
                    parser_version VARCHAR(50),
                    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
                    statement_period VARCHAR(50),
                    transactions_count INTEGER NOT NULL DEFAULT 0,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    error_message TEXT,
                    raw_text TEXT,
                    metadata_json TEXT,
                    created_at VARCHAR(30) NOT NULL,
                    updated_at VARCHAR(30) NOT NULL,
                    CONSTRAINT ck_pdf_import_status
                        CHECK (status IN ('pending','parsing','success','failed','awaiting_account'))
                )
            """))
            await conn.execute(text("""
                INSERT INTO pdf_imports_new
                SELECT id, filename, file_hash, file_size, storage_path,
                       detected_bank, parser_version, account_id,
                       statement_period, transactions_count, status,
                       error_message, raw_text, metadata_json,
                       created_at, updated_at
                FROM pdf_imports
            """))
            await conn.execute(text("DROP TABLE pdf_imports"))
            await conn.execute(text("ALTER TABLE pdf_imports_new RENAME TO pdf_imports"))

    # Lightweight in-place schema migrations (until Alembic is wired up — P2-4).
    # Each entry: (table, column, sql_to_add). Idempotent.
    _column_migrations = [
        ("transactions", "user_note", "ALTER TABLE transactions ADD COLUMN user_note TEXT"),
        ("accounts", "iban", "ALTER TABLE accounts ADD COLUMN iban TEXT"),
    ]
    # Whitelist of identifiers that may appear in interpolated DDL.
    # SQLite has no parameter binding for PRAGMA / ALTER, so we vet the
    # value before substituting. Static today, the assert prevents a
    # future caller from feeding user input through this path.
    _ALLOWED_TABLES = {"transactions", "accounts"}
    async with engine.begin() as conn:
        from sqlalchemy import text
        for table, column, ddl in _column_migrations:
            assert table in _ALLOWED_TABLES, f"Unauthorised table in schema migration: {table}"
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

    # 2026-05-07 dedup (must run BEFORE seed_categories, because seed's
    # `_ensure_rule` calls scalar_one_or_none() and explodes on duplicates):
    # an earlier version of the 信用卡还款 → 跨行划转 merge redirected rules
    # without checking for collisions, which left e.g.
    # (pattern='amex', field='description', category_id=跨行划转) appearing
    # twice. Idempotent — no-op once duplicates are gone.
    async with engine.begin() as conn:
        from sqlalchemy import text
        dedup = await conn.execute(text("""
            DELETE FROM categorization_rules
            WHERE id NOT IN (
                SELECT MIN(id) FROM categorization_rules
                GROUP BY lower(pattern), field, category_id
            )
        """))
        if dedup.rowcount:
            logger.info("rules_deduplicated", count=dedup.rowcount)

    # Seed default expense categories + starter matching rules (idempotent)
    from app.services.categorizer.seed import seed_categories
    async with async_session_factory() as seed_db:
        await seed_categories(seed_db)
        await seed_db.commit()

    # 2026-05-06: backfill 内部储蓄 for orphan single-leg subaccount transfers.
    # The parser tags these with metadata.subaccount=true, but only the
    # matcher's `mark_subaccount_pair` writes the category — single-leg rows
    # (e.g. main-account PDF imported without the saving-space PDF) never had
    # one assigned. Without this, they end up uncategorised and the
    # legacy-transfer migration below pushes them back into the inbox.
    async with engine.begin() as conn:
        from sqlalchemy import text
        cat_row = (await conn.execute(text(
            "SELECT id FROM categories WHERE kind='transfer' AND name='内部储蓄' LIMIT 1"
        ))).first()
        if cat_row:
            backfill = await conn.execute(text(
                "UPDATE transactions SET category_id = :cid "
                "WHERE type = 'transfer' "
                "  AND category_id IS NULL "
                "  AND deleted_at IS NULL "
                "  AND metadata_json IS NOT NULL "
                "  AND json_valid(metadata_json) "
                "  AND json_extract(metadata_json, '$.subaccount') = 1"
            ), {"cid": cat_row[0]})
            if backfill.rowcount:
                logger.info("subaccount_orphans_categorized", count=backfill.rowcount)

    # 2026-05-07: collapse 信用卡还款 → 跨行划转. The distinction was brittle
    # (TF Bank's PDF omits incoming repayments, breaking pair detection) and
    # added cognitive load. Migrate every transaction + rule still pointing
    # at 信用卡还款 over to 跨行划转, then delete the legacy category.
    async with engine.begin() as conn:
        from sqlalchemy import text
        old = (await conn.execute(text(
            "SELECT id FROM categories WHERE kind='transfer' AND name='信用卡还款' LIMIT 1"
        ))).first()
        new = (await conn.execute(text(
            "SELECT id FROM categories WHERE kind='transfer' AND name='跨行划转' LIMIT 1"
        ))).first()
        if old and new:
            tx_n = await conn.execute(text(
                "UPDATE transactions SET category_id = :new "
                "WHERE category_id = :old AND deleted_at IS NULL"
            ), {"old": old[0], "new": new[0]})
            # Drop old-pointing rules whose pattern already has a counterpart
            # under the new category, then re-point what's left. This avoids
            # the duplicate (pattern, category_id) pairs that would otherwise
            # trip up `_ensure_rule`'s scalar_one_or_none on next startup.
            dropped_n = await conn.execute(text(
                "DELETE FROM categorization_rules "
                "WHERE category_id = :old "
                "  AND EXISTS ("
                "    SELECT 1 FROM categorization_rules r2 "
                "    WHERE r2.category_id = :new "
                "      AND lower(r2.pattern) = lower(categorization_rules.pattern) "
                "      AND r2.field = categorization_rules.field"
                "  )"
            ), {"old": old[0], "new": new[0]})
            redirected_n = await conn.execute(text(
                "UPDATE categorization_rules SET category_id = :new "
                "WHERE category_id = :old"
            ), {"old": old[0], "new": new[0]})
            await conn.execute(text(
                "DELETE FROM categories WHERE id = :old"
            ), {"old": old[0]})
            logger.info(
                "category_merged",
                src="信用卡还款",
                dst="跨行划转",
                tx_count=tx_n.rowcount,
                rules_dropped=dropped_n.rowcount,
                rules_redirected=redirected_n.rowcount,
            )

    # 2026-05-06: re-enqueue *truly* uncategorised transfers (no subaccount
    # tag, no pairing, no category) into the inbox so the user can pick a
    # transfer subtype. Idempotent — runs after the subaccount backfill above
    # so rows we just fixed don't get re-flagged.
    async with engine.begin() as conn:
        from sqlalchemy import text
        result = await conn.execute(text(
            "UPDATE transactions SET is_pending = 1 "
            "WHERE type = 'transfer' "
            "  AND category_id IS NULL "
            "  AND deleted_at IS NULL "
            "  AND is_pending = 0"
        ))
        if result.rowcount:
            logger.info("legacy_transfers_reenqueued", count=result.rowcount)

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
