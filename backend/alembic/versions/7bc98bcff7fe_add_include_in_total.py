"""add_include_in_total

Revision ID: 7bc98bcff7fe
Revises: 3317bd446ae0
Create Date: 2026-05-18

Adds ``accounts.include_in_total`` BOOLEAN NOT NULL DEFAULT 1. Lets the
user opt individual accounts out of grand-total aggregation (net_worth
cash + investment) without hiding them from per-account views.

Existing rows default to True so total values don't change for anyone
who hasn't touched the new toggle.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7bc98bcff7fe"
down_revision: Union[str, Sequence[str], None] = "3317bd446ae0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # SQLite batch_alter_table recreates the accounts table, which would
    # fail view-validation against v_account_balance (see ERR-20260518-001).
    op.execute("DROP VIEW IF EXISTS v_account_balance")
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_accounts")

    cols = {c["name"] for c in inspector.get_columns("accounts")}
    if "include_in_total" not in cols:
        with op.batch_alter_table("accounts") as batch:
            batch.add_column(
                sa.Column(
                    "include_in_total",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("1"),
                )
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    cols = {c["name"] for c in inspector.get_columns("accounts")}
    if "include_in_total" in cols:
        op.execute("DROP VIEW IF EXISTS v_account_balance")
        op.execute("DROP TABLE IF EXISTS _alembic_tmp_accounts")
        with op.batch_alter_table("accounts") as batch:
            batch.drop_column("include_in_total")
