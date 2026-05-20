"""asset_identity_chain_contract

Revision ID: 05f31889722c
Revises: b5f0a2f546ed
Create Date: 2026-05-20

P1-4-v2 / V5-P1-1 schema: split crypto Asset identity by (chain, contract).

Before this revision, `Asset` was unique on (asset_class, symbol). That
silently merged USDT-on-Ethereum and USDT-on-Arbitrum into ONE row —
the first contract to write its price would set the value for ALL
holdings sharing that symbol, regardless of chain.

This migration adds two NOT NULL columns (`chain`, `contract`) with
empty-string default + an `is_active` soft-archive flag, then swaps
the unique constraint to (asset_class, symbol, chain, contract).

The data migration that splits existing rows lives in
`backend/scripts/migrate_crypto_asset_identity.py` (A3) — this
migration only handles the schema shape.

All operations are idempotent: dev DBs where the lifespan already
created the new columns via Base.metadata.create_all() skip the alter
steps and only patch the unique key if needed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "05f31889722c"
down_revision: Union[str, Sequence[str], None] = "b5f0a2f546ed"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names(inspector, table: str) -> set[str]:
    return {c["name"] for c in inspector.get_columns(table)}


def _unique_names(inspector, table: str) -> set[str]:
    return {uc["name"] for uc in inspector.get_unique_constraints(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # SQLite batch_alter_table recreates the table via rename. Views that
    # reference `assets` would block the rename (see ERR-20260518-001
    # for the same trap with v_account_balance + accounts). `assets` isn't
    # referenced by any view today, but drop pre-emptively as defence.
    op.execute("DROP VIEW IF EXISTS v_account_balance")
    # SQLite doesn't fully roll back DDL — clean shadow tables from any
    # previous half-failed run.
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_assets")

    cols = _column_names(inspector, "assets")
    uniques = _unique_names(inspector, "assets")
    needs_chain = "chain" not in cols
    needs_contract = "contract" not in cols
    needs_is_active = "is_active" not in cols
    needs_uq_swap = (
        "uq_asset_symbol_class" in uniques
        or "uq_asset_identity" not in uniques
    )

    if (
        needs_chain
        or needs_contract
        or needs_is_active
        or needs_uq_swap
    ):
        with op.batch_alter_table("assets") as batch:
            if needs_chain:
                batch.add_column(
                    sa.Column(
                        "chain",
                        sa.String(length=50),
                        nullable=False,
                        server_default="",
                    )
                )
            if needs_contract:
                batch.add_column(
                    sa.Column(
                        "contract",
                        sa.String(length=128),
                        nullable=False,
                        server_default="",
                    )
                )
            if needs_is_active:
                batch.add_column(
                    sa.Column(
                        "is_active",
                        sa.Boolean(),
                        nullable=False,
                        server_default=sa.text("1"),
                    )
                )
            if "uq_asset_symbol_class" in uniques:
                batch.drop_constraint(
                    "uq_asset_symbol_class", type_="unique"
                )
            if "uq_asset_identity" not in uniques:
                batch.create_unique_constraint(
                    "uq_asset_identity",
                    ["asset_class", "symbol", "chain", "contract"],
                )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = _column_names(inspector, "assets")
    uniques = _unique_names(inspector, "assets")

    if (
        "chain" in cols
        or "contract" in cols
        or "is_active" in cols
        or "uq_asset_identity" in uniques
    ):
        op.execute("DROP VIEW IF EXISTS v_account_balance")
        op.execute("DROP TABLE IF EXISTS _alembic_tmp_assets")

        with op.batch_alter_table("assets") as batch:
            if "uq_asset_identity" in uniques:
                batch.drop_constraint("uq_asset_identity", type_="unique")
            if "uq_asset_symbol_class" not in uniques:
                # Restore the legacy uniqueness. This will fail if the
                # production data has multiple rows sharing
                # (symbol, asset_class) — that's by design: the user
                # must consciously resolve the split before downgrading.
                batch.create_unique_constraint(
                    "uq_asset_symbol_class", ["symbol", "asset_class"]
                )
            if "is_active" in cols:
                batch.drop_column("is_active")
            if "contract" in cols:
                batch.drop_column("contract")
            if "chain" in cols:
                batch.drop_column("chain")
