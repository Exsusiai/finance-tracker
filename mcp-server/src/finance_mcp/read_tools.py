"""MCP **read** tools — complete read surface backed by the async backend.

Every tool here reuses backend services / serialize helpers / shared SQL
fragments, so the numbers an agent reads are identical to the REST API and Web
UI by construction. Registered onto the FastMCP instance via ``register(mcp)``.

Coverage (read-only; no write tools live here):
  Net worth & allocation : get_net_worth, get_asset_allocation
  Accounts               : list_accounts, get_account
  Holdings               : list_holdings, get_portfolio_value_history
  Categories             : list_categories
  Transactions           : list_transactions, get_transaction, search_transactions, list_inbox
  Cash flow              : get_cashflow, get_cashflow_timeseries, get_cashflow_by_category
  Statements             : list_statements, get_statement
  Explainability / meta  : list_categorization_rules, list_kb_notes, get_market_data
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from pydantic import Field
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from ._backend import base_currency, dec_str, session


def _ok(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data}


def _err(msg: str) -> dict[str, Any]:
    return {"success": False, "error": msg}


def register(mcp) -> None:  # noqa: C901 — registration hub, each tool is small
    # ─── Net worth ───────────────────────────────────────────────────────────
    @mcp.tool(
        name="get_net_worth",
        description=(
            "Total net worth = cash (account balances) + investments (holdings × "
            "latest price), folded to the target currency. Single-sourced with the "
            "REST /portfolio/net-worth endpoint and the snapshot job."
        ),
    )
    async def get_net_worth(
        currency: str | None = Field(None, description="Target currency (default base_currency, e.g. CNY)."),
    ) -> dict[str, Any]:
        from app.services.valuation.net_worth import compute_net_worth

        base = base_currency(currency)
        async with session() as db:
            r = await compute_net_worth(db, base)
        return _ok({
            "base_currency": r.base_currency,
            "net_worth": dec_str(r.net_worth),
            "cash_total": dec_str(r.cash_total),
            "investment_total": dec_str(r.investment_total),
            "cash_by_currency": r.cash_by_currency,
            "investment_by_currency": {
                k: {"original_value": dec_str(v["original_value"]), "base_value": dec_str(v["base_value"])}
                for k, v in r.investment_by_currency.items()
            },
            "as_of": r.as_of,
        })

    # ─── Asset allocation (cash + investment by class, with %) ────────────────
    @mcp.tool(
        name="get_asset_allocation",
        description=(
            "Asset distribution: cash vs each investment asset-class (us_stock, "
            "crypto, fund, …), folded to the target currency with percentages. "
            "Uses the same FX + filters as net worth."
        ),
    )
    async def get_asset_allocation(
        currency: str | None = Field(None, description="Target currency (default base_currency)."),
    ) -> dict[str, Any]:
        from app.models import Account, Asset, AssetHolding, MarketPrice
        from app.services.valuation.fx import convert_to_base
        from app.services.valuation.net_worth import compute_net_worth

        base = base_currency(currency)
        async with session() as db:
            nw = await compute_net_worth(db, base)

            # Per-class breakdown across the WHOLE net worth. Seed with the cash
            # leg, then add each investment holding grouped by asset_class (same
            # filter + shared convert_to_base as net_worth's investment leg, so
            # the buckets sum back to net_worth). A holding whose asset_class is
            # itself 'cash' merges into the same bucket — no duplicate label.
            rows = (await db.execute(
                select(AssetHolding, Asset)
                .join(Asset, AssetHolding.asset_id == Asset.id)
                .join(Account, Account.id == AssetHolding.account_id)
                .where(
                    Account.include_in_total == True,  # noqa: E712
                    Account.deleted_at.is_(None),
                    AssetHolding.is_active == True,  # noqa: E712
                )
            )).all()
            by_class: dict[str, Decimal] = {"cash": nw.cash_total}
            for holding, asset in rows:
                latest = (await db.execute(
                    select(MarketPrice).where(MarketPrice.asset_id == asset.id)
                    .order_by(MarketPrice.quoted_at.desc()).limit(1)
                )).scalar_one_or_none()
                if latest is None:
                    continue
                original = holding.quantity * latest.price
                converted = original if latest.currency == base else await convert_to_base(
                    db, original, latest.currency, base
                )
                if converted is None:
                    continue
                cls = asset.asset_class or "other"
                by_class[cls] = by_class.get(cls, Decimal("0")) + converted

        grand_total = nw.net_worth
        classes = [
            {"asset_class": cls, "value": dec_str(val), "percentage": _pct(val, grand_total)}
            for cls, val in sorted(by_class.items(), key=lambda kv: kv[1], reverse=True)
        ]
        return _ok({
            "base_currency": base,
            "grand_total": dec_str(grand_total),
            "cash_total": dec_str(nw.cash_total),
            "investment_total": dec_str(nw.investment_total),
            "by_class": classes,
            "as_of": nw.as_of,
        })

    # ─── Accounts ─────────────────────────────────────────────────────────────
    @mcp.tool(
        name="list_accounts",
        description=(
            "List every account with full metadata AND its current balance "
            "(bank/cash/credit from the ledger; crypto/exchange/brokerage from "
            "holdings × latest price). Honours include_in_total / sort_order."
        ),
    )
    async def list_accounts(
        active_only: bool = Field(False, description="Only return active (non-archived) accounts."),
    ) -> dict[str, Any]:
        from app.api.v1.accounts import _account_to_out
        from app.models import Account

        async with session() as db:
            stmt = select(Account).where(Account.deleted_at.is_(None))
            if active_only:
                stmt = stmt.where(Account.is_active.is_(True))
            stmt = stmt.order_by(Account.sort_order, Account.id)
            accounts = (await db.execute(stmt)).scalars().all()
            balances = await _balances_by_account(db)
            out = []
            for a in accounts:
                d = _account_to_out(a).model_dump()
                d["balance"] = balances.get(a.id, "0")
                out.append(d)
        return _ok(out)

    @mcp.tool(
        name="get_account",
        description="Single account: full metadata + current balance + (for investment accounts) its holdings.",
    )
    async def get_account(
        account_id: int = Field(..., description="Account ID."),
    ) -> dict[str, Any]:
        from app.api.v1.accounts import _account_to_out
        from app.models import Account

        async with session() as db:
            acc = (await db.execute(
                select(Account).where(Account.id == account_id, Account.deleted_at.is_(None))
            )).scalar_one_or_none()
            if not acc:
                return _err(f"Account {account_id} not found")
            balances = await _balances_by_account(db)
            d = _account_to_out(acc).model_dump()
            d["balance"] = balances.get(acc.id, "0")
            if acc.type in ("brokerage", "crypto_wallet", "exchange"):
                d["holdings"] = await _holdings_list(db, account_id=account_id)
        return _ok(d)

    # ─── Holdings ─────────────────────────────────────────────────────────────
    @mcp.tool(
        name="list_holdings",
        description=(
            "Per-holding detail: quantity, avg cost, latest price, market value, "
            "unrealized P&L, chain, is_active. Optionally filter by account."
        ),
    )
    async def list_holdings(
        account_id: int | None = Field(None, description="Filter by account ID."),
    ) -> dict[str, Any]:
        async with session() as db:
            return _ok(await _holdings_list(db, account_id=account_id))

    @mcp.tool(
        name="get_portfolio_value_history",
        description="Weekly portfolio-value snapshots (cash/investment/net worth), oldest first.",
    )
    async def get_portfolio_value_history() -> dict[str, Any]:
        from app.models import PortfolioSnapshot

        async with session() as db:
            rows = (await db.execute(
                select(PortfolioSnapshot).order_by(PortfolioSnapshot.period)
            )).scalars().all()
        return _ok([
            {"period": s.period, "base_currency": s.base_currency,
             "cash_total": dec_str(s.cash_total), "investment_total": dec_str(s.investment_total),
             "net_worth": dec_str(s.net_worth), "captured_at": s.captured_at}
            for s in rows
        ])

    # ─── Categories ───────────────────────────────────────────────────────────
    @mcp.tool(
        name="list_categories",
        description=(
            "Category taxonomy. tree=true returns a nested top-level→children "
            "structure; tree=false returns a flat list with parent_id. Needed to "
            "interpret each transaction's category."
        ),
    )
    async def list_categories(
        kind: str | None = Field(None, description="Filter: expense, income, transfer."),
        tree: bool = Field(True, description="Nested tree (true) or flat list (false)."),
    ) -> dict[str, Any]:
        from app.api.v1.categories import _cat_to_out
        from app.models import Category

        async with session() as db:
            stmt = select(Category)
            if kind:
                stmt = stmt.where(Category.kind == kind)
            stmt = stmt.order_by(Category.kind, Category.sort_order, Category.id)
            cats = (await db.execute(stmt)).scalars().all()

        if not tree:
            return _ok([_cat_to_out(c).model_dump() for c in cats])

        nodes: dict[int, dict] = {}
        roots: list[dict] = []
        for c in cats:
            nodes[c.id] = {"id": c.id, "name": c.name, "kind": c.kind,
                           "parent_id": c.parent_id, "children": []}
        for c in cats:
            node = nodes[c.id]
            if c.parent_id is None or c.parent_id not in nodes:
                roots.append(node)
            else:
                nodes[c.parent_id]["children"].append(node)
        return _ok(roots)

    # ─── Transactions ─────────────────────────────────────────────────────────
    @mcp.tool(
        name="list_transactions",
        description=(
            "Query transactions with filters (account/category/type/date/amount/"
            "source/pending) + cursor pagination. Each row includes its category."
        ),
    )
    async def list_transactions(
        account_id: int | None = Field(None, description="Filter by account ID."),
        category_id: int | None = Field(None, description="Filter by category ID."),
        type: str | None = Field(None, description="expense, income, transfer, adjustment."),
        from_date: str | None = Field(None, description="Start date YYYY-MM-DD (inclusive)."),
        to_date: str | None = Field(None, description="End date YYYY-MM-DD (inclusive)."),
        min_amount: str | None = Field(None, description="Min amount (signed)."),
        max_amount: str | None = Field(None, description="Max amount (signed)."),
        source: str | None = Field(None, description="manual, pdf_import, bank_api, mcp_agent."),
        is_pending: bool | None = Field(None, description="Only pending (true) / only confirmed (false)."),
        limit: int = Field(50, description="Page size (1-1000).", ge=1, le=1000),
        cursor: int | None = Field(None, description="Return rows with id < cursor (pagination)."),
    ) -> dict[str, Any]:
        from app.api.v1.transactions import _tx_to_out
        from app.models import Transaction

        async with session() as db:
            conds = [Transaction.deleted_at.is_(None)]
            if account_id is not None:
                conds.append(Transaction.account_id == account_id)
            if category_id is not None:
                conds.append(Transaction.category_id == category_id)
            if type is not None:
                conds.append(Transaction.type == type)
            if from_date is not None:
                conds.append(Transaction.occurred_at >= from_date)
            if to_date is not None:
                conds.append(Transaction.occurred_at < to_date + "T23:59:59Z")
            if min_amount is not None:
                conds.append(Transaction.amount >= Decimal(min_amount))
            if max_amount is not None:
                conds.append(Transaction.amount <= Decimal(max_amount))
            if source is not None:
                conds.append(Transaction.source == source)
            if is_pending is not None:
                conds.append(Transaction.is_pending == (1 if is_pending else 0))
            if cursor is not None:
                conds.append(Transaction.id < cursor)

            stmt = (
                select(Transaction)
                .options(selectinload(Transaction.account), selectinload(Transaction.category))
                .where(*conds)
                .order_by(Transaction.occurred_at.desc(), Transaction.id.desc())
                .limit(limit + 1)
            )
            rows = (await db.execute(stmt)).scalars().all()
            has_more = len(rows) > limit
            rows = rows[:limit]
            data = [_tx_to_out(t).model_dump() for t in rows]
        next_cursor = rows[-1].id if (has_more and rows) else None
        return _ok({"transactions": data, "count": len(data),
                    "has_more": has_more, "next_cursor": next_cursor})

    @mcp.tool(
        name="get_transaction",
        description="Full detail of one transaction: counter account, FX, base_amount, split info, LLM reason, metadata.",
    )
    async def get_transaction(
        transaction_id: int = Field(..., description="Transaction ID."),
    ) -> dict[str, Any]:
        from app.api.v1.transactions import _tx_to_out
        from app.models import Transaction

        async with session() as db:
            t = (await db.execute(
                select(Transaction)
                .options(selectinload(Transaction.account), selectinload(Transaction.category))
                .where(Transaction.id == transaction_id, Transaction.deleted_at.is_(None))
            )).scalar_one_or_none()
            if not t:
                return _err(f"Transaction {transaction_id} not found")
            return _ok(_tx_to_out(t).model_dump())

    @mcp.tool(
        name="search_transactions",
        description="Full-text search across description / counterparty / raw_description.",
    )
    async def search_transactions(
        query: str = Field(..., description="Search text (case-insensitive)."),
        account_id: int | None = Field(None, description="Limit to one account."),
        type: str | None = Field(None, description="expense, income, transfer."),
        limit: int = Field(20, description="Max results (1-100).", ge=1, le=100),
    ) -> dict[str, Any]:
        from sqlalchemy import or_

        from app.api.v1.transactions import _tx_to_out
        from app.models import Transaction

        pattern = f"%{query}%"
        async with session() as db:
            conds = [
                Transaction.deleted_at.is_(None),
                or_(
                    Transaction.description.ilike(pattern),
                    Transaction.counterparty.ilike(pattern),
                    Transaction.raw_description.ilike(pattern),
                ),
            ]
            if account_id is not None:
                conds.append(Transaction.account_id == account_id)
            if type is not None:
                conds.append(Transaction.type == type)
            rows = (await db.execute(
                select(Transaction)
                .options(selectinload(Transaction.account), selectinload(Transaction.category))
                .where(*conds)
                .order_by(Transaction.occurred_at.desc(), Transaction.id.desc())
                .limit(limit)
            )).scalars().all()
            data = [_tx_to_out(t).model_dump() for t in rows]
        return _ok({"query": query, "count": len(data), "transactions": data})

    @mcp.tool(
        name="list_inbox",
        description="Transactions awaiting review (pending, uncategorized) — includes any LLM suggestion in metadata.",
    )
    async def list_inbox(
        limit: int = Field(100, description="Max results (1-500).", ge=1, le=500),
    ) -> dict[str, Any]:
        from app.api.v1.transactions import _tx_to_out
        from app.models import Transaction

        async with session() as db:
            rows = (await db.execute(
                select(Transaction)
                .options(selectinload(Transaction.account), selectinload(Transaction.category))
                .where(Transaction.deleted_at.is_(None), Transaction.is_pending.is_(True))
                .order_by(Transaction.occurred_at.desc(), Transaction.id.desc())
                .limit(limit)
            )).scalars().all()
            data = [_tx_to_out(t).model_dump() for t in rows]
        return _ok({"count": len(data), "transactions": data})

    # ─── Cash flow ────────────────────────────────────────────────────────────
    @mcp.tool(
        name="get_cashflow",
        description=(
            "Monthly cash flow: income / expense / transfer / savings + per-category "
            "breakdown, folded to base currency. Single-sourced with REST /cashflow/monthly "
            "(same FX + paired-transfer dedup)."
        ),
    )
    async def get_cashflow(
        from_period: str | None = Field(None, description="Start month YYYY-MM (inclusive)."),
        to_period: str | None = Field(None, description="End month YYYY-MM (inclusive)."),
        limit: int = Field(12, description="Max months (1-120).", ge=1, le=120),
    ) -> dict[str, Any]:
        from app.services.cashflow.engine import (
            _AMOUNT_BASE_EXPR as E,
            _NOT_SUBACCOUNT as NS,
            paired_dedup_predicate as dedup,
        )

        base = base_currency()
        async with session() as db:
            stmt = text(f"""
                SELECT substr(occurred_at, 1, 7) AS period,
                    SUM(CASE WHEN type='income'  THEN ABS({E}) ELSE 0 END) AS income,
                    SUM(CASE WHEN type='expense' THEN ABS({E}) ELSE 0 END) AS expense,
                    SUM(CASE WHEN type='transfer' AND {NS} THEN ABS({E}) ELSE 0 END) AS transfer,
                    SUM(CASE WHEN type='income'  THEN  ABS({E})
                             WHEN type='expense' THEN -ABS({E}) ELSE 0 END) AS savings
                FROM transactions
                WHERE deleted_at IS NULL AND is_pending = 0
                  AND (:start IS NULL OR substr(occurred_at,1,7) >= :start)
                  AND (:end   IS NULL OR substr(occurred_at,1,7) <= :end)
                  AND {dedup("transactions")}
                GROUP BY period ORDER BY period DESC LIMIT :limit
            """)
            rows = (await db.execute(stmt, {
                "start": from_period, "end": to_period, "limit": limit, "base_currency": base,
            })).all()

            months = []
            for r in rows:
                period = r[0]
                cats = (await db.execute(text(f"""
                    SELECT c.name, SUM(ABS({E})) AS total, COUNT(*) AS cnt
                    FROM transactions t LEFT JOIN categories c ON t.category_id = c.id
                    WHERE t.deleted_at IS NULL AND t.is_pending = 0
                      AND substr(t.occurred_at,1,7) = :period AND t.category_id IS NOT NULL
                      AND {dedup("t")}
                    GROUP BY t.category_id ORDER BY total DESC
                """), {"period": period, "base_currency": base})).all()
                months.append({
                    "period": period, "base_currency": base,
                    "income": dec_str(r[1]), "expense": dec_str(r[2]),
                    "transfer": dec_str(r[3]), "savings": dec_str(r[4]),
                    "by_category": {c[0]: dec_str(c[1]) for c in cats if c[0]},
                })
        return _ok({"months": months, "count": len(months)})

    @mcp.tool(
        name="get_cashflow_timeseries",
        description=(
            "Ascending monthly series for charting: income, expense, savings, and "
            "real cash-assets balance (carry-forwarded). Matches the dashboard chart."
        ),
    )
    async def get_cashflow_timeseries(
        from_period: str | None = Field(None, description="Start month YYYY-MM."),
        to_period: str | None = Field(None, description="End month YYYY-MM."),
    ) -> dict[str, Any]:
        from app.services.cashflow.engine import _AMOUNT_BASE_EXPR as E
        from app.services.valuation.cash_history import compute_cash_history

        base = base_currency()
        async with session() as db:
            rows = (await db.execute(text(f"""
                SELECT substr(occurred_at,1,7) AS period,
                    SUM(CASE WHEN type='income'  THEN ABS({E}) ELSE 0 END) AS income,
                    SUM(CASE WHEN type='expense' THEN ABS({E}) ELSE 0 END) AS expense,
                    SUM(CASE WHEN type='income'  THEN  ABS({E})
                             WHEN type='expense' THEN -ABS({E}) ELSE 0 END) AS savings
                FROM transactions
                WHERE deleted_at IS NULL AND is_pending = 0
                  AND (:start IS NULL OR substr(occurred_at,1,7) >= :start)
                  AND (:end   IS NULL OR substr(occurred_at,1,7) <= :end)
                GROUP BY period ORDER BY period ASC
            """), {"start": from_period, "end": to_period, "base_currency": base})).all()

            periods = [r[0] for r in rows]
            cash_hist = await compute_cash_history(db, base)

        cash, i, last = [], 0, "0"
        for p in periods:
            while i < len(cash_hist) and cash_hist[i][0] <= p:
                last = cash_hist[i][1]
                i += 1
            cash.append(last)
        return _ok({
            "base_currency": base, "periods": periods,
            "income": [dec_str(r[1]) for r in rows],
            "expense": [dec_str(r[2]) for r in rows],
            "savings": [dec_str(r[3]) for r in rows],
            "cash": cash,
        })

    @mcp.tool(
        name="get_cashflow_by_category",
        description=(
            "Spending/income grouped by category for a single month (period) or an "
            "inclusive month range (from/to aggregated). Folded to base currency."
        ),
    )
    async def get_cashflow_by_category(
        period: str | None = Field(None, description="Single month YYYY-MM."),
        from_period: str | None = Field(None, description="Range start YYYY-MM (overrides period)."),
        to_period: str | None = Field(None, description="Range end YYYY-MM."),
    ) -> dict[str, Any]:
        from app.services.cashflow.engine import (
            _AMOUNT_BASE_EXPR as E,
            paired_dedup_predicate as dedup,
        )

        if from_period or to_period:
            rf, rt = (from_period or to_period), (to_period or from_period)
        elif period:
            rf = rt = period
        else:
            return _err("Provide `period` or `from_period`/`to_period`")

        base = base_currency()
        async with session() as db:
            rows = (await db.execute(text(f"""
                SELECT c.id, COALESCE(c.name,'Uncategorized'), COALESCE(c.kind,'expense'),
                    SUM(ABS({E})) AS total, COUNT(*) AS cnt
                FROM transactions t LEFT JOIN categories c ON t.category_id = c.id
                WHERE t.deleted_at IS NULL AND t.is_pending = 0
                  AND substr(t.occurred_at,1,7) >= :rf AND substr(t.occurred_at,1,7) <= :rt
                  AND {dedup("t")}
                GROUP BY t.category_id ORDER BY total DESC
            """), {"rf": rf, "rt": rt, "base_currency": base})).all()
        return _ok({
            "base_currency": base, "from": rf, "to": rt,
            "categories": [
                {"category_id": r[0], "category_name": r[1], "kind": r[2],
                 "total": dec_str(r[3]), "count": r[4] or 0}
                for r in rows
            ],
        })

    # ─── Statements (PDF imports) ─────────────────────────────────────────────
    @mcp.tool(
        name="list_statements",
        description="Imported bank-statement PDFs: bank, period, status, transaction count.",
    )
    async def list_statements(
        limit: int = Field(50, description="Max results (1-200).", ge=1, le=200),
        offset: int = Field(0, description="Skip N.", ge=0),
    ) -> dict[str, Any]:
        from app.models import PdfImport

        async with session() as db:
            total = (await db.execute(
                select(text("COUNT(*)")).select_from(PdfImport)
            )).scalar() or 0
            rows = (await db.execute(
                select(PdfImport).order_by(PdfImport.id.desc()).limit(limit).offset(offset)
            )).scalars().all()
            data = [{
                "id": p.id, "filename": p.filename, "detected_bank": p.detected_bank,
                "statement_period": p.statement_period, "status": p.status,
                "transactions_count": p.transactions_count, "account_id": p.account_id,
                "created_at": p.created_at,
            } for p in rows]
        return _ok({"statements": data, "count": len(data), "total": total,
                    "offset": offset, "limit": limit})

    @mcp.tool(
        name="get_statement",
        description="One imported statement: status, detected bank, period, error message if failed.",
    )
    async def get_statement(
        import_id: int = Field(..., description="PDF import ID."),
    ) -> dict[str, Any]:
        from app.models import PdfImport

        async with session() as db:
            p = (await db.execute(
                select(PdfImport).where(PdfImport.id == import_id)
            )).scalar_one_or_none()
            if not p:
                return _err(f"Statement {import_id} not found")
            return _ok({
                "id": p.id, "filename": p.filename, "detected_bank": p.detected_bank,
                "statement_period": p.statement_period, "status": p.status,
                "transactions_count": p.transactions_count, "account_id": p.account_id,
                "error_message": p.error_message, "created_at": p.created_at,
                "updated_at": p.updated_at,
            })

    # ─── Explainability / metadata ────────────────────────────────────────────
    @mcp.tool(
        name="list_categorization_rules",
        description="Active categorization rules (pattern → category) — explains why rows were auto-categorized.",
    )
    async def list_categorization_rules(
        enabled_only: bool = Field(True, description="Only enabled rules."),
    ) -> dict[str, Any]:
        from app.models import CategorizationRule

        async with session() as db:
            stmt = select(CategorizationRule)
            if enabled_only:
                stmt = stmt.where(CategorizationRule.enabled.is_(True))
            stmt = stmt.order_by(CategorizationRule.priority.desc(), CategorizationRule.id)
            rows = (await db.execute(stmt)).scalars().all()
            data = [{
                "id": r.id, "pattern": r.pattern, "pattern_type": r.pattern_type,
                "field": r.field, "category_id": r.category_id, "priority": r.priority,
                "enabled": r.enabled, "requires_llm": getattr(r, "requires_llm", None),
            } for r in rows]
        return _ok({"rules": data, "count": len(data)})

    @mcp.tool(
        name="list_kb_notes",
        description="Categorization knowledge-base notes (user-curated hints used by LLM classification).",
    )
    async def list_kb_notes(
        limit: int = Field(200, description="Max results.", ge=1, le=1000),
    ) -> dict[str, Any]:
        from app.models import CategorizationNote

        async with session() as db:
            rows = (await db.execute(
                select(CategorizationNote).order_by(CategorizationNote.id.desc()).limit(limit)
            )).scalars().all()
            data = [{
                "id": n.id,
                "keyword": getattr(n, "keyword", None),
                "note": getattr(n, "note", None),
                "category_id": getattr(n, "category_id", None),
                "created_at": getattr(n, "created_at", None),
            } for n in rows]
        return _ok({"notes": data, "count": len(data)})

    @mcp.tool(
        name="get_market_data",
        description="Latest market prices (per asset) and FX rates currently stored — the basis for all valuations.",
    )
    async def get_market_data() -> dict[str, Any]:
        from app.models import Asset, FxRate, MarketPrice

        async with session() as db:
            price_rows = (await db.execute(
                select(MarketPrice, Asset)
                .join(Asset, Asset.id == MarketPrice.asset_id)
                .where(MarketPrice.quoted_at == (
                    select(text("MAX(quoted_at)")).select_from(MarketPrice)
                    .where(text("asset_id = market_prices.asset_id")).scalar_subquery()
                ))
            )).all()
            prices = [{
                "asset_id": a.id, "symbol": a.symbol, "name": a.name,
                "asset_class": a.asset_class, "price": dec_str(mp.price),
                "currency": mp.currency, "quoted_at": mp.quoted_at, "source": mp.source,
            } for mp, a in price_rows]

            fx_rows = (await db.execute(
                select(FxRate).order_by(FxRate.base_currency, FxRate.quote_currency, FxRate.quoted_at.desc())
            )).scalars().all()
            seen, fx = set(), []
            for r in fx_rows:
                key = (r.base_currency, r.quote_currency)
                if key in seen:
                    continue
                seen.add(key)
                fx.append({"base_currency": r.base_currency, "quote_currency": r.quote_currency,
                           "rate": dec_str(r.rate), "quoted_at": r.quoted_at})
        return _ok({"prices": prices, "fx_rates": fx})


# ─── Shared helpers (module-level; used by several tools) ────────────────────

def _pct(part: Decimal, whole: Decimal) -> str:
    if not whole or whole == 0:
        return "0%"
    return f"{float(part / whole * 100):.1f}%"


async def _balances_by_account(db) -> dict[int, str]:
    """Current balance per account id, matching REST /accounts/balances exactly
    (ledger for cash accounts; holdings value for snapshot accounts)."""
    from app.services.wallet_sync.holdings_value import (
        compute_brokerage_value_per_account,
        compute_holdings_value_per_account,
    )

    _SNAPSHOT = {"brokerage", "crypto_wallet", "exchange"}
    rows = (await db.execute(text("""
        SELECT v.account_id, v.balance, a.type
        FROM v_account_balance v JOIN accounts a ON a.id = v.account_id
    """))).all()
    crypto_ids = [r[0] for r in rows if r[2] in ("crypto_wallet", "exchange")]
    broker_ids = [r[0] for r in rows if r[2] == "brokerage"]
    crypto_val = await compute_holdings_value_per_account(db, crypto_ids)
    broker_val = await compute_brokerage_value_per_account(db, broker_ids)
    out: dict[int, str] = {}
    for aid, bal, atype in rows:
        ledger = Decimal("0") if atype in _SNAPSHOT else Decimal(str(bal or 0))
        total = ledger + crypto_val.get(aid, Decimal("0")) + broker_val.get(aid, Decimal("0"))
        out[aid] = dec_str(total)
    return out


async def _holdings_list(db, account_id: int | None = None) -> list[dict]:
    """Per-holding detail via the REST serializer (_holding_to_out)."""
    from app.api.v1.holdings import _holding_to_out
    from app.models import AssetHolding, MarketPrice

    stmt = (
        select(AssetHolding)
        .options(selectinload(AssetHolding.account), selectinload(AssetHolding.asset))
        .order_by(AssetHolding.id)
    )
    if account_id is not None:
        stmt = stmt.where(AssetHolding.account_id == account_id)
    holdings = (await db.execute(stmt)).scalars().all()
    out = []
    for h in holdings:
        price, cur = None, None
        if h.asset:
            mp = (await db.execute(
                select(MarketPrice).where(MarketPrice.asset_id == h.asset_id)
                .order_by(MarketPrice.quoted_at.desc()).limit(1)
            )).scalar_one_or_none()
            if mp:
                price, cur = mp.price, mp.currency
        out.append(_holding_to_out(h, h.asset, price, cur).model_dump())
    return out
