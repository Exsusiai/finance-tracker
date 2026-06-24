"""add_broker_connections

Revision ID: a1b2c3d4e5f6
Revises: 05f31889722c
Create Date: 2026-06-10

Schema addition for brokerage sync (IBKR Flex Web Service, P-broker):

``broker_connections`` mirrors ``exchange_connections`` for brokerage
read-only reporting credentials. The Flex Web Service token is encrypted
with ``FINANCE_BANK_ENCRYPTION_KEY``; the Flex Query ID is stored in
clear (not a secret).

``accounts.type`` already permits ``'brokerage'`` (added at baseline), so
no CHECK change is needed here.

Idempotent: dev DBs where the lifespan already created the table via
``Base.metadata.create_all()`` skip the create step.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "05f31889722c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_table(inspector, "broker_connections"):
        op.create_table(
            "broker_connections",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "account_id",
                sa.Integer(),
                sa.ForeignKey("accounts.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("provider", sa.String(length=50), nullable=False),
            sa.Column("token_enc", sa.Text(), nullable=False),
            sa.Column("query_id", sa.String(length=64), nullable=False),
            sa.Column("last_synced_at", sa.String(length=30)),
            sa.Column("last_sync_status", sa.String(length=20)),
            sa.Column("last_sync_error", sa.Text()),
            sa.Column("metadata_json", sa.Text()),
            sa.Column("created_at", sa.String(length=30), nullable=False),
            sa.Column("updated_at", sa.String(length=30), nullable=False),
            sa.CheckConstraint(
                "provider IN ('ibkr')",
                name="ck_broker_conn_provider",
            ),
            sa.CheckConstraint(
                "length(token_enc) > 0",
                name="ck_broker_conn_token_nonempty",
            ),
            sa.CheckConstraint(
                "length(query_id) > 0",
                name="ck_broker_conn_query_id_nonempty",
            ),
            sa.UniqueConstraint(
                "account_id",
                "provider",
                name="uq_broker_conn_account_provider",
            ),
        )
        op.create_index(
            "ix_broker_conn_account_id",
            "broker_connections",
            ["account_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, "broker_connections"):
        op.drop_index(
            "ix_broker_conn_account_id", table_name="broker_connections"
        )
        op.drop_table("broker_connections")
