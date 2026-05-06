"""End-to-end API tests for Finance Tracker backend.

Integration suite — requires a running uvicorn instance on
``BASE_URL`` and the ``FINANCE_TRACKER_API_TOKEN`` env var.

Auto-skipped when no server is reachable, so unit-test runs stay green
without manual flag wrangling. Run explicitly with:

    .venv/bin/uvicorn app.main:app --port 8199 &
    .venv/bin/pytest backend/tests/test_api.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import pytest
import httpx

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BASE_URL = "http://127.0.0.1:8199/api/v1"
TOKEN = os.environ.get("FINANCE_TRACKER_API_TOKEN", os.environ.get("TEST_API_TOKEN", ""))


def _server_reachable() -> bool:
    """Probe the integration server so the suite skips when it's not running."""
    try:
        with httpx.Client(timeout=0.5) as c:
            r = c.get(f"{BASE_URL}/health")
            return r.status_code < 500
    except Exception:
        return False


# Sprint 1 FIX-7: auto-skip the whole module when the server isn't up so
# `pytest backend/tests/` runs cleanly without flags. Run integration mode by
# starting uvicorn first (see docstring).
pytestmark = pytest.mark.skipif(
    not _server_reachable(),
    reason="Integration suite requires a running backend on " + BASE_URL,
)


@pytest.fixture(scope="session")
def headers():
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


@pytest.fixture(scope="session")
def client():
    return httpx.Client(base_url=BASE_URL, timeout=10.0)


def _post(client, path, body, headers):
    r = client.post(path, json=body, headers=headers)
    return r.json()


def _get(client, path, headers, **params):
    r = client.get(path, headers=headers, params=params)
    return r.json()


def _patch(client, path, body, headers):
    r = client.patch(path, json=body, headers=headers)
    return r.json()


def _delete(client, path, headers):
    r = client.delete(path, headers=headers)
    return r.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health(self, client):
        d = _get(client, "/health", {})
        assert d["status"] == "ok"

    def test_version(self, client):
        d = _get(client, "/version", {})
        assert d["version"] == "0.1.0"


class TestAuth:
    def test_no_token(self, client):
        r = client.get("/accounts")
        assert r.status_code == 401

    def test_bad_token(self, client):
        r = client.get("/accounts", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401


class TestAccounts:
    def test_crud(self, client, headers):
        # Create
        d = _post(client, "/accounts", {"name": "N26", "type": "bank", "currency": "EUR", "initial_balance": "1000"}, headers)
        assert d["success"]
        aid = d["data"]["id"]

        # List
        d = _get(client, "/accounts", headers)
        assert d["success"] and len(d["data"]) >= 1

        # Get
        d = _get(client, f"/accounts/{aid}", headers)
        assert d["data"]["name"] == "N26"

        # Update
        d = _patch(client, f"/accounts/{aid}", {"notes": "test"}, headers)
        assert d["data"]["notes"] == "test"

        # Delete (soft)
        d = _delete(client, f"/accounts/{aid}", headers)
        assert d["data"]["deleted"]


class TestCategories:
    def test_crud(self, client, headers):
        d = _post(client, "/categories", {"name": "餐饮", "kind": "expense"}, headers)
        assert d["success"]
        cid = d["data"]["id"]

        d = _get(client, "/categories/tree", headers)
        assert d["success"]


class TestTransactions:
    def test_crud(self, client, headers):
        # Create account
        acc = _post(client, "/accounts", {"name": "Test", "type": "bank", "currency": "EUR", "initial_balance": "1000"}, headers)
        aid = acc["data"]["id"]

        cat = _post(client, "/categories", {"name": "Food", "kind": "expense"}, headers)
        cid = cat["data"]["id"]

        # Create
        d = _post(client, "/transactions", {
            "account_id": aid, "category_id": cid,
            "occurred_at": "2026-04-15T12:00:00Z", "amount": "-25.50",
            "currency": "EUR", "type": "expense", "description": "Lunch"
        }, headers)
        assert d["success"]
        tid = d["data"]["id"]

        # Get
        d = _get(client, f"/transactions/{tid}", headers)
        assert d["data"]["amount"] == "-25.5"

        # List with filter
        d = _get(client, "/transactions", headers, type="expense")
        assert d["success"] and len(d["data"]) >= 1

        # Update
        d = _patch(client, f"/transactions/{tid}", {"description": "Updated"}, headers)
        assert d["data"]["description"] == "Updated"

        # Delete
        d = _delete(client, f"/transactions/{tid}", headers)
        assert d["data"]["deleted"]


class TestBalances:
    def test_balances(self, client, headers):
        d = _get(client, "/accounts/balances", headers)
        assert d["success"]


class TestAssets:
    def test_crud(self, client, headers):
        d = _post(client, "/assets", {
            "symbol": "BTC", "name": "Bitcoin", "asset_class": "crypto",
            "currency": "USD", "data_source_id": "bitcoin"
        }, headers)
        assert d["success"]
        aid = d["data"]["id"]

        d = _get(client, f"/assets/{aid}", headers)
        assert d["data"]["symbol"] == "BTC"

        d = _delete(client, f"/assets/{aid}", headers)
        assert d["data"]["deleted"]


class TestHoldings:
    def test_crud(self, client, headers):
        acc = _post(client, "/accounts", {"name": "Broker", "type": "brokerage", "currency": "USD", "initial_balance": "0"}, headers)
        aid = acc["data"]["id"]
        ast = _post(client, "/assets", {"symbol": "ETH", "name": "Ethereum", "asset_class": "crypto", "currency": "USD"}, headers)
        astid = ast["data"]["id"]

        d = _post(client, "/holdings", {"account_id": aid, "asset_id": astid, "quantity": "10", "avg_cost": "2000"}, headers)
        assert d["success"]
        hid = d["data"]["id"]

        d = _get(client, f"/holdings/{hid}", headers)
        assert d["data"]["symbol"] == "ETH"

        d = _delete(client, f"/holdings/{hid}", headers)
        assert d["data"]["deleted"]


class TestCashflow:
    def test_monthly(self, client, headers):
        d = _get(client, "/cashflow/monthly", headers)
        assert d["success"]

    def test_timeseries(self, client, headers):
        d = _get(client, "/cashflow/timeseries", headers)
        assert d["success"]


class TestRules:
    def test_crud_and_test(self, client, headers):
        cat = _post(client, "/categories", {"name": "TestCat", "kind": "expense"}, headers)
        cid = cat["data"]["id"]

        d = _post(client, "/rules", {
            "pattern": "lunch", "pattern_type": "contains",
            "field": "description", "category_id": cid
        }, headers)
        assert d["success"]
        rid = d["data"]["id"]

        d = _post(client, "/rules/test", {"description": "had lunch today"}, headers)
        assert d["data"]["matched"]

        d = _delete(client, f"/rules/{rid}", headers)
        assert d["data"]["deleted"]


class TestSystem:
    def test_settings(self, client, headers):
        d = _get(client, "/system/settings", headers)
        assert d["success"]
        assert d["data"]["base_currency"] == "CNY"

    def test_backup(self, client, headers):
        d = _post(client, "/system/backup", {}, headers)
        assert d["success"]
