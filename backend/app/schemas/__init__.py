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
    # Mirrors models.AccountType + ck_account_type CHECK. When adding a
    # new type, sync FOUR places: this regex, models.AccountType,
    # the Account.__table_args__ CHECK, and a new alembic migration.
    type: str = Field(
        pattern=r"^(bank|credit_card|brokerage|crypto_wallet|exchange|cash|other)$"
    )
    institution: str | None = None
    account_number: str | None = None
    iban: str | None = None
    currency: str = Field(max_length=10)
    initial_balance: str = "0"
    # Accept include_in_total on create so the frontend can submit it in
    # one POST instead of POST-then-PATCH (FE-M5 finding 2026-05-19 —
    # the two-step pattern silently dropped the flag if the PATCH failed).
    include_in_total: bool = True
    notes: str | None = None
    metadata_json: str | None = None

    @field_validator("currency", mode="after")
    @classmethod
    def _crypto_must_be_usdt(cls, v: str, info) -> str:
        """V6-P1-3 (2026-05-20): crypto_wallet / exchange holdings are
        valued in USDT by the wallet_sync pipeline. If the account
        currency is anything else, `/accounts/balances` would add a
        USDT-denominated holding value to an EUR/CNY-labelled bucket
        and silently mislabel the unit. Enforce the project invariant
        at the API edge so the bug is impossible to introduce via UI
        ordering / direct curl / future client."""
        t = (info.data or {}).get("type")
        if t in ("crypto_wallet", "exchange") and v.upper() != "USDT":
            raise ValueError(
                f"{t} accounts must use currency=USDT (got {v!r}). "
                "Crypto positions are quoted in USDT internally."
            )
        return v


class AccountUpdate(BaseModel):
    name: str | None = None
    type: str | None = None
    institution: str | None = None
    account_number: str | None = None
    iban: str | None = None
    currency: str | None = None
    is_active: bool | None = None
    include_in_total: bool | None = None
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
    include_in_total: bool = True
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
    # Allowed |out_amount - in_amount| when binding two existing legs.
    # Default 0.01 enforces cent-precision matching. Manual flows (paying
    # for friends, uneven split, rounding) can pass a larger value to
    # accept the pair anyway. Same-currency only — cross-currency pairs
    # are still rejected outright.
    amount_tolerance: str | None = None


# ─── PDF Import ─────────────────────────────────────────────────────────────

class ParsedPreviewTx(BaseModel):
    """One parsed-but-not-yet-inserted transaction, shown in the pre-commit
    preview (status='awaiting_review'). These don't exist in the DB yet — they
    come straight from the parser output, so they carry no id/account."""

    occurred_at: str | None = None
    amount: str
    currency: str | None = None
    type: str | None = None
    description: str | None = None


class PdfImportOut(BaseModel):
    id: int
    filename: str
    file_hash: str
    file_size: int
    detected_bank: str | None
    parser_version: str | None
    account_id: int | None  # the resolved/candidate account (preselected in UI)
    statement_period: str | None
    transactions_count: int
    status: str
    error_message: str | None
    # DB-backed preview (post-commit): real Transaction rows.
    preview: list[TransactionOut] = []
    # Parser-output preview (pre-commit / awaiting_review): ALL parsed rows,
    # nothing inserted yet.
    parsed_preview: list[ParsedPreviewTx] = []
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
    # Which chain this position lives on. Empty string for non-crypto
    # holdings (stocks / cash / gold) and for CEX-pooled crypto. For
    # on-chain crypto: "ethereum" / "arbitrum" / "bitcoin" / "solana"
    # / "tron" / etc. Same symbol on different chains = distinct rows
    # (A-sprint 2026-05-20). UI uses this to render a chain badge.
    chain: str = ""
    quantity: str
    avg_cost: str | None
    cost_currency: str | None
    current_price: str | None = None
    # price_currency: the currency of the latest market price quote (e.g. "USDT").
    # Distinct from cost_currency — crypto holdings frequently have price_currency
    # but no cost_currency (unknown purchase basis).
    price_currency: str | None = None
    market_value: str | None = None
    # market_value_currency matches price_currency when market_value is set.
    market_value_currency: str | None = None
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
    requires_llm: bool = False
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


# ─── Categorization Notes (knowledge base) ─────────────────────────────────

class NoteCreate(BaseModel):
    category_id: int
    trigger_text: str = Field(min_length=1, max_length=2000)
    note_text: str = Field(min_length=1, max_length=4000)
    enabled: bool = True


class NoteUpdate(BaseModel):
    category_id: int | None = None
    trigger_text: str | None = Field(default=None, max_length=2000)
    note_text: str | None = Field(default=None, max_length=4000)
    enabled: bool | None = None


class NoteOut(BaseModel):
    id: int
    category_id: int
    category_name: str | None = None
    trigger_text: str
    note_text: str
    source_transaction_id: int | None = None
    usage_count: int
    enabled: bool
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


# ─── LLM settings ──────────────────────────────────────────────────────────

class LLMSettingsOut(BaseModel):
    enabled: bool
    provider: str
    model: str
    monthly_usd_budget: float
    confidence_threshold: float
    use_grounding: bool
    max_notes_in_prompt: int
    api_key_present: bool  # never echoes the secret itself
    # True when an encrypted key row EXISTS but won't decrypt with the
    # current FINANCE_BANK_ENCRYPTION_KEY (key was rotated). Lets the UI
    # show "key changed, re-enter" instead of "not set". (ERR-20260607-001)
    api_key_stale: bool = False


class LLMSettingsUpdate(BaseModel):
    enabled: bool | None = None
    model: str | None = None
    monthly_usd_budget: float | None = Field(default=None, ge=0)
    confidence_threshold: float | None = Field(default=None, ge=0, le=1)
    use_grounding: bool | None = None
    max_notes_in_prompt: int | None = Field(default=None, ge=0, le=100)
    # Provider API key (write-only). Empty string clears it. NEVER echoed
    # back via GET — `api_key_present` boolean is the only read signal.
    gemini_api_key: str | None = None


class LLMCostOut(BaseModel):
    used_usd: float
    budget_usd: float
    remaining_usd: float
    period: str  # "YYYY-MM"


# ─── Wallet Sync (P1-4) ────────────────────────────────────────────────────


# Whitelist of chains the backend can actually sync. Must mirror
# crypto_sync.dispatch(). Used by ChainAddressIn to reject typos /
# arbitrary strings before they end up in /v1/accounts/{id}/addresses
# (Sec-H2 finding — see also services/wallet_sync/orchestrator.py).
_SUPPORTED_CHAINS: frozenset[str] = frozenset({
    # EVM family
    "ethereum", "arbitrum", "optimism", "base", "polygon",
    "polygon-zkevm", "zksync", "linea", "scroll", "mantle", "blast",
    # non-EVM
    "bitcoin", "solana", "tron",
})

# Per-chain address format regex. Conservative — rejects anything that
# isn't structurally a valid address for that chain. Defends the URL
# interpolation in providers (Sec-H2 finding 2026-05-19).
import re as _re
_ADDRESS_PATTERNS: dict[str, "_re.Pattern[str]"] = {
    "ethereum":      _re.compile(r"^0x[a-fA-F0-9]{40}$"),
    "arbitrum":      _re.compile(r"^0x[a-fA-F0-9]{40}$"),
    "optimism":      _re.compile(r"^0x[a-fA-F0-9]{40}$"),
    "base":          _re.compile(r"^0x[a-fA-F0-9]{40}$"),
    "polygon":       _re.compile(r"^0x[a-fA-F0-9]{40}$"),
    "polygon-zkevm": _re.compile(r"^0x[a-fA-F0-9]{40}$"),
    "zksync":        _re.compile(r"^0x[a-fA-F0-9]{40}$"),
    "linea":         _re.compile(r"^0x[a-fA-F0-9]{40}$"),
    "scroll":        _re.compile(r"^0x[a-fA-F0-9]{40}$"),
    "mantle":        _re.compile(r"^0x[a-fA-F0-9]{40}$"),
    "blast":         _re.compile(r"^0x[a-fA-F0-9]{40}$"),
    # Bitcoin: legacy 1.../3..., bech32 bc1.../tb1...
    "bitcoin":       _re.compile(r"^(?:bc1|tb1|[13])[a-zA-Z0-9]{25,87}$"),
    # Solana base58 32-byte pubkey
    "solana":        _re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"),
    # Tron addresses always start with T
    "tron":          _re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$"),
}


class ChainAddressIn(BaseModel):
    chain: str = Field(min_length=1, max_length=50)
    address: str = Field(min_length=1, max_length=128)
    label: str | None = Field(default=None, max_length=255)

    @field_validator("chain", mode="after")
    @classmethod
    def _normalise_chain(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in _SUPPORTED_CHAINS:
            raise ValueError(
                f"unsupported chain {v!r}; supported: {sorted(_SUPPORTED_CHAINS)}"
            )
        return v

    @field_validator("address", mode="after")
    @classmethod
    def _validate_address(cls, v: str, info) -> str:
        v = v.strip()
        chain = info.data.get("chain")  # already normalised by validator above
        # If chain itself failed validation we'd never reach here, but be
        # defensive — fall back to a tolerant check that at least bans
        # path-separator characters (the SSRF concern).
        if not chain:
            if "/" in v or ".." in v:
                raise ValueError("address contains illegal characters")
            return v
        pattern = _ADDRESS_PATTERNS.get(chain)
        if pattern is None:
            # Chain is in _SUPPORTED_CHAINS but somehow not in the regex
            # table — treat as defensive deny.
            raise ValueError(f"no address validator wired for chain {chain!r}")
        if not pattern.fullmatch(v):
            raise ValueError(f"address does not match expected format for {chain}")
        return v


class ChainAddressOut(BaseModel):
    id: int
    chain: str
    address: str
    label: str | None = None
    last_synced_at: str | None = None
    last_sync_status: str | None = None
    last_sync_error: str | None = None

    model_config = {"from_attributes": True}


class ExchangeConnectionIn(BaseModel):
    exchange: str = Field(min_length=1, max_length=50)
    # Cap secrets at 512 chars so a misbehaving client can't ship a
    # multi-MB header (Sec-M4 finding 2026-05-19). Real CEX keys are
    # all under 128 chars.
    api_key: str = Field(min_length=1, max_length=512)
    api_secret: str = Field(min_length=1, max_length=512)
    # Required for Bitget; ignored on Binance. Validated again service-side
    # so the user gets a clean 4xx rather than upstream "Invalid sign".
    passphrase: str | None = Field(default=None, max_length=512)


class ExchangeConnectionOut(BaseModel):
    """Never echoes secrets back to the client.

    The frontend shows ``has_credentials`` to indicate the row exists; to
    rotate the key the user PUTs a fresh ``ExchangeConnectionIn``.
    """

    id: int
    exchange: str
    has_credentials: bool
    has_passphrase: bool
    # True when api_key/secret rows exist but no longer decrypt with the
    # current encryption key (rotated). UI prompts re-entry. (ERR-20260607-001)
    credentials_stale: bool = False
    last_synced_at: str | None = None
    last_sync_status: str | None = None
    last_sync_error: str | None = None


class BrokerConnectionIn(BaseModel):
    provider: str = Field(min_length=1, max_length=50)
    # Flex Web Service token. Capped like exchange secrets; real tokens are
    # ~40-char numeric strings.
    token: str = Field(min_length=1, max_length=512)
    # Flex Query ID — not a secret, identifies which configured query to run.
    query_id: str = Field(min_length=1, max_length=64)


class BrokerConnectionOut(BaseModel):
    """Never echoes the Flex token back. ``has_token`` indicates the row
    exists; rotation = PUT a fresh ``BrokerConnectionIn``."""

    id: int
    provider: str
    # NULL for Trade Republic (no Flex-query concept); set for IBKR.
    query_id: str | None = None
    has_token: bool
    # True when the token row exists but no longer decrypts with the current
    # encryption key (rotated). UI prompts re-entry. (ERR-20260607-001)
    credentials_stale: bool = False
    last_synced_at: str | None = None
    last_sync_status: str | None = None
    last_sync_error: str | None = None


class TRConnectIn(BaseModel):
    """Step 1 of Trade Republic web login: phone + PIN."""

    phone: str = Field(min_length=5, max_length=20)
    pin: str = Field(min_length=4, max_length=8)


class TRConnectOut(BaseModel):
    """Result of step 1 — TR has sent a 4-digit code to the app/SMS."""

    countdown_seconds: int
    message: str = "验证码已发送，请在 App 或短信中查收并输入"


class TRVerifyIn(BaseModel):
    """Step 2: the 4-digit code from the TR app/SMS."""

    code: str = Field(min_length=4, max_length=6)


class SyncResultOut(BaseModel):
    label: str
    chain: str | None = None
    exchange: str | None = None
    synced: int
    error: str | None = None


class SyncSummaryOut(BaseModel):
    account_id: int
    account_type: str
    total_synced: int
    total_errors: int
    results: list[SyncResultOut]


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
