"""Finance Tracker MCP Server — tools for AI agents to query and manage personal finances.

Two tool families:
  • READ  — registered from ``read_tools.py``; they reuse the backend's **async**
            services / serialize helpers / shared SQL fragments, so MCP numbers
            match the REST API & Web UI by construction (no hand-copied SQL).
  • WRITE — ``add_transaction`` / ``parse_bank_statement`` below; they keep the
            original **sync** ``sqlite3`` path (mirrors backend ingestion
            invariants inline). Untouched by the read refactor.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

# ─── Inject backend into sys.path ───────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_BACKEND_DIR = _PROJECT_ROOT / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402

from . import read_tools  # noqa: E402

settings = get_settings()
_DB_PATH = settings.db_path


# ─── Sync DB helpers (WRITE path only) ──────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    """Get a SQLite connection with pragmas."""
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Sync mirror of backend cashflow recompute SQL (used by the WRITE tools after
# inserts). Mirrors `app.services.cashflow.engine._AMOUNT_BASE_EXPR`: same-currency
# rows bypass FX, foreign rows fall through base_amount → amount*fx_rate, rows
# with neither are NULL (excluded from SUM). %BASE% is spliced with the validated
# ISO-4217 base currency (sqlite3 driver can't bind inside CASE literals).
_AMOUNT_BASE_EXPR_SYNC = (
    "CASE "
    "  WHEN currency = '%BASE%' THEN amount "
    "  WHEN base_amount IS NOT NULL THEN base_amount "
    "  WHEN fx_rate_to_base IS NOT NULL THEN amount * fx_rate_to_base "
    "  ELSE NULL "
    "END"
)


def _recompute_snapshot_sql(base_currency: str) -> str:
    """Build the recompute SQL with base_currency spliced in (validated ISO code)."""
    base = base_currency.upper().replace("'", "")
    expr = _AMOUNT_BASE_EXPR_SYNC.replace("%BASE%", base)
    not_subaccount = (
        "COALESCE("
        "json_valid(metadata_json) AND "
        "json_extract(metadata_json, '$.subaccount') = 1, 0) = 0"
    )
    paired_dedup = (
        "NOT EXISTS (SELECT 1 FROM transactions p "
        "WHERE p.deleted_at IS NULL AND p.id < transactions.id "
        "AND transactions.metadata_json IS NOT NULL AND json_valid(transactions.metadata_json) "
        "AND p.id = json_extract(transactions.metadata_json, '$.paired_with_tx_id'))"
    )
    return f"""
        INSERT OR REPLACE INTO cash_flow_snapshots
            (period_year, period_month, base_currency,
             income_total, expense_total, transfer_total, savings_total, other_total,
             by_category_json, by_account_json, computed_at)
        SELECT
            ?, ?, ?,
            COALESCE(SUM(CASE WHEN type = 'income'  THEN ABS({expr}) ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN type = 'expense' THEN ABS({expr}) ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN type = 'transfer' AND {not_subaccount}
                              THEN ABS({expr}) ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN type = 'income'  THEN  ABS({expr})
                              WHEN type = 'expense' THEN -ABS({expr})
                              ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN type = 'adjustment' THEN {expr} ELSE 0 END), 0),
            NULL,
            NULL,
            ?
        FROM transactions
        WHERE deleted_at IS NULL
          AND is_pending = 0
          AND CAST(substr(occurred_at, 1, 4) AS INTEGER) = ?
          AND CAST(substr(occurred_at, 6, 2) AS INTEGER) = ?
          AND {paired_dedup}
    """


def _recompute_period_sync(conn, year: int, month: int) -> None:
    """Sync mirror of backend recompute_period — used by WRITE tools after inserts."""
    now = _now_iso()
    sql = _recompute_snapshot_sql(settings.base_currency)
    conn.execute(sql, (year, month, settings.base_currency, now, year, month))


def _fx_rate_lookup(conn, base: str, quote: str):
    """Latest fx_rates row for (base→quote) or None."""
    return conn.execute(
        "SELECT rate FROM fx_rates WHERE base_currency = ? AND quote_currency = ? "
        "ORDER BY quoted_at DESC LIMIT 1",
        (base, quote),
    ).fetchone()


def _convert_fx(conn, amount: Decimal, src: str, base: str) -> Decimal | None:
    """Convert ``amount`` from ``src`` → ``base`` (sync, WRITE path).

    Same-currency identity → direct rate → inverse rate → triangulate via
    CNY/USD/EUR. USD-pegged stablecoins are aliased to USD first (wallet_sync
    writes market_prices.currency='USDT'; the fiat FX scheduler never emits USDT
    rows). Returns None when no FX path exists.
    """
    _USD_PEGGED = {"USDT", "USDC", "DAI", "BUSD", "TUSD", "FRAX"}
    if src in _USD_PEGGED:
        src = "USD"
    if base in _USD_PEGGED:
        base = "USD"

    if not src or src == base:
        return amount

    direct = _fx_rate_lookup(conn, src, base)
    if direct and direct["rate"]:
        return amount * Decimal(str(direct["rate"]))

    inverse = _fx_rate_lookup(conn, base, src)
    if inverse and inverse["rate"]:
        r = Decimal(str(inverse["rate"]))
        if r > 0:
            return amount / r

    for pivot in ("CNY", "USD", "EUR"):
        if pivot in (src, base):
            continue
        a_direct = _fx_rate_lookup(conn, src, pivot)
        a_inv = _fx_rate_lookup(conn, pivot, src) if not a_direct else None
        a = (
            Decimal(str(a_direct["rate"])) if a_direct
            else (Decimal(1) / Decimal(str(a_inv["rate"]))) if (a_inv and Decimal(str(a_inv["rate"])) > 0)
            else None
        )
        b_direct = _fx_rate_lookup(conn, pivot, base)
        b_inv = _fx_rate_lookup(conn, base, pivot) if not b_direct else None
        b = (
            Decimal(str(b_direct["rate"])) if b_direct
            else (Decimal(1) / Decimal(str(b_inv["rate"]))) if (b_inv and Decimal(str(b_inv["rate"])) > 0)
            else None
        )
        if a and b:
            return amount * a * b

    return None


# ─── MCP Server ─────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="finance-tracker",
    instructions=(
        "Personal finance management tools. Read tools expose the full picture — "
        "accounts, balances, net worth, asset allocation, holdings, every "
        "transaction and its category, cash flow, statements. Amounts are strings "
        "preserving decimal precision; currency defaults to the configured base "
        "(CNY) unless specified."
    ),
)

# Register the complete READ surface (async, backed by backend services).
read_tools.register(mcp)


# ─── WRITE Tool: add_transaction ────────────────────────────────────────────

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

        # Apply the same invariants the backend ingestion service enforces.
        # 1) amount sign: non-adjustment rows are stored as ABS(amount).
        amount_decimal = Decimal(amount)
        if type != "adjustment" and amount_decimal < 0:
            amount_decimal = -amount_decimal
        # 2) category_id ↔ type kind consistency: reject mismatched pairs.
        if category_id is not None:
            row = conn.execute(
                "SELECT kind FROM categories WHERE id = ?", (category_id,)
            ).fetchone()
            if row is None:
                return {"success": False, "error": f"Category {category_id} not found"}
            cat_kind = row["kind"] if isinstance(row, sqlite3.Row) else row[0]
            if cat_kind != type:
                return {
                    "success": False,
                    "error": (
                        f"Category kind '{cat_kind}' does not match transaction type "
                        f"'{type}' (review V1 §P1-4 invariant)."
                    ),
                }

        # Fold foreign-currency rows to base_currency so cashflow doesn't fallback.
        fx_rate_to_base_val = None
        base_amount_val = None
        metadata_json_val = None
        if currency != settings.base_currency and amount_decimal != 0:
            converted = _convert_fx(conn, amount_decimal, currency, settings.base_currency)
            if converted is not None:
                fx_rate_to_base_val = converted / amount_decimal
                base_amount_val = converted
            else:
                metadata_json_val = json.dumps({
                    "fx_missing": True,
                    "fx_src": currency,
                    "fx_base": settings.base_currency,
                })

        cur = conn.execute("""
            INSERT INTO transactions
                (account_id, amount, currency, type, occurred_at, description,
                 counterparty, category_id, tags_json, source, is_pending,
                 fx_rate_to_base, base_amount, metadata_json,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'mcp_agent', ?, ?, ?, ?, ?, ?)
        """, (
            account_id, str(amount_decimal), currency, type, occurred,
            description, counterparty, category_id, tags_json,
            1 if is_pending else 0,
            str(fx_rate_to_base_val) if fx_rate_to_base_val is not None else None,
            str(base_amount_val) if base_amount_val is not None else None,
            metadata_json_val,
            now, now,
        ))
        tx_id = cur.lastrowid

        # Refresh the cashflow snapshot for the affected month.
        if len(occurred) >= 7 and not is_pending:
            try:
                year = int(occurred[0:4])
                month = int(occurred[5:7])
                _recompute_period_sync(conn, year, month)
            except (ValueError, sqlite3.Error):
                pass  # snapshot refresh is best-effort; the row is already inserted

        conn.commit()

        return {
            "success": True,
            "data": {
                "id": tx_id,
                "account_id": account_id,
                "amount": str(amount_decimal),
                "currency": currency,
                "type": type,
                "occurred_at": occurred,
                "description": description,
                "source": "mcp_agent",
                "is_pending": is_pending,
                "fx_rate_to_base": str(fx_rate_to_base_val) if fx_rate_to_base_val is not None else None,
                "base_amount": str(base_amount_val) if base_amount_val is not None else None,
            },
        }
    except Exception as e:
        conn.rollback()
        return {"success": False, "error": str(e)}
    finally:
        conn.close()


# ─── WRITE Tool: parse_bank_statement ───────────────────────────────────────

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
        path = Path(file_path)
        if not path.exists():
            return {"success": False, "error": f"File not found: {file_path}"}

        content = path.read_bytes()
        if not content:
            return {"success": False, "error": "Empty file"}

        MAX_PDF_SIZE_MB = 10
        if len(content) > MAX_PDF_SIZE_MB * 1024 * 1024:
            return {
                "success": False,
                "error": f"PDF too large ({len(content)} bytes; max {MAX_PDF_SIZE_MB} MiB)",
            }
        if not content.startswith(b"%PDF-"):
            return {
                "success": False,
                "error": "Uploaded file is not a valid PDF (missing %PDF- magic bytes)",
            }

        # Resolve account_id: auto-pick when there's exactly one active account.
        if not account_id:
            active = conn.execute(
                "SELECT id, name, type, currency FROM accounts "
                "WHERE deleted_at IS NULL AND is_active = 1"
            ).fetchall()
            if len(active) == 1:
                account_id = active[0]["id"]
            elif len(active) == 0:
                return {"success": False, "error":
                        "No active account exists. Create one first via add_account / Web UI."}
            else:
                return {
                    "success": False,
                    "error": "account_id is required when more than one active account exists.",
                    "available_accounts": [
                        {"id": a["id"], "name": a["name"], "type": a["type"], "currency": a["currency"]}
                        for a in active
                    ],
                }

        file_hash = hashlib.sha256(content).hexdigest()

        existing = conn.execute(
            "SELECT id FROM pdf_imports WHERE file_hash = ?", (file_hash,)
        ).fetchone()
        if existing:
            return {"success": False, "error": f"PDF already imported (import_id={existing['id']})"}

        storage_dir = settings.pdf_storage_dir
        storage_dir.mkdir(parents=True, exist_ok=True)
        storage_path = storage_dir / f"{file_hash}.pdf"
        with open(storage_path, "wb") as f:
            f.write(content)

        now = _now_iso()

        cur = conn.execute("""
            INSERT INTO pdf_imports
                (filename, file_hash, file_size, storage_path, account_id,
                 transactions_count, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, 'parsing', ?, ?)
        """, (path.name, file_hash, len(content), str(storage_path), account_id, now, now))
        import_id = cur.lastrowid

        # Parse using the canonical backend engine (async; await inside FastMCP loop).
        try:
            from app.services.pdf_parser.engine import parse_pdf_statement as _backend_parse

            parse_result = await _backend_parse(None, None, content)  # type: ignore[arg-type]
            raw_text = parse_result.get("raw_text") or ""
            detected_bank = parse_result.get("detected_bank")
            statement_period = parse_result.get("statement_period")
            transactions = parse_result.get("transactions") or []
            if parse_result.get("error"):
                conn.execute(
                    "UPDATE pdf_imports SET status='failed', error_message=?, updated_at=? WHERE id=?",
                    (parse_result["error"], now, import_id),
                )
                conn.commit()
                return {"success": False, "error": parse_result["error"], "import_id": import_id}

            # Mirror the REST ingestion invariants so MCP-driven imports produce
            # the SAME shape of data (metadata, ABS amount, auto-categorise,
            # pre-confirm transfers, recompute affected periods).
            tx_ids = []
            affected_periods: set[tuple[int, int]] = set()
            kind_categories = {
                row["id"]: row["kind"]
                for row in conn.execute("SELECT id, kind FROM categories").fetchall()
            }
            rules = conn.execute(
                "SELECT id, pattern, pattern_type, field, category_id, priority "
                "FROM categorization_rules WHERE enabled = 1 "
                "ORDER BY priority DESC, id"
            ).fetchall()

            def _rule_field(tx_data, field):
                if field == "description":
                    return tx_data.get("description") or ""
                if field == "counterparty":
                    return tx_data.get("counterparty") or ""
                if field == "raw_description":
                    return tx_data.get("raw_description") or ""
                return ""

            def _match(rule, value: str) -> bool:
                if not value:
                    return False
                p = rule["pattern"] or ""
                pt = rule["pattern_type"]
                v = value.lower()
                if pt == "contains":
                    return p.lower() in v
                if pt == "exact":
                    return p.lower() == v
                if pt == "starts_with":
                    return v.startswith(p.lower())
                if pt == "regex":
                    # Mirror backend _safe_regex_search ReDoS protection (1s timeout).
                    import concurrent.futures
                    import re as _re
                    try:
                        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                            fut = ex.submit(_re.search, p, value, _re.IGNORECASE)
                            return bool(fut.result(timeout=1.0))
                    except (_re.error, concurrent.futures.TimeoutError):
                        return False
                return False

            for tx_data in transactions:
                raw_amt = Decimal(str(tx_data.get("amount", 0)))
                tx_type = tx_data.get("type", "expense")
                if tx_type != "adjustment" and raw_amt < 0:
                    raw_amt = -raw_amt

                category_id = tx_data.get("category_id")
                is_pending = 0 if auto_confirm else 1
                if tx_type == "transfer":
                    is_pending = 0
                elif category_id is None:
                    for rule in rules:
                        rule_cat_kind = kind_categories.get(rule["category_id"])
                        if rule_cat_kind != tx_type:
                            continue
                        if _match(rule, _rule_field(tx_data, rule["field"])):
                            category_id = rule["category_id"]
                            is_pending = 0
                            break

                tx_cur = conn.execute("""
                    INSERT INTO transactions
                        (account_id, occurred_at, amount, currency, type, description,
                         raw_description, counterparty, category_id, source, pdf_import_id,
                         external_id, metadata_json, is_pending, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pdf_import', ?, ?, ?, ?, ?, ?)
                """, (
                    account_id,
                    tx_data.get("occurred_at", now),
                    str(raw_amt),
                    tx_data.get("currency", "CNY"),
                    tx_type,
                    tx_data.get("description"),
                    tx_data.get("raw_description"),
                    tx_data.get("counterparty"),
                    category_id,
                    import_id,
                    tx_data.get("external_id"),
                    tx_data.get("metadata_json"),
                    is_pending,
                    now, now,
                ))
                tx_ids.append(tx_cur.lastrowid)

                occ = tx_data.get("occurred_at", now)
                if not is_pending and len(occ) >= 7:
                    try:
                        affected_periods.add((int(occ[0:4]), int(occ[5:7])))
                    except ValueError:
                        pass

            conn.execute("""
                UPDATE pdf_imports SET
                    detected_bank = ?, parser_version = '0.1.0',
                    statement_period = ?, raw_text = ?,
                    transactions_count = ?, status = 'success',
                    updated_at = ?
                WHERE id = ?
            """, (detected_bank, statement_period, raw_text[:10000],
                  len(transactions), now, import_id))

            # Pair +X/−X rows in the same account within ±3 days as in-bank moves
            # (mirror transfer_matcher.detect_same_account_pairs at sync level).
            fresh_rows = conn.execute("""
                SELECT id, occurred_at, amount, type, description, metadata_json
                FROM transactions WHERE pdf_import_id = ?
                ORDER BY occurred_at, id
            """, (import_id,)).fetchall()

            from collections import defaultdict
            from datetime import date as _date
            buckets: dict = defaultdict(list)
            for r in fresh_rows:
                buckets[str(abs(Decimal(r["amount"])))].append(r)

            WINDOW_DAYS = 3
            already_paired: set = set()
            for amt_key, bucket_rows in buckets.items():
                if len(bucket_rows) < 2:
                    continue
                for i, a in enumerate(bucket_rows):
                    if a["id"] in already_paired:
                        continue
                    for b in bucket_rows[i + 1:]:
                        if b["id"] in already_paired:
                            continue
                        if a["type"] == b["type"]:
                            continue
                        try:
                            d_a = _date.fromisoformat(a["occurred_at"][:10])
                            d_b = _date.fromisoformat(b["occurred_at"][:10])
                            if abs((d_a - d_b).days) > WINDOW_DAYS:
                                continue
                        except (ValueError, IndexError):
                            continue
                        for row in (a, b):
                            meta = {}
                            if row["metadata_json"]:
                                try:
                                    meta = json.loads(row["metadata_json"]) or {}
                                except (json.JSONDecodeError, TypeError):
                                    meta = {}
                            if not isinstance(meta, dict):
                                meta = {}
                            meta["subaccount"] = True
                            meta["source"] = "amount_match"
                            conn.execute(
                                "UPDATE transactions SET type='transfer', metadata_json=? WHERE id=?",
                                (json.dumps(meta), row["id"]),
                            )
                        already_paired.update({a["id"], b["id"]})
                        break

            for (year, month) in affected_periods:
                try:
                    _recompute_period_sync(conn, year, month)
                except sqlite3.Error:
                    pass

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
                    "transactions": transactions[:20],
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


# ─── Entry Point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
