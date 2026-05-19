"""add_crypto_wallet_and_exchange

Revision ID: 3317bd446ae0
Revises: e53bc1301436
Create Date: 2026-05-18

Schema additions for the P1-4 crypto-wallet / CEX sync feature:

1. accounts.type CHECK accepts new value 'exchange' (Binance / Bitget API
   accounts).
2. asset_holdings gets ``chain`` (NOT NULL, default '') and ``is_active``
   (NOT NULL, default true). The unique key is replaced from
   ``(account_id, asset_id)`` to ``(account_id, asset_id, chain)`` so the
   same token on different chains (USDT on ETH vs Arbitrum vs Tron) lives
   in distinct rows.
3. ``chain_addresses`` stores the (chain, address) pairs that belong to a
   ``crypto_wallet`` account.
4. ``exchange_connections`` mirrors ``bank_connections`` for CEX
   read-only API credentials (encrypted with FINANCE_BANK_ENCRYPTION_KEY).

All operations are idempotent: dev DBs where the lifespan already created
the new tables via ``Base.metadata.create_all()`` will skip the create
steps and only patch the in-place ALTERs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "3317bd446ae0"
down_revision: Union[str, Sequence[str], None] = "e53bc1301436"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def _column_names(inspector, table: str) -> set[str]:
    return {c["name"] for c in inspector.get_columns(table)}


def _unique_names(inspector, table: str) -> set[str]:
    return {uc["name"] for uc in inspector.get_unique_constraints(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # SQLite batch_alter_table recreates the table via rename, which fires
    # view validation on `v_account_balance`. Drop it first; the lifespan
    # in `app.main` recreates it at next backend start (see
    # `_BALANCE_VIEW_SQL` there). This keeps the migration deterministic.
    op.execute("DROP VIEW IF EXISTS v_account_balance")
    # SQLite doesn't fully roll back DDL — a prior failed batch_alter_table
    # can leave `_alembic_tmp_*` shadow tables. Clean them so a retry of
    # this migration is safe.
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_accounts")
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_asset_holdings")

    # ─── 1. accounts.type CHECK adds 'exchange' ───────────────────────
    # SQLite stores CHECK as part of the table definition. Use batch
    # mode to recreate the table with the new constraint. Skipped if
    # the constraint already permits 'exchange'.
    sql = (
        bind.execute(
            sa.text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='accounts'"
            )
        ).scalar()
        or ""
    )
    if "'exchange'" not in sql:
        with op.batch_alter_table(
            "accounts",
            table_kwargs={"sqlite_autoincrement": True},
        ) as batch:
            batch.drop_constraint("ck_account_type", type_="check")
            batch.create_check_constraint(
                "ck_account_type",
                "type IN ('bank','credit_card','brokerage','crypto_wallet',"
                "'exchange','cash','other')",
            )

    # ─── 2. asset_holdings: add columns + swap unique ────────────────
    ah_cols = _column_names(inspector, "asset_holdings")
    ah_uniques = _unique_names(inspector, "asset_holdings")
    needs_chain = "chain" not in ah_cols
    needs_active = "is_active" not in ah_cols
    needs_uq_swap = (
        "uq_holding_account_asset" in ah_uniques
        or "uq_holding_account_asset_chain" not in ah_uniques
    )

    if needs_chain or needs_active or needs_uq_swap:
        with op.batch_alter_table("asset_holdings") as batch:
            if needs_chain:
                batch.add_column(
                    sa.Column(
                        "chain",
                        sa.String(length=50),
                        nullable=False,
                        server_default="",
                    )
                )
            if needs_active:
                batch.add_column(
                    sa.Column(
                        "is_active",
                        sa.Boolean(),
                        nullable=False,
                        server_default=sa.text("1"),
                    )
                )
            if "uq_holding_account_asset" in ah_uniques:
                batch.drop_constraint(
                    "uq_holding_account_asset", type_="unique"
                )
            if "uq_holding_account_asset_chain" not in ah_uniques:
                batch.create_unique_constraint(
                    "uq_holding_account_asset_chain",
                    ["account_id", "asset_id", "chain"],
                )

    # ─── 3. chain_addresses ──────────────────────────────────────────
    if not _has_table(inspector, "chain_addresses"):
        op.create_table(
            "chain_addresses",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "account_id",
                sa.Integer(),
                sa.ForeignKey("accounts.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("chain", sa.String(length=50), nullable=False),
            sa.Column("address", sa.String(length=128), nullable=False),
            sa.Column("label", sa.String(length=255)),
            sa.Column("last_synced_at", sa.String(length=30)),
            sa.Column("last_sync_status", sa.String(length=20)),
            sa.Column("last_sync_error", sa.Text()),
            sa.Column("created_at", sa.String(length=30), nullable=False),
            sa.Column("updated_at", sa.String(length=30), nullable=False),
            sa.UniqueConstraint(
                "account_id",
                "chain",
                "address",
                name="uq_chain_address_account_chain_addr",
            ),
        )
        op.create_index(
            "ix_chain_addresses_account_id",
            "chain_addresses",
            ["account_id"],
        )

    # ─── 4. exchange_connections ─────────────────────────────────────
    if not _has_table(inspector, "exchange_connections"):
        op.create_table(
            "exchange_connections",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "account_id",
                sa.Integer(),
                sa.ForeignKey("accounts.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("exchange", sa.String(length=50), nullable=False),
            sa.Column("api_key_enc", sa.Text(), nullable=False),
            sa.Column("api_secret_enc", sa.Text(), nullable=False),
            sa.Column("api_passphrase_enc", sa.Text()),
            sa.Column("last_synced_at", sa.String(length=30)),
            sa.Column("last_sync_status", sa.String(length=20)),
            sa.Column("last_sync_error", sa.Text()),
            sa.Column("metadata_json", sa.Text()),
            sa.Column("created_at", sa.String(length=30), nullable=False),
            sa.Column("updated_at", sa.String(length=30), nullable=False),
            sa.CheckConstraint(
                "exchange IN ('binance','bitget')",
                name="ck_exchange_conn_exchange",
            ),
            sa.UniqueConstraint(
                "account_id",
                "exchange",
                name="uq_exchange_conn_account_exchange",
            ),
        )
        op.create_index(
            "ix_exchange_conn_account_id",
            "exchange_connections",
            ["account_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, "exchange_connections"):
        op.drop_index(
            "ix_exchange_conn_account_id", table_name="exchange_connections"
        )
        op.drop_table("exchange_connections")

    if _has_table(inspector, "chain_addresses"):
        op.drop_index(
            "ix_chain_addresses_account_id", table_name="chain_addresses"
        )
        op.drop_table("chain_addresses")

    ah_cols = _column_names(inspector, "asset_holdings")
    ah_uniques = _unique_names(inspector, "asset_holdings")
    if (
        "chain" in ah_cols
        or "is_active" in ah_cols
        or "uq_holding_account_asset_chain" in ah_uniques
    ):
        with op.batch_alter_table("asset_holdings") as batch:
            if "uq_holding_account_asset_chain" in ah_uniques:
                batch.drop_constraint(
                    "uq_holding_account_asset_chain", type_="unique"
                )
            if "uq_holding_account_asset" not in ah_uniques:
                batch.create_unique_constraint(
                    "uq_holding_account_asset", ["account_id", "asset_id"]
                )
            if "is_active" in ah_cols:
                batch.drop_column("is_active")
            if "chain" in ah_cols:
                batch.drop_column("chain")

    # Revert CHECK on accounts (remove 'exchange').
    sql = (
        bind.execute(
            sa.text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='accounts'"
            )
        ).scalar()
        or ""
    )
    if "'exchange'" in sql:
        with op.batch_alter_table(
            "accounts",
            table_kwargs={"sqlite_autoincrement": True},
        ) as batch:
            batch.drop_constraint("ck_account_type", type_="check")
            batch.create_check_constraint(
                "ck_account_type",
                "type IN ('bank','credit_card','brokerage','crypto_wallet',"
                "'cash','other')",
            )
