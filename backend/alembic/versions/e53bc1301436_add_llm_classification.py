"""add_llm_classification

Revision ID: e53bc1301436
Revises: 1ed07e31cab5
Create Date: 2026-05-08 16:46:28.058430

Schema additions for the LLM-fallback classification system (P1-1):

1. categorization_rules.requires_llm — when True the rule "matches" but
   the ingestion pipeline must still consult the LLM (PayPal-style
   composite rules where simple keywords are insufficient).
2. transactions.{categorization_method,categorization_confidence,llm_reason}
   — audit columns set by the classifier (rule / llm / manual).
3. categorization_notes — user-maintained knowledge base injected into
   LLM prompts as few-shot examples.
4. app_settings — KV table for runtime-tunable LLM config (provider,
   model, budget, threshold, grounding toggle, monthly cost counter).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e53bc1301436"
down_revision: Union[str, Sequence[str], None] = "1ed07e31cab5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ─── 1. categorization_rules.requires_llm ──────────────────────────
    rule_cols = {c["name"] for c in inspector.get_columns("categorization_rules")}
    if "requires_llm" not in rule_cols:
        with op.batch_alter_table("categorization_rules") as batch:
            batch.add_column(
                sa.Column(
                    "requires_llm",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
            )
    rule_indexes = {ix["name"] for ix in inspector.get_indexes("categorization_rules")}
    if "ix_rules_requires_llm" not in rule_indexes:
        op.create_index(
            "ix_rules_requires_llm",
            "categorization_rules",
            ["requires_llm"],
        )

    # ─── 2. transactions audit columns ─────────────────────────────────
    tx_cols = {c["name"] for c in inspector.get_columns("transactions")}
    with op.batch_alter_table("transactions") as batch:
        if "categorization_method" not in tx_cols:
            batch.add_column(sa.Column("categorization_method", sa.String(20), nullable=True))
        if "categorization_confidence" not in tx_cols:
            batch.add_column(sa.Column("categorization_confidence", sa.Float(), nullable=True))
        if "llm_reason" not in tx_cols:
            batch.add_column(sa.Column("llm_reason", sa.Text(), nullable=True))

    # ─── 3. categorization_notes ───────────────────────────────────────
    if not inspector.has_table("categorization_notes"):
        op.create_table(
            "categorization_notes",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column(
                "category_id",
                sa.Integer,
                sa.ForeignKey("categories.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("trigger_text", sa.Text, nullable=False),
            sa.Column("note_text", sa.Text, nullable=False),
            sa.Column(
                "source_transaction_id",
                sa.Integer,
                sa.ForeignKey("transactions.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("usage_count", sa.Integer, nullable=False, server_default=sa.text("0")),
            sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.String(30), nullable=False),
            sa.Column("updated_at", sa.String(30), nullable=False),
        )
        op.create_index("ix_notes_category", "categorization_notes", ["category_id"])
        op.create_index("ix_notes_enabled", "categorization_notes", ["enabled"])

    # ─── 4. app_settings (KV) ──────────────────────────────────────────
    if not inspector.has_table("app_settings"):
        op.create_table(
            "app_settings",
            sa.Column("key", sa.String(100), primary_key=True),
            sa.Column("value", sa.Text, nullable=False),
            sa.Column("updated_at", sa.String(30), nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("app_settings"):
        op.drop_table("app_settings")

    if inspector.has_table("categorization_notes"):
        op.drop_index("ix_notes_enabled", table_name="categorization_notes")
        op.drop_index("ix_notes_category", table_name="categorization_notes")
        op.drop_table("categorization_notes")

    tx_cols = {c["name"] for c in inspector.get_columns("transactions")}
    with op.batch_alter_table("transactions") as batch:
        if "llm_reason" in tx_cols:
            batch.drop_column("llm_reason")
        if "categorization_confidence" in tx_cols:
            batch.drop_column("categorization_confidence")
        if "categorization_method" in tx_cols:
            batch.drop_column("categorization_method")

    rule_indexes = {ix["name"] for ix in inspector.get_indexes("categorization_rules")}
    if "ix_rules_requires_llm" in rule_indexes:
        op.drop_index("ix_rules_requires_llm", table_name="categorization_rules")

    rule_cols = {c["name"] for c in inspector.get_columns("categorization_rules")}
    with op.batch_alter_table("categorization_rules") as batch:
        if "requires_llm" in rule_cols:
            batch.drop_column("requires_llm")
