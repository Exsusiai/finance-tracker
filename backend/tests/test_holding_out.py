"""V5-P2-2: tests for _holding_to_out market_value / price_currency plumbing.

Key invariants:
- market_value is computed whenever latest_price exists, regardless of cost_currency.
- market_value_currency == price_currency when market_value is set.
- unrealized_pnl is only computed when cost_currency == price_currency.
"""

from __future__ import annotations

import os
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

_TEST_TOKEN = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "CNY")

from app.api.v1.holdings import _holding_to_out  # noqa: E402
from app.models import AssetHolding, Asset  # noqa: E402


def _make_holding(
    *,
    quantity: str = "1",
    avg_cost: str | None = None,
    cost_currency: str | None = None,
) -> AssetHolding:
    h = MagicMock(spec=AssetHolding)
    h.id = 1
    h.account_id = 1
    h.account = MagicMock()
    h.account.name = "TestAccount"
    h.asset_id = 1
    h.quantity = Decimal(quantity)
    h.avg_cost = Decimal(avg_cost) if avg_cost is not None else None
    h.cost_currency = cost_currency
    h.last_synced_at = None
    h.created_at = "2026-01-01T00:00:00Z"
    h.updated_at = "2026-01-01T00:00:00Z"
    return h


def _make_asset(symbol: str = "BTC", asset_class: str = "crypto") -> Asset:
    a = MagicMock(spec=Asset)
    a.symbol = symbol
    a.name = symbol
    a.asset_class = asset_class
    return a


class TestHoldingToOut:
    def test_market_value_computed_when_cost_currency_is_null(self):
        """Wallet/CEX-synced crypto: cost_currency=None but price exists → market_value filled."""
        h = _make_holding(quantity="2.5", avg_cost=None, cost_currency=None)
        out = _holding_to_out(h, _make_asset(), latest_price=Decimal("40000"), price_currency="USDT")

        assert out.market_value == str(Decimal("2.5") * Decimal("40000"))  # 2.5 * 40000
        assert out.market_value_currency == "USDT"
        assert out.price_currency == "USDT"

    def test_market_value_currency_equals_price_currency(self):
        """market_value_currency must track the quote currency of the price, not cost_currency."""
        h = _make_holding(quantity="1", avg_cost="30000", cost_currency="EUR")
        out = _holding_to_out(h, _make_asset(), latest_price=Decimal("45000"), price_currency="USDT")

        assert out.market_value == "45000"
        assert out.market_value_currency == "USDT"

    def test_unrealized_pnl_null_when_cost_currency_differs_from_price_currency(self):
        """PnL subtraction is only valid when units match."""
        h = _make_holding(quantity="1", avg_cost="30000", cost_currency="EUR")
        out = _holding_to_out(h, _make_asset(), latest_price=Decimal("45000"), price_currency="USDT")

        # market_value is still computed (fix objective), but pnl is undefined
        assert out.market_value is not None
        assert out.unrealized_pnl is None

    def test_unrealized_pnl_computed_when_currencies_match(self):
        """Standard case: cost and price are in the same currency."""
        h = _make_holding(quantity="2", avg_cost="30000", cost_currency="USDT")
        out = _holding_to_out(h, _make_asset(), latest_price=Decimal("40000"), price_currency="USDT")

        # market_value = 2 * 40000 = 80000; cost = 2 * 30000 = 60000; pnl = 20000
        assert out.market_value == "80000"
        assert out.unrealized_pnl == "20000"

    def test_no_price_yields_null_market_value(self):
        """No market price → market_value, price_currency, market_value_currency all null."""
        h = _make_holding(quantity="1", avg_cost="100", cost_currency="USDT")
        out = _holding_to_out(h, _make_asset(), latest_price=None, price_currency=None)

        assert out.market_value is None
        assert out.price_currency is None
        assert out.market_value_currency is None
        assert out.unrealized_pnl is None

    def test_no_asset_produces_valid_output(self):
        """_holding_to_out should not crash when asset is None (manually created holding)."""
        h = _make_holding(quantity="10", avg_cost=None, cost_currency=None)
        out = _holding_to_out(h, asset=None, latest_price=Decimal("1.5"), price_currency="USDT")

        assert out.symbol is None
        assert out.asset_class is None
        assert out.market_value == str(Decimal("10") * Decimal("1.5"))
        assert out.market_value_currency == "USDT"

    def test_quantity_precision_preserved(self):
        """Decimal arithmetic must not truncate precision."""
        h = _make_holding(quantity="0.12345678", cost_currency=None)
        out = _holding_to_out(h, _make_asset(), latest_price=Decimal("50000"), price_currency="USDT")

        expected = Decimal("0.12345678") * Decimal("50000")
        assert out.market_value == str(expected)
