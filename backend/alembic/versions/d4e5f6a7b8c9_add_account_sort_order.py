"""add_account_sort_order

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-27

Adds ``accounts.sort_order`` INTEGER NOT NULL DEFAULT 0 for manual
drag-to-reorder of the accounts list. Lower sorts first; ties broken by
id. Existing rows default to 0 so they keep creation order until the user
reorders.

Uses a plain ``ADD COLUMN`` (no table recreation), so it does NOT touch
``v_account_balance`` — unlike batch_alter_table (see ERR-20260518-001).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("accounts")}
    if "sort_order" not in cols:
        op.add_column(
            "accounts",
            sa.Column(
                "sort_order",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("accounts")}
    if "sort_order" in cols:
        op.execute("DROP VIEW IF EXISTS v_account_balance")
        op.execute("DROP TABLE IF EXISTS _alembic_tmp_accounts")
        with op.batch_alter_table("accounts") as batch:
            batch.drop_column("sort_order")
