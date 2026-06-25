"""add_asset_holding_source

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-25

Adds ``asset_holdings.source`` VARCHAR(50) NOT NULL DEFAULT 'manual' (review
V7 §P1-9). It records which pipeline owns a holding ('manual' for hand-entered
rows, or a provider tag like 'ibkr' / 'traderepublic' for broker-synced rows).

Broker re-sync only zeroes holdings whose source matches the syncing provider,
so a user's manually-added position in a connected brokerage account is no
longer wiped. (The broker-connection API is one-connection-per-account, so a
single account never has two providers competing over the same asset.)

Existing rows default to 'manual'. The broker upsert reclaims previously-synced
rows (last_synced_at set + Asset.data_source == provider) back to the provider
source on the next sync, so historical broker holdings can again be zeroed when
sold (review V8-P1-1).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("asset_holdings")}
    if "source" not in cols:
        # Plain ADD COLUMN — SQLite supports NOT NULL with a constant default,
        # and asset_holdings is not referenced by any view, so no batch recreate.
        op.add_column(
            "asset_holdings",
            sa.Column(
                "source",
                sa.String(length=50),
                nullable=False,
                server_default="manual",
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("asset_holdings")}
    if "source" in cols:
        op.execute("DROP TABLE IF EXISTS _alembic_tmp_asset_holdings")
        with op.batch_alter_table("asset_holdings") as batch:
            batch.drop_column("source")
