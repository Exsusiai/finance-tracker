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
    # P1-4: CEX (Binance / Bitget / …) with read-only API keys stored
    # encrypted in `exchange_connections`.
    exchange = "exchange"
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
    # Per-account opt-out from grand-total aggregation. When False the
    # account still appears in lists & per-account views but is dropped
    # from net_worth (cash + investment) and balance summaries. Used for
    # e.g. business / shared / experimental accounts the user doesn't
    # want to count toward their personal net worth.
    include_in_total: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Manual display order for the accounts list (drag-to-reorder in the UI).
    # Lower sorts first; ties broken by id. Defaults to 0 so legacy rows keep
    # creation order until the user reorders.
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
            "type IN ('bank','credit_card','brokerage','crypto_wallet','exchange','cash','other')",
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
    awaiting_review = "awaiting_review"     # parsed + previewed, NOT yet
                                            # inserted. The default landing
                                            # state for every upload: the user
                                            # reviews the preview then commits
                                            # (insert) or cancels (delete). No
                                            # transactions exist in the DB
                                            # until commit.


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
            "status IN ('pending','parsing','success','failed',"
            "'awaiting_account','awaiting_review')",
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
    # A-sprint (2026-05-20): crypto Asset identity used to be
    # (asset_class, symbol) which silently merged USDT-on-Ethereum and
    # USDT-on-Arbitrum into ONE row sharing ONE price — a contract on
    # one chain could poison the value of a different-chain holding
    # with the same ticker. Identity is now (asset_class, symbol,
    # chain, contract). Non-crypto rows (cash/stock/gold/...) keep
    # chain='' / contract=''; native chain coins (ETH/BTC/SOL) also
    # use '' since price is unified L1+L2. Only on-chain tokens carry
    # both chain and contract.
    chain: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    contract: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # Soft-archive flag for A3 migration: when we split a legacy shared
    # Asset row into chain-specific replacements, the original row is
    # left in place with is_active=False so historical market_prices /
    # any orphaned references survive for audit / rollback.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
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
        # Old `uq_asset_symbol_class` is gone — see chain/contract docstring
        # above. New unique allows same symbol across (chain, contract).
        UniqueConstraint(
            "asset_class", "symbol", "chain", "contract",
            name="uq_asset_identity",
        ),
    )


# ─── Asset Holding ──────────────────────────────────────────────────────────

class AssetHolding(Base):
    __tablename__ = "asset_holdings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    asset_id: Mapped[int] = mapped_column(Integer, ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False)
    # P1-4: chain that this holding sits on. Empty string for non-crypto
    # holdings (stocks, cash, gold, …) — kept NOT NULL so the
    # (account_id, asset_id, chain) unique works cleanly under SQLite NULL
    # semantics. For crypto: "ethereum" / "arbitrum" / "bitcoin" / etc.
    chain: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    avg_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    cost_currency: Mapped[str | None] = mapped_column(String(10))
    last_synced_at: Mapped[str | None] = mapped_column(String(30))
    # P1-4 sync semantics: re-sync writes `is_active` + `quantity` based on
    # whether the token still appeared in the latest fetch.
    #   - present this round  → is_active=True,  quantity=<fetched>
    #   - missing this round  → is_active=False, quantity=0
    # is_active is kept separate from quantity so the UI can hide stale
    # rows by default while still preserving the row identity (history
    # of last_synced_at, created_at, …).
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # V7-P1-9: which pipeline owns this holding — 'manual' for hand-entered
    # rows, or a provider tag ('ibkr' / 'traderepublic' / …) for broker-synced
    # rows. Broker re-sync only zeroes holdings with a matching source, so a
    # user's manually-added gold/private position in a connected brokerage
    # account is never wiped. NOTE (V8-P1-2): `source` is NOT part of the
    # holding unique key `(account_id, asset_id, chain)`, so it does not let two
    # providers hold the SAME asset in ONE account simultaneously — that's fine
    # because the broker-connection API is one-connection-per-account (get /
    # upsert / delete key by account_id alone, see api/v1/wallet_sync.py).
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="manual")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)
    updated_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)

    account: Mapped["Account"] = relationship(back_populates="holdings")
    asset: Mapped["Asset"] = relationship(back_populates="holdings")

    __table_args__ = (
        UniqueConstraint(
            "account_id", "asset_id", "chain",
            name="uq_holding_account_asset_chain",
        ),
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
        # Latest-price-per-asset lookup is the hottest read path
        # (holdings_value._SQL, holdings.py portfolio_summary, etc.).
        # The existing unique key has source between currency and time,
        # so it can't service "MAX(quoted_at) WHERE asset_id=? AND
        # currency='USDT'". This composite covers it.
        Index(
            "ix_market_prices_asset_currency_quoted",
            "asset_id", "currency", "quoted_at",
        ),
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


class PortfolioSnapshot(Base):
    """Weekly snapshot of portfolio market value (cash + investments).

    Historical portfolio value is NOT reconstructable — `asset_holdings`
    only stores CURRENT quantities (crypto/broker are snapshot-synced, no
    per-week position history). So we capture forward: a scheduler job
    upserts the current week's row (keyed by that week's Monday) with the
    latest valuation, and at week rollover a new row starts. Each week
    therefore holds its last-captured value. Powers the dashboard
    "组合市值走势" line.
    """

    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    period: Mapped[str] = mapped_column(String(10), nullable=False, unique=True)  # "YYYY-MM-DD" (week Monday)
    base_currency: Mapped[str] = mapped_column(String(10), nullable=False, default="CNY")
    cash_total: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    investment_total: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    net_worth: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    captured_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)


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


# ─── Chain Addresses (P1-4 crypto wallet) ──────────────────────────────────


class ChainAddress(Base):
    """One on-chain address that belongs to a `crypto_wallet` account.

    Each account can aggregate addresses across chains — e.g. one ETH +
    one BTC + one SOL address are all part of the same "wallet" entity.
    `(account_id, chain, address)` is unique. The same EVM address may
    legitimately appear on multiple chains (Ethereum + Arbitrum + Base
    share addresses) — each as its own row.
    """

    __tablename__ = "chain_addresses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    # Free-form chain id (e.g. "ethereum", "arbitrum", "bitcoin", "solana",
    # "tron"). Validated in service layer, not via DB CHECK, so adding a new
    # chain doesn't require a migration.
    chain: Mapped[str] = mapped_column(String(50), nullable=False)
    address: Mapped[str] = mapped_column(String(128), nullable=False)
    label: Mapped[str | None] = mapped_column(String(255))
    # Last successful sync (UTC ISO-8601). NULL = never synced.
    last_synced_at: Mapped[str | None] = mapped_column(String(30))
    last_sync_status: Mapped[str | None] = mapped_column(String(20))
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)
    updated_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)

    __table_args__ = (
        UniqueConstraint(
            "account_id", "chain", "address",
            name="uq_chain_address_account_chain_addr",
        ),
        Index("ix_chain_addresses_account_id", "account_id"),
    )


# ─── Exchange Connections (P1-4 CEX read-only API) ─────────────────────────


class ExchangeConnection(Base):
    """Read-only API credentials for a CEX account (Binance / Bitget).

    Mirrors the `bank_connections` pattern: AES-256-GCM encrypted blobs
    keyed by `FINANCE_BANK_ENCRYPTION_KEY`. `api_passphrase_enc` is kept
    nullable because Binance/Bitget don't use a passphrase, but OKX-style
    exchanges that we may add later do.
    """

    __tablename__ = "exchange_connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    exchange: Mapped[str] = mapped_column(String(50), nullable=False)
    api_key_enc: Mapped[str] = mapped_column(Text, nullable=False)
    api_secret_enc: Mapped[str] = mapped_column(Text, nullable=False)
    api_passphrase_enc: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[str | None] = mapped_column(String(30))
    last_sync_status: Mapped[str | None] = mapped_column(String(20))
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)
    updated_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)

    __table_args__ = (
        CheckConstraint(
            "exchange IN ('binance','bitget')",
            name="ck_exchange_conn_exchange",
        ),
        # Defence against empty-string sneaking past NOT NULL when the
        # encrypt step misbehaves (DB-H4 finding 2026-05-19). A 0-length
        # encrypted blob is always a bug — decrypt would fail at runtime.
        CheckConstraint(
            "length(api_key_enc) > 0",
            name="ck_exchange_conn_api_key_nonempty",
        ),
        CheckConstraint(
            "length(api_secret_enc) > 0",
            name="ck_exchange_conn_api_secret_nonempty",
        ),
        UniqueConstraint(
            "account_id", "exchange",
            name="uq_exchange_conn_account_exchange",
        ),
        Index("ix_exchange_conn_account_id", "account_id"),
    )


# ─── Broker Connections (brokerage Flex / API read-only) ───────────────────


class BrokerConnection(Base):
    """Read-only reporting credentials for a `brokerage` account.

    First provider is Interactive Brokers via the **Flex Web Service** — a
    token-based reporting API (NOT a trading API), available on every IBKR
    account type including Lite, with no Pro requirement. The token is
    AES-256-GCM encrypted with `FINANCE_BANK_ENCRYPTION_KEY` (same path as
    `exchange_connections` / `bank_connections`). The Flex *Query ID* is not
    a secret, so it's stored in clear for display.

    Mirrors `ExchangeConnection`: one connection per (account, provider),
    secrets write-only, sync state tracked inline.
    """

    __tablename__ = "broker_connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False, default="ibkr")
    # AES-256-GCM encrypted credential blob. Its shape depends on provider:
    #   - ibkr: the Flex Web Service token (a single string)
    #   - traderepublic: the serialized web-login session cookies (Netscape
    #     cookie-jar text) obtained after the 2-step 4-digit-code login
    token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    # Flex Query ID (not a secret) — identifies which configured query to run.
    # Only used by IBKR; nullable because Trade Republic has no query concept.
    query_id: Mapped[str | None] = mapped_column(String(64))
    last_synced_at: Mapped[str | None] = mapped_column(String(30))
    last_sync_status: Mapped[str | None] = mapped_column(String(20))
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)
    updated_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)

    __table_args__ = (
        CheckConstraint(
            "provider IN ('ibkr','traderepublic')",
            name="ck_broker_conn_provider",
        ),
        # A 0-length encrypted blob is always a bug (decrypt would fail).
        CheckConstraint(
            "length(token_enc) > 0",
            name="ck_broker_conn_token_nonempty",
        ),
        UniqueConstraint(
            "account_id", "provider",
            name="uq_broker_conn_account_provider",
        ),
        Index("ix_broker_conn_account_id", "account_id"),
    )
