"""add_portfolio_snapshots

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-27

Creates ``portfolio_snapshots`` — one row per month (``period`` "YYYY-MM",
unique) holding the forward-captured portfolio value (cash + investments +
net worth, in base currency). Powers the dashboard "组合市值走势" line.
History is not reconstructable, so the table fills going forward.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "portfolio_snapshots" in inspector.get_table_names():
        return
    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("base_currency", sa.String(length=10), nullable=False, server_default="CNY"),
        sa.Column("cash_total", sa.Numeric(20, 8), nullable=False, server_default="0"),
        sa.Column("investment_total", sa.Numeric(20, 8), nullable=False, server_default="0"),
        sa.Column("net_worth", sa.Numeric(20, 8), nullable=False, server_default="0"),
        sa.Column("captured_at", sa.String(length=30), nullable=False),
        sa.UniqueConstraint("period", name="uq_portfolio_snapshot_period"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "portfolio_snapshots" in inspector.get_table_names():
        op.drop_table("portfolio_snapshots")
