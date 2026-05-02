"""Finance Tracker MCP Server — tools for AI agents to query and manage personal finances."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

# ─── Inject backend into sys.path ───────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_BACKEND_DIR = _PROJECT_ROOT / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# Now we can import backend modules
from app.core.config import get_settings  # noqa: E402
from app.db.session import Base  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    Asset,
    AssetHolding,
    Category,
    FxRate,
    MarketPrice,
    PdfImport,
    Transaction,
)

# ─── Database setup (sync, for MCP stdio) ───────────────────────────────────
import sqlite3  # noqa: E402

settings = get_settings()
_DB_PATH = settings.db_path


def _get_conn() -> sqlite3.Connection:
    """Get a SQLite connection with pragmas."""
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


# ─── Helpers ────────────────────────────────────────────────────────────────

def _dec(val) -> str:
    """Normalize Decimal/float/int to clean string."""
    if val is None:
        return "0"
    d = Decimal(str(val))
    n = d.normalize()
    sign, digits, exponent = n.as_tuple()
    if exponent >= 0:
        return str(int(n))
    return format(n, "f")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── MCP Server ─────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="finance-tracker",
    instructions=(
        "Personal finance management tools. Query assets, transactions, cash flow, "
        "and parse bank statement PDFs. All amounts are strings preserving decimal precision. "
        "Currency defaults to CNY unless specified."
    ),
)


# ─── Tool 1: get_total_assets ───────────────────────────────────────────────

@mcp.tool(
    name="get_total_assets",
    description="Query total asset valuation across all accounts and holdings. Returns balances by account, portfolio value by asset class and currency.",
)
async def get_total_assets(
    currency: str | None = Field(
        None,
        description="Target currency for conversion (e.g. 'CNY', 'EUR'). Defaults to base_currency.",
    ),
) -> dict[str, Any]:
    conn = _get_conn()
    try:
        base_currency = currency or settings.base_currency

        # 1. Account balances
        rows = conn.execute("""
            SELECT account_id, account_name, currency, balance
            FROM v_account_balance
        """).fetchall()
        accounts = [
            {"account_id": r["account_id"], "account_name": r["account_name"],
             "currency": r["currency"], "balance": _dec(r["balance"])}
            for r in rows
        ]

        # 2. Portfolio holdings value
        holdings = conn.execute("""
            SELECT ah.id, ah.quantity, ah.avg_cost, ah.cost_currency,
                   a.id AS asset_id, a.symbol, a.name, a.asset_class, a.currency AS asset_currency,
                   mp.price, mp.currency AS price_currency
            FROM asset_holdings ah
            JOIN assets a ON ah.asset_id = a.id
            LEFT JOIN (
                SELECT asset_id, price, currency
                FROM market_prices mp1
                WHERE quoted_at = (SELECT MAX(quoted_at) FROM market_prices mp2 WHERE mp2.asset_id = mp1.asset_id)
            ) mp ON mp.asset_id = a.id
        """).fetchall()

        total_portfolio = Decimal("0")
        by_class: dict[str, Decimal] = {}
        by_currency: dict[str, Decimal] = {}

        for h in holdings:
            if h["price"] is None:
                continue
            value = Decimal(str(h["quantity"])) * Decimal(str(h["price"]))
            price_cur = h["price_currency"]

            # Convert to base currency if needed
            if price_cur and price_cur != base_currency:
                fx = conn.execute("""
                    SELECT rate FROM fx_rates
                    WHERE base_currency = ? AND quote_currency = ?
                    ORDER BY quoted_at DESC LIMIT 1
                """, (base_currency, price_cur)).fetchone()
                if fx:
                    value = value * Decimal(str(fx["rate"]))
                else:
                    continue  # skip if no FX rate

            total_portfolio += value
            ac = h["asset_class"] or "other"
            by_class[ac] = by_class.get(ac, Decimal("0")) + value
            by_currency[price_cur or "unknown"] = by_currency.get(price_cur or "unknown", Decimal("0")) + value

        # 3. Cash account balance total
        total_cash = Decimal("0")
        cash_by_currency: dict[str, Decimal] = {}
        for a in accounts:
            amt = Decimal(a["balance"])
            cur = a["currency"]
            if cur == base_currency:
                total_cash += amt
            else:
                fx = conn.execute("""
                    SELECT rate FROM fx_rates
                    WHERE base_currency = ? AND quote_currency = ?
                    ORDER BY quoted_at DESC LIMIT 1
                """, (base_currency, cur)).fetchone()
                if fx:
                    converted = amt * Decimal(str(fx["rate"]))
                    total_cash += converted
                else:
                    total_cash += amt  # keep original
            cash_by_currency[cur] = cash_by_currency.get(cur, Decimal("0")) + amt

        total_assets = total_cash + total_portfolio

        return {
            "success": True,
            "data": {
                "total_assets": _dec(total_assets),
                "base_currency": base_currency,
                "as_of": _now_iso(),
                "cash": {
                    "total": _dec(total_cash),
                    "by_currency": {k: _dec(v) for k, v in cash_by_currency.items()},
                    "accounts": accounts,
                },
                "portfolio": {
                    "total": _dec(total_portfolio),
                    "by_class": {k: _dec(v) for k, v in by_class.items()},
                    "by_currency": {k: _dec(v) for k, v in by_currency.items()},
                },
            },
        }
    finally:
        conn.close()


# ─── Tool 2: get_transactions ──────────────────────────────────────────────

@mcp.tool(
    name="get_transactions",
    description="Query transaction records with flexible filtering. Supports date range, account, category, type, amount range, and pagination.",
)
async def get_transactions(
    account_id: int | None = Field(None, description="Filter by account ID"),
    category_id: int | None = Field(None, description="Filter by category ID"),
    type: str | None = Field(None, description="Filter by type: expense, income, transfer, adjustment"),
    from_date: str | None = Field(None, description="Start date (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ)"),
    to_date: str | None = Field(None, description="End date (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ)"),
    min_amount: float | None = Field(None, description="Minimum amount (absolute value)"),
    max_amount: float | None = Field(None, description="Maximum amount (absolute value)"),
    source: str | None = Field(None, description="Filter by source: manual, pdf_import, bank_api, mcp_agent"),
    is_pending: bool | None = Field(None, description="Filter pending transactions only"),
    limit: int = Field(50, description="Max results (1-200)", ge=1, le=200),
    offset: int = Field(0, description="Skip N results", ge=0),
) -> dict[str, Any]:
    conn = _get_conn()
    try:
        conditions = ["t.deleted_at IS NULL"]
        params: list[Any] = []

        if account_id is not None:
            conditions.append("t.account_id = ?")
            params.append(account_id)
        if category_id is not None:
            conditions.append("t.category_id = ?")
            params.append(category_id)
        if type is not None:
            conditions.append("t.type = ?")
            params.append(type)
        if from_date is not None:
            conditions.append("t.occurred_at >= ?")
            params.append(from_date)
        if to_date is not None:
            # If only date, append end-of-day
            if len(to_date) == 10:
                to_date += "T23:59:59Z"
            conditions.append("t.occurred_at <= ?")
            params.append(to_date)
        if min_amount is not None:
            conditions.append("ABS(t.amount) >= ?")
            params.append(str(min_amount))
        if max_amount is not None:
            conditions.append("ABS(t.amount) <= ?")
            params.append(str(max_amount))
        if source is not None:
            conditions.append("t.source = ?")
            params.append(source)
        if is_pending is not None:
            conditions.append("t.is_pending = ?")
            params.append(1 if is_pending else 0)

        where = " AND ".join(conditions)

        # Count
        count_row = conn.execute(
            f"SELECT COUNT(*) FROM transactions t WHERE {where}", params
        ).fetchone()
        total = count_row[0]

        # Fetch
        rows = conn.execute(f"""
            SELECT t.id, t.account_id, a.name AS account_name,
                   t.counter_account_id, t.category_id, c.name AS category_name,
                   t.occurred_at, t.posted_at, t.amount, t.currency,
                   t.fx_rate_to_base, t.base_amount, t.type, t.description,
                   t.raw_description, t.counterparty, t.location, t.tags_json,
                   t.source, t.is_pending, t.created_at, t.updated_at
            FROM transactions t
            LEFT JOIN accounts a ON t.account_id = a.id
            LEFT JOIN categories c ON t.category_id = c.id
            WHERE {where}
            ORDER BY t.occurred_at DESC, t.id DESC
            LIMIT ? OFFSET ?
        """, params + [limit, offset]).fetchall()

        transactions = []
        for r in rows:
            tags = []
            if r["tags_json"]:
                try:
                    tags = json.loads(r["tags_json"])
                except (json.JSONDecodeError, TypeError):
                    pass
            transactions.append({
                "id": r["id"],
                "account_id": r["account_id"],
                "account_name": r["account_name"],
                "category_id": r["category_id"],
                "category_name": r["category_name"],
                "occurred_at": r["occurred_at"],
                "amount": _dec(r["amount"]),
                "currency": r["currency"],
                "type": r["type"],
                "description": r["description"],
                "counterparty": r["counterparty"],
                "tags": tags,
                "source": r["source"],
                "is_pending": bool(r["is_pending"]),
            })

        return {
            "success": True,
            "data": {
                "transactions": transactions,
                "total": total,
                "limit": limit,
                "offset": offset,
                "has_more": (offset + limit) < total,
            },
        }
    finally:
        conn.close()


# ─── Tool 3: add_transaction ────────────────────────────────────────────────

@mcp.tool(
    name="add_transaction",
    description="Manually add a new transaction record. Used for quick bookkeeping by the agent.",
)
async def add_transaction(
    account_id: int = Field(..., description="Account ID for the transaction"),
    amount: str = Field(..., description="Amount as decimal string (positive). Use negative for expenses."),
    currency: str = Field("CNY", description="Currency code (e.g. CNY, EUR, USD)"),
    type: str = Field(..., description="Transaction type: expense, income, transfer, adjustment"),
    occurred_at: str = Field(None, description="When it happened (ISO format). Defaults to now."),
    description: str | None = Field(None, description="Transaction description"),
    counterparty: str | None = Field(None, description="Counterparty name"),
    category_id: int | None = Field(None, description="Category ID"),
    tags: list[str] | None = Field(None, description="List of tags"),
    is_pending: bool = Field(False, description="Mark as pending (unconfirmed)"),
) -> dict[str, Any]:
    conn = _get_conn()
    try:
        now = _now_iso()
        occurred = occurred_at or now
        tags_json = json.dumps(tags) if tags else None

        cur = conn.execute("""
            INSERT INTO transactions
                (account_id, amount, currency, type, occurred_at, description,
                 counterparty, category_id, tags_json, source, is_pending,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'mcp_agent', ?, ?, ?)
        """, (
            account_id, str(Decimal(amount)), currency, type, occurred,
            description, counterparty, category_id, tags_json,
            1 if is_pending else 0, now, now,
        ))
        conn.commit()
        tx_id = cur.lastrowid

        return {
            "success": True,
            "data": {
                "id": tx_id,
                "account_id": account_id,
                "amount": amount,
                "currency": currency,
                "type": type,
                "occurred_at": occurred,
                "description": description,
                "source": "mcp_agent",
                "is_pending": is_pending,
            },
        }
    except Exception as e:
        conn.rollback()
        return {"success": False, "error": str(e)}
    finally:
        conn.close()


# ─── Tool 4: parse_bank_statement ───────────────────────────────────────────

@mcp.tool(
    name="parse_bank_statement",
    description="Parse an uploaded bank statement PDF. Returns detected bank, extracted transactions, and parsing status. Call with a file path to a PDF.",
)
async def parse_bank_statement(
    file_path: str = Field(..., description="Absolute path to the PDF bank statement file"),
    account_id: int | None = Field(None, description="Account ID to associate transactions with"),
    auto_confirm: bool = Field(
        False,
        description="If True, automatically confirm all parsed transactions (mark as non-pending).",
    ),
) -> dict[str, Any]:
    conn = _get_conn()
    try:
        # Read file
        path = Path(file_path)
        if not path.exists():
            return {"success": False, "error": f"File not found: {file_path}"}

        content = path.read_bytes()
        if not content:
            return {"success": False, "error": "Empty file"}

        file_hash = hashlib.sha256(content).hexdigest()

        # Check duplicate
        existing = conn.execute(
            "SELECT id FROM pdf_imports WHERE file_hash = ?", (file_hash,)
        ).fetchone()
        if existing:
            return {"success": False, "error": f"PDF already imported (import_id={existing['id']})"}

        # Store PDF
        storage_dir = settings.pdf_storage_dir
        storage_dir.mkdir(parents=True, exist_ok=True)
        storage_path = storage_dir / f"{file_hash}.pdf"
        with open(storage_path, "wb") as f:
            f.write(content)

        now = _now_iso()

        # Create import record
        cur = conn.execute("""
            INSERT INTO pdf_imports
                (filename, file_hash, file_size, storage_path, account_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'parsing', ?, ?)
        """, (path.name, file_hash, len(content), str(storage_path), account_id, now, now))
        import_id = cur.lastrowid

        # Parse using backend engine (sync wrapper)
        try:
            import pdfplumber

            raw_text = ""
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        raw_text += page_text + "\n"

            # Detect bank
            detected_bank = _detect_bank(raw_text)
            statement_period = _detect_period(raw_text)

            # Parse transactions
            if detected_bank:
                transactions = _parse_for_bank(detected_bank, raw_text)
            else:
                transactions = _parse_generic(raw_text)

            # Insert transactions
            tx_ids = []
            for tx_data in transactions:
                tx_cur = conn.execute("""
                    INSERT INTO transactions
                        (account_id, occurred_at, amount, currency, type, description,
                         raw_description, counterparty, source, pdf_import_id, external_id,
                         is_pending, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pdf_import', ?, ?, ?, ?, ?)
                """, (
                    account_id or 0,
                    tx_data.get("occurred_at", now),
                    str(Decimal(str(tx_data.get("amount", 0)))),
                    tx_data.get("currency", "CNY"),
                    tx_data.get("type", "expense"),
                    tx_data.get("description"),
                    tx_data.get("raw_description"),
                    tx_data.get("counterparty"),
                    import_id,
                    tx_data.get("external_id"),
                    0 if auto_confirm else 1,
                    now, now,
                ))
                tx_ids.append(tx_cur.lastrowid)

            # Update import record
            conn.execute("""
                UPDATE pdf_imports SET
                    detected_bank = ?, parser_version = '0.1.0',
                    statement_period = ?, raw_text = ?,
                    transactions_count = ?, status = 'success',
                    updated_at = ?
                WHERE id = ?
            """, (detected_bank, statement_period, raw_text[:10000],
                  len(transactions), now, import_id))

            conn.commit()

            return {
                "success": True,
                "data": {
                    "import_id": import_id,
                    "filename": path.name,
                    "file_size": len(content),
                    "detected_bank": detected_bank,
                    "statement_period": statement_period,
                    "transactions_count": len(transactions),
                    "transactions": transactions[:20],  # Preview first 20
                    "auto_confirmed": auto_confirm,
                    "all_confirmed": auto_confirm,
                },
            }
        except ImportError:
            conn.execute("""
                UPDATE pdf_imports SET status = 'failed', error_message = 'pdfplumber not installed', updated_at = ?
                WHERE id = ?
            """, (now, import_id))
            conn.commit()
            return {"success": False, "error": "pdfplumber not installed"}
        except Exception as e:
            conn.execute("""
                UPDATE pdf_imports SET status = 'failed', error_message = ?, updated_at = ?
                WHERE id = ?
            """, (str(e), now, import_id))
            conn.commit()
            return {"success": False, "error": f"Parse failed: {e}"}

    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        conn.close()


# ─── PDF parsing helpers (mirrored from backend, sync) ─────────────────────

def _detect_bank(text: str) -> str | None:
    text_lower = text.lower()
    markers = {
        "icbc": ["工商银行", "中国工商银行", "icbc"],
        "cmb": ["招商银行", "china merchants bank", "cmb"],
        "ccb": ["建设银行", "中国建设银行", "ccb"],
        "boc": ["中国银行", "bank of china", "boc"],
        "n26": ["n26", "n26 bank"],
        "revolut": ["revolut"],
    }
    for bank, bank_markers in markers.items():
        for m in bank_markers:
            if m.lower() in text_lower:
                return bank
    return None


def _detect_period(text: str) -> str | None:
    import re
    for pat in [r"(\d{4})年(\d{2})月", r"(\d{4})-(\d{2})"]:
        m = re.search(pat, text)
        if m:
            return f"{m.group(1)}-{m.group(2)}"
    return None


def _parse_for_bank(bank: str, text: str) -> list[dict]:
    import re
    parsers = {"icbc": _parse_icbc, "cmb": _parse_cmb, "ccb": _parse_ccb,
               "boc": _parse_boc, "n26": _parse_n26, "revolut": _parse_revolut}
    fn = parsers.get(bank)
    return fn(text) if fn else _parse_generic(text)


def _parse_generic(text: str) -> list[dict]:
    import re
    transactions = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        dm = re.search(r"(\d{4}[/-]\d{2}[/-]\d{2})", line)
        if not dm:
            continue
        date = dm.group(1).replace("/", "-")
        rest = line[dm.end():].strip()
        am = re.search(r"([+-]?\d+\.?\d*)", rest)
        if not am:
            continue
        try:
            amt = float(am.group(1).replace(",", ""))
            if amt == 0:
                continue
            desc = rest[am.end():].strip()
            if not desc:
                continue
            tx_type = "income" if amt > 0 else "expense"
            transactions.append({
                "occurred_at": f"{date}T00:00:00Z",
                "amount": abs(amt),
                "currency": "CNY",
                "type": tx_type,
                "description": desc,
                "raw_description": desc,
                "counterparty": None,
                "external_id": f"pdf_gen_{len(transactions) + 1}",
            })
        except (ValueError, IndexError):
            continue
    return transactions


def _parse_icbc(text: str) -> list[dict]:
    import re
    txs = []
    for m in re.finditer(r"(\d{4}/\d{2}/\d{2})\s*[收支]\s*([+-]?\d+\.?\d*)\s*([^\n]+)", text):
        try:
            amt = float(m.group(2).replace(",", ""))
            if amt == 0:
                continue
            tx_type = "income" if amt > 0 else "expense"
            txs.append({"occurred_at": f"{m.group(1).replace('/', '-')}T00:00:00Z",
                        "amount": abs(amt), "currency": "CNY", "type": tx_type,
                        "description": m.group(3).strip(), "raw_description": m.group(3).strip(),
                        "counterparty": "ICBC", "external_id": f"icbc_{len(txs) + 1}"})
        except (ValueError, IndexError):
            continue
    return txs


def _parse_cmb(text: str) -> list[dict]:
    import re
    txs = []
    for m in re.finditer(r"(\d{4}-\d{2}-\d{2})\s*[存取收付]\s*([+-]?\d+\.?\d*)\s*([^\n]+)", text):
        try:
            amt = float(m.group(2).replace(",", ""))
            if amt == 0:
                continue
            tx_type = "income" if amt > 0 else "expense"
            txs.append({"occurred_at": f"{m.group(1)}T00:00:00Z",
                        "amount": abs(amt), "currency": "CNY", "type": tx_type,
                        "description": m.group(3).strip(), "raw_description": m.group(3).strip(),
                        "counterparty": "CMB", "external_id": f"cmb_{len(txs) + 1}"})
        except (ValueError, IndexError):
            continue
    return txs


def _parse_ccb(text: str) -> list[dict]:
    import re
    txs = []
    for m in re.finditer(r"(\d{8})\s*[收支]\s*(\d+\.?\d*)\s*([^\n]+)", text):
        try:
            ds = m.group(1)
            date = f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}"
            amt = float(m.group(2).replace(",", ""))
            if amt == 0:
                continue
            tx_type = "income" if "收" in text[m.start():m.start()+30] else "expense"
            txs.append({"occurred_at": f"{date}T00:00:00Z",
                        "amount": abs(amt), "currency": "CNY", "type": tx_type,
                        "description": m.group(3).strip(), "raw_description": m.group(3).strip(),
                        "counterparty": "CCB", "external_id": f"ccb_{len(txs) + 1}"})
        except (ValueError, IndexError):
            continue
    return txs


def _parse_boc(text: str) -> list[dict]:
    import re
    txs = []
    for m in re.finditer(r"(\d{4}-\d{2}-\d{2})\s*[收入支出]\s*(\d+\.?\d*)\s*([^\n]+)", text):
        try:
            amt = float(m.group(2).replace(",", ""))
            if amt == 0:
                continue
            tx_type = "income" if "收入" in text[m.start():m.start()+30] else "expense"
            txs.append({"occurred_at": f"{m.group(1)}T00:00:00Z",
                        "amount": abs(amt), "currency": "CNY", "type": tx_type,
                        "description": m.group(3).strip(), "raw_description": m.group(3).strip(),
                        "counterparty": "BOC", "external_id": f"boc_{len(txs) + 1}"})
        except (ValueError, IndexError):
            continue
    return txs


def _parse_n26(text: str) -> list[dict]:
    import re
    txs = []
    for m in re.finditer(r"(\d{4}[/-]\d{2}[/-]\d{2})\s*(DEPOSIT|SPENDING|TRANSFER|PENDING)?\s*([+-]?\d+\.?\d*)\s*([^\n]+)", text):
        try:
            action = m.group(2) or ""
            amt = float(m.group(3).replace(",", ""))
            if amt == 0:
                continue
            tx_type = "income" if action == "DEPOSIT" or (not action and amt > 0) else "expense"
            txs.append({"occurred_at": f"{m.group(1).replace('/', '-')}T00:00:00Z",
                        "amount": abs(amt), "currency": "EUR", "type": tx_type,
                        "description": m.group(4).strip(), "raw_description": m.group(4).strip(),
                        "counterparty": "N26", "external_id": f"n26_{len(txs) + 1}"})
        except (ValueError, IndexError):
            continue
    return txs


def _parse_revolut(text: str) -> list[dict]:
    import re
    txs = []
    for m in re.finditer(r"(\d{4}[/-]\d{2}[/-]\d{2})\s*(Card Payment|Top-Up|Exchange|Transfer|ATM Withdrawal)?\s*([+-]?\d+\.?\d*)\s*([^\n]+)", text):
        try:
            action = m.group(2) or ""
            amt = float(m.group(3).replace(",", ""))
            if amt == 0:
                continue
            tx_type = "income" if action in ("Top-Up",) or (not action and amt > 0) else "expense"
            txs.append({"occurred_at": f"{m.group(1).replace('/', '-')}T00:00:00Z",
                        "amount": abs(amt), "currency": "EUR", "type": tx_type,
                        "description": m.group(4).strip(), "raw_description": m.group(4).strip(),
                        "counterparty": "Revolut", "external_id": f"revolut_{len(txs) + 1}"})
        except (ValueError, IndexError):
            continue
    return txs


# ─── Tool 5: get_cashflow ──────────────────────────────────────────────────

@mcp.tool(
    name="get_cashflow",
    description="Query monthly cash flow summary — income, expense, savings, and per-category breakdown.",
)
async def get_cashflow(
    from_period: str | None = Field(None, description="Start period (YYYY-MM)"),
    to_period: str | None = Field(None, description="End period (YYYY-MM)"),
    limit: int = Field(12, description="Max months to return (1-60)", ge=1, le=60),
) -> dict[str, Any]:
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT
                substr(occurred_at, 1, 7) AS period,
                SUM(CASE WHEN type = 'income' THEN amount ELSE 0 END) AS income,
                SUM(CASE WHEN type = 'expense' THEN ABS(amount) ELSE 0 END) AS expense,
                SUM(CASE WHEN type = 'transfer' THEN ABS(amount) ELSE 0 END) AS transfer,
                SUM(CASE WHEN type = 'income' THEN amount WHEN type = 'expense' THEN amount ELSE 0 END) AS savings
            FROM transactions
            WHERE deleted_at IS NULL AND is_pending = 0
              AND (? IS NULL OR substr(occurred_at, 1, 7) >= ?)
              AND (? IS NULL OR substr(occurred_at, 1, 7) <= ?)
            GROUP BY period
            ORDER BY period DESC
            LIMIT ?
        """, (from_period, from_period, to_period, to_period, limit)).fetchall()

        months = []
        for r in rows:
            period = r["period"]
            # Per-category breakdown
            cats = conn.execute("""
                SELECT c.name, c.kind, SUM(t.amount) AS total, COUNT(*) AS cnt
                FROM transactions t
                LEFT JOIN categories c ON t.category_id = c.id
                WHERE t.deleted_at IS NULL AND t.is_pending = 0
                  AND substr(t.occurred_at, 1, 7) = ? AND t.category_id IS NOT NULL
                GROUP BY t.category_id
                ORDER BY ABS(total) DESC
            """, (period,)).fetchall()

            by_category = {c["name"]: _dec(c["total"]) for c in cats if c["name"]}

            months.append({
                "period": period,
                "income": _dec(r["income"]),
                "expense": _dec(r["expense"]),
                "transfer": _dec(r["transfer"]),
                "savings": _dec(r["savings"]),
                "by_category": by_category,
            })

        # Summary stats
        total_income = sum(Decimal(m["income"]) for m in months)
        total_expense = sum(Decimal(m["expense"]) for m in months)
        avg_monthly_expense = total_expense / len(months) if months else Decimal("0")

        return {
            "success": True,
            "data": {
                "months": months,
                "summary": {
                    "total_income": _dec(total_income),
                    "total_expense": _dec(total_expense),
                    "net_savings": _dec(total_income - total_expense),
                    "avg_monthly_expense": _dec(avg_monthly_expense),
                    "months_count": len(months),
                },
            },
        }
    finally:
        conn.close()


# ─── Tool 6: get_asset_allocation ───────────────────────────────────────────

@mcp.tool(
    name="get_asset_allocation",
    description="Query asset allocation breakdown — by asset class (cash, stocks, crypto, gold, etc.) and by currency.",
)
async def get_asset_allocation(
    base_currency: str | None = Field(None, description="Convert all values to this currency. Defaults to base_currency."),
) -> dict[str, Any]:
    conn = _get_conn()
    try:
        bc = base_currency or settings.base_currency

        holdings = conn.execute("""
            SELECT ah.id, ah.quantity, ah.avg_cost, ah.cost_currency,
                   a.id AS asset_id, a.symbol, a.name, a.asset_class,
                   a.currency AS asset_currency,
                   mp.price, mp.currency AS price_currency
            FROM asset_holdings ah
            JOIN assets a ON ah.asset_id = a.id
            LEFT JOIN (
                SELECT asset_id, price, currency
                FROM market_prices mp1
                WHERE quoted_at = (SELECT MAX(quoted_at) FROM market_prices mp2 WHERE mp2.asset_id = mp1.asset_id)
            ) mp ON mp.asset_id = a.id
        """).fetchall()

        total_value = Decimal("0")
        by_class: dict[str, list[dict]] = {}
        by_currency: dict[str, Decimal] = {}

        for h in holdings:
            if h["price"] is None:
                continue

            value = Decimal(str(h["quantity"])) * Decimal(str(h["price"]))
            price_cur = h["price_currency"] or h["asset_currency"]

            # FX conversion
            if price_cur and price_cur != bc:
                fx = conn.execute("""
                    SELECT rate FROM fx_rates
                    WHERE base_currency = ? AND quote_currency = ?
                    ORDER BY quoted_at DESC LIMIT 1
                """, (bc, price_cur)).fetchone()
                if fx:
                    value = value * Decimal(str(fx["rate"]))
                else:
                    continue

            total_value += value
            ac = h["asset_class"] or "other"

            if ac not in by_class:
                by_class[ac] = []
            by_class[ac].append({
                "symbol": h["symbol"],
                "name": h["name"],
                "quantity": _dec(h["quantity"]),
                "price": _dec(h["price"]),
                "price_currency": price_cur,
                "value": _dec(value),
                "avg_cost": _dec(h["avg_cost"]) if h["avg_cost"] else None,
            })

            by_currency[price_cur] = by_currency.get(price_cur, Decimal("0")) + value

        # Calculate percentages
        class_summary = {}
        for ac, items in by_class.items():
            class_total = sum(Decimal(i["value"]) for i in items)
            pct = (class_total / total_value * 100) if total_value > 0 else Decimal("0")
            class_summary[ac] = {
                "total_value": _dec(class_total),
                "percentage": f"{float(pct):.1f}%",
                "count": len(items),
                "assets": items,
            }

        # Cash from accounts
        cash_rows = conn.execute("""
            SELECT currency, SUM(balance) AS total
            FROM v_account_balance
            GROUP BY currency
        """).fetchall()
        cash_total = Decimal("0")
        cash_by_cur = {}
        for cr in cash_rows:
            amt = Decimal(str(cr["total"]))
            cur = cr["currency"]
            if cur != bc:
                fx = conn.execute("""
                    SELECT rate FROM fx_rates WHERE base_currency = ? AND quote_currency = ?
                    ORDER BY quoted_at DESC LIMIT 1
                """, (bc, cur)).fetchone()
                if fx:
                    amt = amt * Decimal(str(fx["rate"]))
                else:
                    continue
            cash_total += amt
            cash_by_cur[cur] = _dec(Decimal(str(cr["total"])))

        grand_total = total_value + cash_total

        return {
            "success": True,
            "data": {
                "base_currency": bc,
                "grand_total": _dec(grand_total),
                "as_of": _now_iso(),
                "cash": {
                    "total": _dec(cash_total),
                    "percentage": f"{float(cash_total / grand_total * 100):.1f}%" if grand_total > 0 else "0%",
                    "by_currency": cash_by_cur,
                },
                "investments": {
                    "total": _dec(total_value),
                    "percentage": f"{float(total_value / grand_total * 100):.1f}%" if grand_total > 0 else "0%",
                    "by_class": class_summary,
                    "by_currency": {k: _dec(v) for k, v in by_currency.items()},
                },
            },
        }
    finally:
        conn.close()


# ─── Tool 7: search_transactions ────────────────────────────────────────────

@mcp.tool(
    name="search_transactions",
    description="Full-text search across transactions. Searches description, counterparty, and raw_description fields.",
)
async def search_transactions(
    query: str = Field(..., description="Search query text (case-insensitive)"),
    account_id: int | None = Field(None, description="Limit search to specific account"),
    from_date: str | None = Field(None, description="Start date filter (YYYY-MM-DD)"),
    to_date: str | None = Field(None, description="End date filter (YYYY-MM-DD)"),
    type: str | None = Field(None, description="Filter: expense, income, transfer"),
    limit: int = Field(20, description="Max results (1-100)", ge=1, le=100),
) -> dict[str, Any]:
    conn = _get_conn()
    try:
        pattern = f"%{query}%"
        conditions = ["t.deleted_at IS NULL"]
        params: list[Any] = []

        # Search across text fields
        conditions.append("(t.description LIKE ? OR t.counterparty LIKE ? OR t.raw_description LIKE ?)")
        params.extend([pattern, pattern, pattern])

        if account_id is not None:
            conditions.append("t.account_id = ?")
            params.append(account_id)
        if from_date is not None:
            conditions.append("t.occurred_at >= ?")
            params.append(from_date)
        if to_date is not None:
            if len(to_date) == 10:
                to_date += "T23:59:59Z"
            conditions.append("t.occurred_at <= ?")
            params.append(to_date)
        if type is not None:
            conditions.append("t.type = ?")
            params.append(type)

        where = " AND ".join(conditions)

        rows = conn.execute(f"""
            SELECT t.id, t.account_id, a.name AS account_name,
                   t.category_id, c.name AS category_name,
                   t.occurred_at, t.amount, t.currency, t.type,
                   t.description, t.counterparty, t.source, t.is_pending
            FROM transactions t
            LEFT JOIN accounts a ON t.account_id = a.id
            LEFT JOIN categories c ON t.category_id = c.id
            WHERE {where}
            ORDER BY t.occurred_at DESC, t.id DESC
            LIMIT ?
        """, params + [limit]).fetchall()

        transactions = []
        for r in rows:
            transactions.append({
                "id": r["id"],
                "account_id": r["account_id"],
                "account_name": r["account_name"],
                "category_name": r["category_name"],
                "occurred_at": r["occurred_at"],
                "amount": _dec(r["amount"]),
                "currency": r["currency"],
                "type": r["type"],
                "description": r["description"],
                "counterparty": r["counterparty"],
                "source": r["source"],
                "is_pending": bool(r["is_pending"]),
            })

        # Aggregate stats
        total_income = sum(Decimal(t["amount"]) for t in transactions if t["type"] == "income")
        total_expense = sum(Decimal(t["amount"]) for t in transactions if t["type"] == "expense")

        return {
            "success": True,
            "data": {
                "query": query,
                "count": len(transactions),
                "total_income": _dec(total_income),
                "total_expense": _dec(total_expense),
                "transactions": transactions,
            },
        }
    finally:
        conn.close()


# ─── Entry Point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
