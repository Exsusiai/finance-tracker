"""broker_conn support trading212

Revision ID: c5d6e7f8a9b0
Revises: e5f6a7b8c9d0
Create Date: 2026-10-05

Extends the ``broker_connections`` provider CHECK to accept ``'trading212'``
(official public REST API, Basic-auth key pair stored as an encrypted
``"<api_key>:<api_secret>"`` blob in ``token_enc``; ``query_id`` stays NULL).

SQLite can't ALTER a CHECK in place, so the table is recreated via
``batch_alter_table``. Idempotent: skips if the CHECK already permits
'trading212'.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c5d6e7f8a9b0"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_sql(bind, name: str) -> str:
    return (
        bind.execute(
            sa.text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:n"),
            {"n": name},
        ).scalar()
        or ""
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "broker_connections" not in inspector.get_table_names():
        # Fresh DB where create_all already used the new model definition.
        return

    sql = _table_sql(bind, "broker_connections")
    if "trading212" in sql:
        return  # already migrated

    # batch_alter_table recreates the table via rename → fires view validation.
    op.execute("DROP VIEW IF EXISTS v_account_balance")
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_broker_connections")

    with op.batch_alter_table("broker_connections") as batch:
        batch.drop_constraint("ck_broker_conn_provider", type_="check")
        batch.create_check_constraint(
            "ck_broker_conn_provider",
            "provider IN ('ibkr','traderepublic','trading212')",
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "broker_connections" not in inspector.get_table_names():
        return
    sql = _table_sql(bind, "broker_connections")
    if "trading212" not in sql:
        return

    op.execute("DROP VIEW IF EXISTS v_account_balance")
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_broker_connections")

    # Best-effort revert: T212 rows would violate the narrowed CHECK → clear.
    op.execute("DELETE FROM broker_connections WHERE provider = 'trading212'")
    with op.batch_alter_table("broker_connections") as batch:
        batch.drop_constraint("ck_broker_conn_provider", type_="check")
        batch.create_check_constraint(
            "ck_broker_conn_provider",
            "provider IN ('ibkr','traderepublic')",
        )
