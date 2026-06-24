"""broker_conn support traderepublic

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-23

Extends ``broker_connections`` to support Trade Republic alongside IBKR:

1. ``provider`` CHECK accepts ``'traderepublic'`` (was ibkr-only).
2. ``query_id`` becomes nullable and its non-empty CHECK is dropped — Trade
   Republic has no Flex-query concept; for TR the column stays NULL and the
   session cookies live in ``token_enc``.

SQLite can't ALTER a CHECK in place, so we recreate the table via
``batch_alter_table``. Idempotent: skips if the provider CHECK already
permits 'traderepublic'.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
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
    if "traderepublic" in sql:
        return  # already migrated

    # batch_alter_table recreates the table via rename → fires view validation.
    op.execute("DROP VIEW IF EXISTS v_account_balance")
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_broker_connections")

    with op.batch_alter_table("broker_connections") as batch:
        batch.drop_constraint("ck_broker_conn_provider", type_="check")
        batch.create_check_constraint(
            "ck_broker_conn_provider",
            "provider IN ('ibkr','traderepublic')",
        )
        # query_id: drop non-empty CHECK + make nullable.
        try:
            batch.drop_constraint("ck_broker_conn_query_id_nonempty", type_="check")
        except Exception:
            pass
        batch.alter_column("query_id", existing_type=sa.String(length=64), nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "broker_connections" not in inspector.get_table_names():
        return
    sql = _table_sql(bind, "broker_connections")
    if "traderepublic" not in sql:
        return

    op.execute("DROP VIEW IF EXISTS v_account_balance")
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_broker_connections")

    # Best-effort revert: TR rows would violate the narrowed CHECK, so clear them.
    op.execute("DELETE FROM broker_connections WHERE provider = 'traderepublic'")
    with op.batch_alter_table("broker_connections") as batch:
        batch.drop_constraint("ck_broker_conn_provider", type_="check")
        batch.create_check_constraint(
            "ck_broker_conn_provider",
            "provider IN ('ibkr')",
        )
        batch.create_check_constraint(
            "ck_broker_conn_query_id_nonempty",
            "length(query_id) > 0",
        )
        batch.alter_column("query_id", existing_type=sa.String(length=64), nullable=False)
