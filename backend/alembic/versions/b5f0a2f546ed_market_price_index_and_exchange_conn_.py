"""market_price_index_and_exchange_conn_checks

Revision ID: b5f0a2f546ed
Revises: 7bc98bcff7fe
Create Date: 2026-05-19

Two unrelated quality improvements from the 2026-05-19 code review:

1. Index on ``market_prices(asset_id, currency, quoted_at)`` so the
   latest-price subquery (holdings_value._SQL + several spots in
   holdings.py) stops doing full-table scans (DB-C2 finding).

2. CHECK constraints on ``exchange_connections.api_key_enc`` /
   ``api_secret_enc`` blocking empty strings from sneaking past the
   NOT NULL when the encrypt step misbehaves (DB-H4 finding). An
   empty encrypted blob is always a bug; decrypt would fail at runtime.

Both are idempotent so re-applying on an already-migrated DB is safe.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b5f0a2f546ed"
down_revision: Union[str, Sequence[str], None] = "7bc98bcff7fe"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_INDEX_NAME = "ix_market_prices_asset_currency_quoted"


def _has_index(inspector, table: str, name: str) -> bool:
    return any(ix["name"] == name for ix in inspector.get_indexes(table))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1) Market-price index.
    if not _has_index(inspector, "market_prices", _INDEX_NAME):
        op.create_index(
            _INDEX_NAME,
            "market_prices",
            ["asset_id", "currency", "quoted_at"],
        )

    # 2) exchange_connections CHECK constraints. SQLite stores CHECK
    # inside the table definition → batch_alter_table is required.
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_exchange_connections")

    sql = (
        bind.execute(
            sa.text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='exchange_connections'"
            )
        ).scalar()
        or ""
    )
    needs_key_check = "ck_exchange_conn_api_key_nonempty" not in sql
    needs_secret_check = "ck_exchange_conn_api_secret_nonempty" not in sql

    if needs_key_check or needs_secret_check:
        with op.batch_alter_table("exchange_connections") as batch:
            if needs_key_check:
                batch.create_check_constraint(
                    "ck_exchange_conn_api_key_nonempty",
                    "length(api_key_enc) > 0",
                )
            if needs_secret_check:
                batch.create_check_constraint(
                    "ck_exchange_conn_api_secret_nonempty",
                    "length(api_secret_enc) > 0",
                )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_index(inspector, "market_prices", _INDEX_NAME):
        op.drop_index(_INDEX_NAME, table_name="market_prices")

    sql = (
        bind.execute(
            sa.text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='exchange_connections'"
            )
        ).scalar()
        or ""
    )
    if (
        "ck_exchange_conn_api_key_nonempty" in sql
        or "ck_exchange_conn_api_secret_nonempty" in sql
    ):
        op.execute("DROP TABLE IF EXISTS _alembic_tmp_exchange_connections")
        with op.batch_alter_table("exchange_connections") as batch:
            try:
                batch.drop_constraint(
                    "ck_exchange_conn_api_key_nonempty", type_="check"
                )
            except Exception:
                pass
            try:
                batch.drop_constraint(
                    "ck_exchange_conn_api_secret_nonempty", type_="check"
                )
            except Exception:
                pass
