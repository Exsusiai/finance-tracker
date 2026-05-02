"""Notion sync engine — pushes finance data to Notion databases."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

NOTION_VERSION = "2022-06-28"
NOTION_API_BASE = "https://api.notion.com/v1"

# ─── Rate limit safety ───────────────────────────────────────────────────────

# Notion allows ~3 requests/sec. We stay well under that.
_MIN_INTERVAL_SEC = 0.4


@dataclass
class SyncStats:
    """Tracks sync operation results."""

    transactions_created: int = 0
    transactions_updated: int = 0
    transactions_skipped: int = 0
    asset_summary_updated: bool = False
    cashflow_entries_created: int = 0
    cashflow_entries_skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_transactions(self) -> int:
        return self.transactions_created + self.transactions_updated + self.transactions_skipped

    def summary(self) -> str:
        parts = [
            f"Transactions: {self.transactions_created} created, "
            f"{self.transactions_updated} updated, "
            f"{self.transactions_skipped} skipped",
        ]
        if self.asset_summary_updated:
            parts.append("Asset summary: updated")
        if self.cashflow_entries_created or self.cashflow_entries_skipped:
            parts.append(
                f"Cash flow: {self.cashflow_entries_created} created, "
                f"{self.cashflow_entries_skipped} skipped"
            )
        if self.errors:
            parts.append(f"Errors: {len(self.errors)}")
        return "; ".join(parts)


class NotionSyncService:
    """One-way sync from finance-tracker DB → Notion.

    Usage::

        svc = NotionSyncService(
            notion_token="ntn_xxx",
            transactions_db_id="xxx",
            cashflow_db_id="xxx",
            asset_page_id="xxx",
        )
        stats = await svc.sync_all(db_session)
    """

    def __init__(
        self,
        notion_token: str,
        transactions_db_id: str = "",
        cashflow_db_id: str = "",
        asset_page_id: str = "",
    ):
        self._token = notion_token
        self._tx_db_id = transactions_db_id
        self._cf_db_id = cashflow_db_id
        self._asset_page_id = asset_page_id
        self._last_request_ts: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._token and (self._tx_db_id or self._cf_db_id or self._asset_page_id))

    # ─── HTTP helpers ─────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
    ) -> dict:
        """Send a rate-limited request to the Notion API."""
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < _MIN_INTERVAL_SEC:
            await asyncio_sleep(_MIN_INTERVAL_SEC - elapsed)

        url = f"{NOTION_API_BASE}{path}" if not path.startswith("http") else path
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(method, url, headers=self._headers(), json=payload)
            self._last_request_ts = time.monotonic()

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "1"))
                logger.warning("Notion rate limited, retrying after %.1fs", retry_after)
                await asyncio_sleep(retry_after)
                return await self._request(method, path, payload)

            if resp.status_code >= 400:
                body = resp.text
                logger.error("Notion API error %d: %s", resp.status_code, body)
                raise RuntimeError(f"Notion API {resp.status_code}: {body}")

            return resp.json()

    async def _query_database(
        self,
        database_id: str,
        filter_clause: dict | None = None,
        sorts: list[dict] | None = None,
        page_size: int = 100,
    ) -> list[dict]:
        """Paginated database query."""
        results: list[dict] = []
        start_cursor: str | None = None
        while True:
            payload: dict[str, Any] = {"page_size": page_size}
            if filter_clause:
                payload["filter"] = filter_clause
            if sorts:
                payload["sorts"] = sorts
            if start_cursor:
                payload["start_cursor"] = start_cursor

            data = await self._request("POST", f"/databases/{database_id}/query", payload)
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            start_cursor = data["next_cursor"]
        return results

    async def _update_page(self, page_id: str, properties: dict) -> dict:
        return await self._request("PATCH", f"/pages/{page_id}", {"properties": properties})

    async def _create_page(self, database_id: str, properties: dict, children: list[dict] | None = None) -> dict:
        payload: dict[str, Any] = {
            "parent": {"database_id": database_id},
            "properties": properties,
        }
        if children:
            payload["children"] = children
        return await self._request("POST", "/pages", payload)

    async def _append_blocks(self, block_id: str, children: list[dict]) -> dict:
        return await self._request("PATCH", f"/blocks/{block_id}/children", {"children": children})

    # ─── Sync: Transactions ──────────────────────────────────────────────

    async def sync_transactions(self, db_session, since: str | None = None) -> SyncStats:
        """Sync recent transactions to Notion transactions database.

        Args:
            db_session: SQLAlchemy async session.
            since: ISO-8601 timestamp — only sync transactions updated after this time.
                   If None, syncs all transactions (up to 1000 most recent).
        """
        stats = SyncStats()

        if not self._tx_db_id:
            stats.errors.append("Transactions database ID not configured")
            return stats

        from sqlalchemy import select, and_
        from app.models import Transaction, Account, Category

        # Build query
        query = (
            select(Transaction, Account, Category)
            .outerjoin(Account, Transaction.account_id == Account.id)
            .outerjoin(Category, Transaction.category_id)
            .where(Transaction.deleted_at.is_(None))
        )
        if since:
            query = query.where(Transaction.updated_at >= since)
        query = query.order_by(Transaction.occurred_at.desc()).limit(1000)

        result = await db_session.execute(query)
        rows = result.all()

        if not rows:
            logger.info("No transactions to sync")
            return stats

        # Build lookup of existing Notion entries (by our internal ID in metadata)
        notion_pages = await self._query_database(self._tx_db_id)
        notion_by_tx_id: dict[int, dict] = {}
        for page in notion_pages:
            meta = _extract_notion_property(page, "Tx ID")
            if meta:
                try:
                    notion_by_tx_id[int(meta)] = page
                except ValueError:
                    pass

        for tx, account, category in rows:
            try:
                props = _build_transaction_properties(tx, account, category)

                existing = notion_by_tx_id.get(tx.id)
                if existing:
                    await self._update_page(existing["id"], props)
                    stats.transactions_updated += 1
                else:
                    await self._create_page(self._tx_db_id, props)
                    stats.transactions_created += 1

            except Exception as e:
                stats.errors.append(f"Transaction {tx.id}: {e}")
                logger.exception("Failed to sync transaction %d", tx.id)

        logger.info("Transaction sync complete: %s", stats.summary())
        return stats

    # ─── Sync: Cash Flow ─────────────────────────────────────────────────

    async def sync_cashflow(self, db_session) -> SyncStats:
        """Sync cash flow snapshots to Notion cashflow database."""
        stats = SyncStats()

        if not self._cf_db_id:
            stats.errors.append("Cash flow database ID not configured")
            return stats

        from sqlalchemy import select
        from app.models import CashFlowSnapshot

        result = await db_session.execute(
            select(CashFlowSnapshot)
            .order_by(CashFlowSnapshot.period_year.desc(), CashFlowSnapshot.period_month.desc())
            .limit(60)  # Last 5 years
        )
        snapshots = result.scalars().all()

        if not snapshots:
            logger.info("No cash flow snapshots to sync")
            return stats

        # Build lookup of existing Notion entries
        notion_pages = await self._query_database(self._cf_db_id)
        notion_by_period: dict[str, dict] = {}
        for page in notion_pages:
            period = _extract_notion_property(page, "Period")
            if period:
                notion_by_period[period] = page

        for snap in snapshots:
            try:
                period_key = f"{snap.period_year}-{snap.period_month:02d}"
                props = _build_cashflow_properties(snap)

                existing = notion_by_period.get(period_key)
                if existing:
                    await self._update_page(existing["id"], props)
                    stats.cashflow_entries_skipped += 1  # counted as "handled"
                else:
                    await self._create_page(self._cf_db_id, props)
                    stats.cashflow_entries_created += 1

            except Exception as e:
                stats.errors.append(f"CashFlow {snap.period_year}-{snap.period_month}: {e}")
                logger.exception("Failed to sync cashflow %d-%d", snap.period_year, snap.period_month)

        logger.info("Cash flow sync complete: %s", stats.summary())
        return stats

    # ─── Sync: Asset Summary ─────────────────────────────────────────────

    async def sync_asset_summary(self, db_session) -> SyncStats:
        """Sync asset portfolio summary to a Notion page."""
        stats = SyncStats()

        if not self._asset_page_id:
            stats.errors.append("Asset summary page ID not configured")
            return stats

        from sqlalchemy import select, func
        from app.models import AssetHolding, Asset, MarketPrice, Account

        # Get all holdings with asset info
        result = await db_session.execute(
            select(AssetHolding, Asset, Account)
            .join(Asset, AssetHolding.asset_id == Asset.id)
            .join(Account, AssetHolding.account_id == Account.id)
            .where(Account.is_active == True)
        )
        rows = result.all()

        if not rows:
            logger.info("No asset holdings to sync")
            return stats

        # Group by asset
        from collections import defaultdict
        asset_totals: dict[int, dict] = {}
        for holding, asset, account in rows:
            aid = asset.id
            if aid not in asset_totals:
                asset_totals[aid] = {
                    "asset": asset,
                    "total_quantity": Decimal("0"),
                    "accounts": [],
                }
            asset_totals[aid]["total_quantity"] += holding.quantity
            asset_totals[aid]["accounts"].append(
                f"{account.name}: {holding.quantity}"
            )

        # Get latest prices
        asset_ids = list(asset_totals.keys())
        if not asset_ids:
            return stats

        # Build summary text
        lines = [f"**资产汇总** — 更新于 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"]

        total_value_base = Decimal("0")

        for aid, info in asset_totals.items():
            asset = info["asset"]
            qty = info["total_quantity"]
            lines.append(f"### {asset.name} ({asset.symbol})")
            lines.append(f"- 类型: {asset.asset_class}")
            lines.append(f"- 总持有: {qty} {asset.currency}")
            for acct in info["accounts"]:
                lines.append(f"  - {acct}")
            lines.append("")

        # Also compute total account balances
        from app.models import Transaction
        bal_result = await db_session.execute(
            select(Account, Account.initial_balance + func.coalesce(func.sum(Transaction.amount), 0))
            .outerjoin(Transaction, Transaction.account_id == Account.id)
            .where(Account.deleted_at.is_(None), Account.is_active == True, Transaction.deleted_at.is_(None))
            .group_by(Account.id)
        )
        balances = bal_result.all()

        if balances:
            lines.append("### 账户余额\n")
            for account, balance in balances:
                lines.append(f"- {account.name} ({account.currency}): {balance}")
            lines.append("")

        # Build Notion blocks
        children = []
        for line in lines:
            if not line:
                continue
            if line.startswith("### "):
                children.append({
                    "object": "block",
                    "type": "heading_3",
                    "heading_3": {"rich_text": [{"type": "text", "text": {"content": line[4:]}}]},
                })
            elif line.startswith("- "):
                children.append({
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": [{"type": "text", "text": {"content": line[2:]}}]
                    },
                })
            elif line.startswith("  - "):
                children.append({
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": [{"type": "text", "text": {"content": line[4:]}}],
                    },
                })
            elif line.startswith("**") and line.endswith("**\n"):
                text = line[2:-3]
                children.append({
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {"rich_text": [{"type": "text", "text": {"content": text}}]},
                })
            else:
                children.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text", "text": {"content": line}}]},
                })

        if not children:
            children.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": "No data"}}]},
            })

        # Delete existing children and append new ones (Notion doesn't have "replace all")
        # Strategy: append blocks, then clean up old ones on next sync
        # For simplicity, we just append. The page will accumulate, but the latest
        # entry is always the current state. A cleanup step can be added later.
        try:
            await self._append_blocks(self._asset_page_id, children)
            stats.asset_summary_updated = True
        except Exception as e:
            stats.errors.append(f"Asset summary: {e}")
            logger.exception("Failed to sync asset summary")

        logger.info("Asset summary sync complete: %s", stats.summary())
        return stats

    # ─── Sync: All ───────────────────────────────────────────────────────

    async def sync_all(self, db_session, since: str | None = None) -> SyncStats:
        """Run all sync operations and return combined stats."""
        combined = SyncStats()

        if not self.configured:
            logger.warning("Notion sync not configured — skipping")
            combined.errors.append("Notion sync not configured")
            return combined

        if self._tx_db_id:
            try:
                tx_stats = await self.sync_transactions(db_session, since=since)
                _merge_stats(combined, tx_stats)
            except Exception as e:
                combined.errors.append(f"Transaction sync failed: {e}")
                logger.exception("Transaction sync failed")

        if self._cf_db_id:
            try:
                cf_stats = await self.sync_cashflow(db_session)
                _merge_stats(combined, cf_stats)
            except Exception as e:
                combined.errors.append(f"Cash flow sync failed: {e}")
                logger.exception("Cash flow sync failed")

        if self._asset_page_id:
            try:
                asset_stats = await self.sync_asset_summary(db_session)
                _merge_stats(combined, asset_stats)
            except Exception as e:
                combined.errors.append(f"Asset summary sync failed: {e}")
                logger.exception("Asset summary sync failed")

        logger.info("Full Notion sync complete: %s", combined.summary())
        return combined

    # ─── Database Setup ──────────────────────────────────────────────────

    async def ensure_databases(self, parent_page_id: str) -> dict[str, str]:
        """Create Notion databases for finance data if they don't exist.

        Returns a dict with keys: transactions_db_id, cashflow_db_id, asset_page_id.

        This is a one-time setup operation. The created database IDs should be
        saved to the configuration.
        """
        result: dict[str, str] = {}

        # Create Transactions database
        if not self._tx_db_id:
            tx_db = await self._request("POST", "/databases", {
                "parent": {"page_id": parent_page_id},
                "title": [{"type": "text", "text": {"content": "💳 交易记录"}}],
                "properties": {
                    "描述": {"title": {}},
                    "金额": {"number": {"format": "number"}},
                    "币种": {"rich_text": {}},
                    "类型": {"select": {"options": [
                        {"name": "expense", "color": "red"},
                        {"name": "income", "color": "green"},
                        {"name": "transfer", "color": "blue"},
                        {"name": "adjustment", "color": "gray"},
                    ]}},
                    "分类": {"select": {}},
                    "账户": {"rich_text": {}},
                    "交易时间": {"date": {}},
                    "Tx ID": {"number": {}},
                    "来源": {"select": {"options": [
                        {"name": "manual"},
                        {"name": "pdf_import"},
                        {"name": "bank_api"},
                        {"name": "mcp_agent"},
                    ]}},
                },
            })
            self._tx_db_id = tx_db["id"]
            result["transactions_db_id"] = tx_db["id"]

        # Create Cash Flow database
        if not self._cf_db_id:
            cf_db = await self._request("POST", "/databases", {
                "parent": {"page_id": parent_page_id},
                "title": [{"type": "text", "text": {"content": "📊 月度现金流"}}],
                "properties": {
                    "Period": {"title": {}},
                    "收入": {"number": {"format": "number"}},
                    "支出": {"number": {"format": "number"}},
                    "储蓄": {"number": {"format": "number"}},
                    "转账": {"number": {"format": "number"}},
                    "基准币种": {"select": {"options": [
                        {"name": "CNY"},
                        {"name": "EUR"},
                        {"name": "USD"},
                    ]}},
                },
            })
            self._cf_db_id = cf_db["id"]
            result["cashflow_db_id"] = cf_db["id"]

        # Create Asset Summary page
        if not self._asset_page_id:
            asset_page = await self._request("POST", "/pages", {
                "parent": {"page_id": parent_page_id},
                "properties": {
                    "title": {"title": [{"text": {"content": "📈 资产汇总"}}]},
                },
            })
            self._asset_page_id = asset_page["id"]
            result["asset_page_id"] = asset_page["id"]

        return result


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _extract_notion_property(page: dict, property_name: str) -> str | None:
    """Extract a property value from a Notion page object."""
    props = page.get("properties", {})
    prop = props.get(property_name, {})
    prop_type = prop.get("type")

    if prop_type == "title":
        parts = prop.get("title", [])
        return "".join(p.get("plain_text", "") for p in parts) if parts else None
    elif prop_type == "rich_text":
        parts = prop.get("rich_text", [])
        return "".join(p.get("plain_text", "") for p in parts) if parts else None
    elif prop_type == "number":
        val = prop.get("number")
        return str(val) if val is not None else None
    elif prop_type == "select":
        sel = prop.get("select")
        return sel.get("name") if sel else None
    elif prop_type == "date":
        d = prop.get("date")
        return d.get("start") if d else None
    return None


def _build_transaction_properties(tx, account, category) -> dict:
    """Build Notion page properties from a Transaction model."""
    desc = tx.description or tx.raw_description or f"Transaction #{tx.id}"
    # Truncate to Notion's 2000 char limit for title
    if len(desc) > 2000:
        desc = desc[:1997] + "..."

    props: dict[str, Any] = {
        "描述": {"title": [{"text": {"content": desc}}]},
        "金额": {"number": float(tx.amount)},
        "币种": {"rich_text": [{"text": {"content": tx.currency}}]},
        "类型": {"select": {"name": tx.type}},
        "交易时间": {"date": {"start": tx.occurred_at[:10] if len(tx.occurred_at) >= 10 else tx.occurred_at}},
        "Tx ID": {"number": tx.id},
        "来源": {"select": {"name": tx.source}},
    }

    if account:
        acct_text = f"{account.name}"
        if account.institution:
            acct_text += f" ({account.institution})"
        props["账户"] = {"rich_text": [{"text": {"content": acct_text[:2000]}}]}

    if category:
        props["分类"] = {"select": {"name": category.name}}

    return props


def _build_cashflow_properties(snap) -> dict:
    """Build Notion page properties from a CashFlowSnapshot model."""
    period_key = f"{snap.period_year}-{snap.period_month:02d}"
    return {
        "Period": {"title": [{"text": {"content": period_key}}]},
        "收入": {"number": float(snap.income_total)},
        "支出": {"number": float(snap.expense_total)},
        "储蓄": {"number": float(snap.savings_total)},
        "转账": {"number": float(snap.transfer_total)},
        "基准币种": {"select": {"name": snap.base_currency}},
    }


def _merge_stats(target: SyncStats, source: SyncStats) -> None:
    """Merge source stats into target."""
    target.transactions_created += source.transactions_created
    target.transactions_updated += source.transactions_updated
    target.transactions_skipped += source.transactions_skipped
    target.asset_summary_updated = target.asset_summary_updated or source.asset_summary_updated
    target.cashflow_entries_created += source.cashflow_entries_created
    target.cashflow_entries_skipped += source.cashflow_entries_skipped
    target.errors.extend(source.errors)


async def asyncio_sleep(seconds: float) -> None:
    """Import asyncio.sleep at runtime to avoid issues."""
    import asyncio
    await asyncio.sleep(seconds)
