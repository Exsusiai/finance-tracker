"""SQLAlchemy ORM models mirroring docs/SCHEMA.sql."""

from __future__ import annotations

# Import all models so Alembic / Base.metadata.create_all discovers them
from app.models.bank_connection import BankConnection

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def touch_updated_at(instance) -> None:
    """Manually set updated_at on a model instance (required for async SQLAlchemy)."""
    if hasattr(instance, "updated_at"):
        instance.updated_at = _utcnow_str()


# ─── Account ────────────────────────────────────────────────────────────────

class AccountType(StrEnum):
    bank = "bank"
    credit_card = "credit_card"
    brokerage = "brokerage"
    crypto_wallet = "crypto_wallet"
    cash = "cash"
    other = "other"


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    institution: Mapped[str | None] = mapped_column(String(255))
    account_number: Mapped[str | None] = mapped_column(String(100))
    # Full IBAN (or first 8+ chars). Used by transfer_matcher to detect
    # internal cross-bank transfers when the description carries the
    # counter-party's IBAN — much more reliable than name matching since
    # counterparties on self-transfers are usually the user's own name.
    iban: Mapped[str | None] = mapped_column(String(34))
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    initial_balance: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)
    updated_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)
    deleted_at: Mapped[str | None] = mapped_column(String(30))

    transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="account",
        foreign_keys="[Transaction.account_id]",
    )
    holdings: Mapped[list["AssetHolding"]] = relationship(back_populates="account")

    __table_args__ = (
        CheckConstraint(
            "type IN ('bank','credit_card','brokerage','crypto_wallet','cash','other')",
            name="ck_account_type",
        ),
    )


# ─── Category ───────────────────────────────────────────────────────────────

class CategoryKind(StrEnum):
    expense = "expense"
    income = "income"
    transfer = "transfer"


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("categories.id", ondelete="SET NULL"))
    icon: Mapped[str | None] = mapped_column(String(50))
    color: Mapped[str | None] = mapped_column(String(10))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)
    updated_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)

    parent: Mapped["Category | None"] = relationship(remote_side="Category.id")
    children: Mapped[list["Category"]] = relationship(back_populates="parent")
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="category")

    __table_args__ = (
        CheckConstraint("kind IN ('expense','income','transfer')", name="ck_category_kind"),
        UniqueConstraint("name", "kind", "parent_id", name="uq_category_name_kind_parent"),
    )


# ─── PDF Import ─────────────────────────────────────────────────────────────

class PdfImportStatus(StrEnum):
    pending = "pending"
    parsing = "parsing"
    success = "success"
    failed = "failed"
    awaiting_account = "awaiting_account"  # parser succeeded but the user
                                            # needs to choose which account
                                            # this PDF belongs to before
                                            # transactions are inserted.


class PdfImport(Base):
    __tablename__ = "pdf_imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    detected_bank: Mapped[str | None] = mapped_column(String(50))
    parser_version: Mapped[str | None] = mapped_column(String(50))
    account_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="SET NULL"))
    statement_period: Mapped[str | None] = mapped_column(String(50))
    transactions_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    error_message: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)
    updated_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="pdf_import")

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','parsing','success','failed','awaiting_account')",
            name="ck_pdf_import_status",
        ),
    )


# ─── Transaction ────────────────────────────────────────────────────────────

class TransactionType(StrEnum):
    expense = "expense"
    income = "income"
    transfer = "transfer"
    adjustment = "adjustment"


class TransactionSource(StrEnum):
    manual = "manual"
    pdf_import = "pdf_import"
    bank_api = "bank_api"
    mcp_agent = "mcp_agent"


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False)
    counter_account_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="SET NULL"))
    category_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("categories.id", ondelete="SET NULL"))
    occurred_at: Mapped[str] = mapped_column(String(30), nullable=False)
    posted_at: Mapped[str | None] = mapped_column(String(30))
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    fx_rate_to_base: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    base_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    raw_description: Mapped[str | None] = mapped_column(Text)
    counterparty: Mapped[str | None] = mapped_column(String(255))
    location: Mapped[str | None] = mapped_column(String(255))
    tags_json: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    pdf_import_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("pdf_imports.id", ondelete="SET NULL"))
    external_id: Mapped[str | None] = mapped_column(String(255))
    is_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    # User-authored note attached at inbox confirmation time. Persisted as a
    # classification "clue" — surfaced to the LLM-fallback path (P1-1c) as
    # few-shot context for similar future transactions.
    user_note: Mapped[str | None] = mapped_column(Text)
    # Audit columns for the L1/L2 classification pipeline.
    # method ∈ {'rule','llm','manual'}; confidence is 0..1 only when method='llm'.
    categorization_method: Mapped[str | None] = mapped_column(String(20))
    categorization_confidence: Mapped[float | None] = mapped_column(Float())
    llm_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)
    updated_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)
    deleted_at: Mapped[str | None] = mapped_column(String(30))

    account: Mapped["Account"] = relationship(foreign_keys=[account_id], back_populates="transactions")
    counter_account: Mapped["Account | None"] = relationship(
        foreign_keys="[Transaction.counter_account_id]",
    )
    category: Mapped["Category | None"] = relationship(back_populates="transactions")
    pdf_import: Mapped["PdfImport | None"] = relationship(back_populates="transactions")

    __table_args__ = (
        CheckConstraint("type IN ('expense','income','transfer','adjustment')", name="ck_tx_type"),
        CheckConstraint(
            "source IN ('manual','pdf_import','bank_api','mcp_agent')",
            name="ck_tx_source",
        ),
        # Composite index for the most common filter combo (account + date range)
        Index("ix_transactions_account_id_occurred_at", "account_id", "occurred_at"),
        # Index for per-category cashflow aggregation
        Index("ix_transactions_category_id", "category_id"),
        # Index for upload preview / reparse / delete by PDF import
        Index("ix_transactions_pdf_import_id", "pdf_import_id"),
        # NOTE: the partial unique index on (account_id, external_id) WHERE
        # deleted_at IS NULL AND external_id IS NOT NULL cannot be expressed
        # cleanly as a SQLAlchemy UniqueConstraint — it is created at runtime
        # via idempotent DDL in app/main.py lifespan (_index_migrations).
    )


# ─── Asset ──────────────────────────────────────────────────────────────────

class AssetClass(StrEnum):
    cash = "cash"
    a_share = "a_share"
    eu_stock = "eu_stock"
    us_stock = "us_stock"
    crypto = "crypto"
    gold = "gold"
    bond = "bond"
    fund = "fund"
    other = "other"


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(20), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    market: Mapped[str | None] = mapped_column(String(50))
    data_source: Mapped[str | None] = mapped_column(String(50))
    data_source_id: Mapped[str | None] = mapped_column(String(100))
    decimals: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)
    updated_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)

    holdings: Mapped[list["AssetHolding"]] = relationship(back_populates="asset")
    prices: Mapped[list["MarketPrice"]] = relationship(back_populates="asset")

    __table_args__ = (
        CheckConstraint(
            "asset_class IN ('cash','a_share','eu_stock','us_stock','crypto','gold','bond','fund','other')",
            name="ck_asset_class",
        ),
        UniqueConstraint("symbol", "asset_class", name="uq_asset_symbol_class"),
    )


# ─── Asset Holding ──────────────────────────────────────────────────────────

class AssetHolding(Base):
    __tablename__ = "asset_holdings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    asset_id: Mapped[int] = mapped_column(Integer, ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    avg_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    cost_currency: Mapped[str | None] = mapped_column(String(10))
    last_synced_at: Mapped[str | None] = mapped_column(String(30))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)
    updated_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)

    account: Mapped["Account"] = relationship(back_populates="holdings")
    asset: Mapped["Asset"] = relationship(back_populates="holdings")

    __table_args__ = (
        UniqueConstraint("account_id", "asset_id", name="uq_holding_account_asset"),
    )


# ─── Market Price ───────────────────────────────────────────────────────────

class MarketPrice(Base):
    __tablename__ = "market_prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(Integer, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False)
    quoted_at: Mapped[str] = mapped_column(String(30), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    raw_payload: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)

    asset: Mapped["Asset"] = relationship(back_populates="prices")

    __table_args__ = (
        UniqueConstraint("asset_id", "source", "quoted_at", name="uq_price_asset_source_time"),
    )


# ─── FX Rate ────────────────────────────────────────────────────────────────

class FxRate(Base):
    __tablename__ = "fx_rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    base_currency: Mapped[str] = mapped_column(String(10), nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(10), nullable=False)
    quoted_at: Mapped[str] = mapped_column(String(30), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)

    __table_args__ = (
        UniqueConstraint("base_currency", "quote_currency", "source", "quoted_at", name="uq_fx_pair_source_time"),
    )


# ─── Cash Flow Snapshot ─────────────────────────────────────────────────────

class CashFlowSnapshot(Base):
    __tablename__ = "cash_flow_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    period_year: Mapped[int] = mapped_column(Integer, nullable=False)
    period_month: Mapped[int] = mapped_column(Integer, nullable=False)
    base_currency: Mapped[str] = mapped_column(String(10), nullable=False, default="CNY")
    income_total: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    expense_total: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    transfer_total: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    savings_total: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    other_total: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    by_category_json: Mapped[str | None] = mapped_column(Text)
    by_account_json: Mapped[str | None] = mapped_column(Text)
    computed_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)

    __table_args__ = (
        UniqueConstraint("period_year", "period_month", "base_currency", name="uq_cashflow_period"),
    )


# ─── Categorization Rule ────────────────────────────────────────────────────

class CategorizationRule(Base):
    __tablename__ = "categorization_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pattern: Mapped[str] = mapped_column(String(500), nullable=False)
    pattern_type: Mapped[str] = mapped_column(String(20), nullable=False, default="contains")
    field: Mapped[str] = mapped_column(String(50), nullable=False, default="description")
    category_id: Mapped[int] = mapped_column(Integer, ForeignKey("categories.id", ondelete="CASCADE"), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # When True, a hit on this rule does NOT short-circuit auto-classification —
    # the ingestion pipeline routes the transaction to the LLM (L2) instead.
    # Set automatically when the user attaches a free-form note to a category
    # change, signalling that simple keyword equality is insufficient
    # (composite conditions like "PayPal AND amount=2.99" need an LLM).
    requires_llm: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    created_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)
    updated_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)

    category: Mapped["Category"] = relationship()

    __table_args__ = (
        CheckConstraint(
            "pattern_type IN ('contains','regex','exact','starts_with')",
            name="ck_rule_pattern_type",
        ),
        CheckConstraint(
            "field IN ('description','counterparty','raw_description')",
            name="ck_rule_field",
        ),
        Index("ix_rules_requires_llm", "requires_llm"),
    )


# ─── Categorization Note (knowledge base for LLM) ───────────────────────────

class CategorizationNote(Base):
    """User-maintained classification knowledge base.

    Each note is a natural-language hint ("PayPal 每月 2.99 EUR 是订阅 X")
    that gets injected into the LLM prompt as few-shot context. Notes are
    primarily created by the inbox-confirm flow when the user attaches a
    user_note to a manual classification, but can also be edited directly
    from the Settings → 知识库 UI.
    """

    __tablename__ = "categorization_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("categories.id", ondelete="CASCADE"), nullable=False
    )
    trigger_text: Mapped[str] = mapped_column(Text, nullable=False)
    note_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_transaction_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("transactions.id", ondelete="SET NULL")
    )
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)
    updated_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)

    category: Mapped["Category"] = relationship()
    source_transaction: Mapped["Transaction | None"] = relationship()

    __table_args__ = (
        Index("ix_notes_category", "category_id"),
        Index("ix_notes_enabled", "enabled"),
    )


# ─── App Settings (KV store) ────────────────────────────────────────────────

class AppSetting(Base):
    """Generic key-value runtime config.

    Used for LLM tunables (provider/model/budget/threshold/grounding) that
    we want to edit from the Settings UI without restarting the backend.
    Values are TEXT; callers cast on read. See `app.services.app_settings`
    for typed accessors.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)
