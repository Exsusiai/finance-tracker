"""Pydantic v2 schemas for request/response serialization.

All amounts use str to preserve decimal precision across JSON round-trips.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field, field_validator

T = TypeVar("T")


def _validate_metadata_json(value: str | None) -> str | None:
    """Sprint 4 FIX-23 (review V3 §V3-P1-6): metadata_json must parse as a
    JSON object so v_account_balance / cashflow SQL can safely call
    ``json_extract`` on it. The previous schema accepted any string, which
    let one bad write break every balance query for the whole DB.

    Returns the canonicalised JSON string (re-serialised after parsing) so
    callers can rely on the row being well-formed.
    """
    if value is None or value == "":
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError(f"metadata_json must be valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise ValueError(
            f"metadata_json must be a JSON object, got {type(parsed).__name__}"
        )
    # Re-serialise so the stored form is always canonical (sorted keys make
    # subsequent equality / json_extract behaviour stable).
    return json.dumps(parsed, sort_keys=True, ensure_ascii=False)


# ─── Envelope ───────────────────────────────────────────────────────────────

class ApiSuccess(BaseModel, Generic[T]):
    success: bool = True
    data: T
    meta: dict[str, Any] | None = None


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ApiError(BaseModel):
    success: bool = False
    error: ErrorDetail


class PaginationMeta(BaseModel):
    next_cursor: str | None = None
    total: int = 0


# ─── Account ────────────────────────────────────────────────────────────────

class AccountCreate(BaseModel):
    name: str = Field(max_length=255)
    type: str = Field(pattern=r"^(bank|credit_card|brokerage|crypto_wallet|cash|other)$")
    institution: str | None = None
    account_number: str | None = None
    iban: str | None = None
    currency: str = Field(max_length=10)
    initial_balance: str = "0"
    notes: str | None = None
    metadata_json: str | None = None


class AccountUpdate(BaseModel):
    name: str | None = None
    type: str | None = None
    institution: str | None = None
    account_number: str | None = None
    iban: str | None = None
    currency: str | None = None
    is_active: bool | None = None
    notes: str | None = None
    metadata_json: str | None = None


class AccountOut(BaseModel):
    id: int
    name: str
    type: str
    institution: str | None
    account_number: str | None
    iban: str | None = None
    currency: str
    initial_balance: str
    is_active: bool
    notes: str | None
    metadata_json: str | None
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class BalanceOut(BaseModel):
    account_id: int
    account_name: str
    currency: str
    balance: str


# ─── Category ───────────────────────────────────────────────────────────────

class CategoryCreate(BaseModel):
    name: str = Field(max_length=100)
    kind: str = Field(pattern=r"^(expense|income|transfer)$")
    parent_id: int | None = None
    icon: str | None = None
    color: str | None = None
    sort_order: int = 0


class CategoryUpdate(BaseModel):
    name: str | None = None
    icon: str | None = None
    color: str | None = None
    sort_order: int | None = None


class CategoryOut(BaseModel):
    id: int
    name: str
    kind: str
    parent_id: int | None
    icon: str | None
    color: str | None
    sort_order: int
    is_system: bool
    created_at: str

    model_config = {"from_attributes": True}


class CategoryTree(BaseModel):
    id: int
    name: str
    kind: str
    icon: str | None
    color: str | None
    sort_order: int
    is_system: bool
    children: list[CategoryTree] = []


# ─── Transaction ────────────────────────────────────────────────────────────

class TransactionCreate(BaseModel):
    account_id: int
    counter_account_id: int | None = None
    category_id: int | None = None
    occurred_at: str
    posted_at: str | None = None
    amount: str
    currency: str = Field(max_length=10)
    fx_rate_to_base: str | None = None
    base_amount: str | None = None
    type: str = Field(pattern=r"^(expense|income|transfer|adjustment)$")
    description: str | None = None
    raw_description: str | None = None
    counterparty: str | None = None
    location: str | None = None
    tags: list[str] | None = None
    source: str = "manual"
    external_id: str | None = None
    is_pending: bool = False
    metadata_json: str | None = None
    user_note: str | None = None

    @field_validator("metadata_json")
    @classmethod
    def _check_metadata_json(cls, v: str | None) -> str | None:
        return _validate_metadata_json(v)


class TransactionUpdate(BaseModel):
    account_id: int | None = None
    counter_account_id: int | None = None
    category_id: int | None = None
    occurred_at: str | None = None
    posted_at: str | None = None
    amount: str | None = None
    currency: str | None = None
    fx_rate_to_base: str | None = None
    base_amount: str | None = None
    type: str | None = None
    description: str | None = None
    raw_description: str | None = None
    counterparty: str | None = None
    location: str | None = None
    tags: list[str] | None = None
    external_id: str | None = None
    is_pending: bool | None = None
    metadata_json: str | None = None
    user_note: str | None = None

    @field_validator("metadata_json")
    @classmethod
    def _check_metadata_json(cls, v: str | None) -> str | None:
        return _validate_metadata_json(v)


class TransactionOut(BaseModel):
    id: int
    account_id: int
    account_name: str | None = None
    counter_account_id: int | None
    category_id: int | None
    category_name: str | None = None
    occurred_at: str
    posted_at: str | None
    amount: str
    currency: str
    fx_rate_to_base: str | None
    base_amount: str | None
    type: str
    description: str | None
    raw_description: str | None
    counterparty: str | None
    location: str | None
    tags: list[str] = Field(default_factory=list)
    source: str
    pdf_import_id: int | None
    external_id: str | None
    is_pending: bool
    metadata_json: str | None
    user_note: str | None = None
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class TransactionBatchCreate(BaseModel):
    transactions: list[TransactionCreate]


class MarkTransferIn(BaseModel):
    counter_transaction_id: int | None = None
    transfer_direction: str | None = Field(None, pattern=r"^(in|out)$")
    # Optional transfer-kind category to attach to both legs of the pair.
    # When omitted, falls back to whatever the matcher resolved (内部储蓄 vs
    # 跨行划转). When the user picks one explicitly, theirs wins.
    category_id: int | None = None
    # Counter account id (no existing counter tx). When set, the route auto-
    # creates a mirror leg in that account so both balances reflect the move.
    counter_account_id: int | None = None


# ─── PDF Import ─────────────────────────────────────────────────────────────

class PdfImportOut(BaseModel):
    id: int
    filename: str
    file_hash: str
    file_size: int
    detected_bank: str | None
    parser_version: str | None
    account_id: int | None
    statement_period: str | None
    transactions_count: int
    status: str
    error_message: str | None
    preview: list[TransactionOut] = []
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


# ─── Asset ──────────────────────────────────────────────────────────────────

class AssetCreate(BaseModel):
    symbol: str = Field(max_length=50)
    name: str = Field(max_length=255)
    asset_class: str = Field(pattern=r"^(cash|a_share|eu_stock|us_stock|crypto|gold|bond|fund|other)$")
    currency: str = Field(max_length=10)
    market: str | None = None
    data_source: str | None = None
    data_source_id: str | None = None
    decimals: int = 2
    notes: str | None = None


class AssetUpdate(BaseModel):
    name: str | None = None
    data_source: str | None = None
    data_source_id: str | None = None
    decimals: int | None = None
    notes: str | None = None


class AssetOut(BaseModel):
    id: int
    symbol: str
    name: str
    asset_class: str
    currency: str
    market: str | None
    data_source: str | None
    data_source_id: str | None
    decimals: int
    notes: str | None
    created_at: str
    updated_at: str
    latest_price: str | None = None
    latest_price_currency: str | None = None

    model_config = {"from_attributes": True}


class AssetSearchResult(BaseModel):
    symbol: str
    name: str
    asset_class: str
    currency: str
    data_source: str
    data_source_id: str
    market: str | None = None
    thumb: str | None = None


# ─── Asset Holding ──────────────────────────────────────────────────────────

class HoldingCreate(BaseModel):
    account_id: int
    asset_id: int
    quantity: str
    avg_cost: str | None = None
    cost_currency: str | None = None
    notes: str | None = None


class HoldingUpdate(BaseModel):
    quantity: str | None = None
    avg_cost: str | None = None
    cost_currency: str | None = None
    notes: str | None = None


class HoldingOut(BaseModel):
    id: int
    account_id: int
    account_name: str | None = None
    asset_id: int
    symbol: str | None = None
    asset_name: str | None = None
    asset_class: str | None = None
    quantity: str
    avg_cost: str | None
    cost_currency: str | None
    current_price: str | None = None
    market_value: str | None = None
    unrealized_pnl: str | None = None
    last_synced_at: str | None
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


# ─── Portfolio ──────────────────────────────────────────────────────────────

class PortfolioSummary(BaseModel):
    base_currency: str
    total_value: str
    as_of: str
    by_class: dict[str, str] = {}
    # Sprint 4 FIX-22 (review V3 §V3-P1-5): each entry now carries both the
    # original-currency total AND the base-currency value, so callers reading
    # by_currency["EUR"] get unambiguous semantics. Previously the dict
    # key/value units mismatched (key was quote currency but value was
    # already in base currency).
    by_currency: dict[str, dict[str, str]] = {}
    # Holdings excluded from totals because no FX path resolved.
    fx_missing: list[dict[str, str]] = []


class PortfolioBreakdown(BaseModel):
    by_class: dict[str, dict[str, Any]] = {}
    by_currency: dict[str, dict[str, Any]] = {}


class NetWorthOut(BaseModel):
    base_currency: str
    cash_total: str
    investment_total: str
    net_worth: str
    cash_by_currency: dict[str, dict[str, str]] = {}
    # Sprint 4 FIX-22 (review V3 §V3-P1-5): same shape as PortfolioSummary —
    # each entry exposes both the original-currency total and the base-
    # currency total to remove the previous key/value unit mismatch.
    investment_by_currency: dict[str, dict[str, str]] = {}
    as_of: str


class BalanceAdjustmentIn(BaseModel):
    target_balance: str
    note: str | None = None
    occurred_at: str | None = None


# ─── Market Data ────────────────────────────────────────────────────────────

class MarketPriceOut(BaseModel):
    asset_id: int
    symbol: str | None = None
    quoted_at: str
    price: str
    currency: str
    source: str


class FxRateOut(BaseModel):
    base_currency: str
    quote_currency: str
    quoted_at: str
    rate: str
    source: str


class MarketRefreshStatus(BaseModel):
    last_refreshed_at: str | None
    status: str  # idle | running | error
    error_message: str | None = None
    next_scheduled_at: str | None = None


# ─── Cash Flow ──────────────────────────────────────────────────────────────

class CashFlowMonthly(BaseModel):
    period: str  # "YYYY-MM"
    base_currency: str = "CNY"  # All numeric fields below are folded to this currency
    income: str
    expense: str
    transfer: str
    savings: str
    fx_missing_count: int = 0  # Sprint 4 FIX-19 (§V3-P0-1): foreign rows excluded due to missing FX
    by_category: dict[str, str] = {}
    by_account: dict[str, str] = {}


class CashFlowByCategory(BaseModel):
    category_id: int | None
    category_name: str
    kind: str
    total: str
    count: int


class CashFlowTimeseries(BaseModel):
    periods: list[str] = []
    income: list[str] = []
    expense: list[str] = []
    savings: list[str] = []


# ─── Categorization Rule ────────────────────────────────────────────────────

class RuleCreate(BaseModel):
    pattern: str = Field(max_length=500)
    pattern_type: str = Field(pattern=r"^(contains|regex|exact|starts_with)$")
    field: str = Field(pattern=r"^(description|counterparty|raw_description)$")
    category_id: int
    priority: int = 0
    enabled: bool = True


class RuleUpdate(BaseModel):
    pattern: str | None = None
    pattern_type: str | None = None
    field: str | None = None
    category_id: int | None = None
    priority: int | None = None
    enabled: bool | None = None


class RuleOut(BaseModel):
    id: int
    pattern: str
    pattern_type: str
    field: str
    category_id: int
    category_name: str | None = None
    priority: int
    enabled: bool
    hit_count: int
    created_at: str

    model_config = {"from_attributes": True}


class RuleTestIn(BaseModel):
    description: str | None = None
    counterparty: str | None = None
    raw_description: str | None = None


class RuleTestOut(BaseModel):
    matched: bool
    rule_id: int | None = None
    category_id: int | None = None
    category_name: str | None = None


# ─── System / Settings ─────────────────────────────────────────────────────

class SettingsOut(BaseModel):
    base_currency: str
    market_refresh_crypto_sec: int
    market_refresh_stock_sec: int
    market_refresh_fx_sec: int
    market_refresh_gold_sec: int


class SettingsUpdate(BaseModel):
    base_currency: str | None = None
    market_refresh_crypto_sec: int | None = None
    market_refresh_stock_sec: int | None = None
    market_refresh_fx_sec: int | None = None
    market_refresh_gold_sec: int | None = None


class BackupInfo(BaseModel):
    filename: str
    size_bytes: int
    created_at: str
