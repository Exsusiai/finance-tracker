"""SQLAlchemy ORM models mirroring docs/SCHEMA.sql."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
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
    __table_args__ = (
        Index("idx_accounts_active", "is_active", postgresql_where=None),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    institution: Mapped[str | None] = mapped_column(String(255))
    account_number: Mapped[str | None] = mapped_column(String(100))
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    initial_balance: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)
    updated_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str, onupdate=_utcnow_str)
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
    updated_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str, onupdate=_utcnow_str)

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
    updated_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str, onupdate=_utcnow_str)

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="pdf_import")

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','parsing','success','failed')",
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
    created_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)
    updated_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str, onupdate=_utcnow_str)
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
    updated_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str, onupdate=_utcnow_str)

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
    updated_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str, onupdate=_utcnow_str)

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
    created_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)
    updated_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str, onupdate=_utcnow_str)

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
    )
