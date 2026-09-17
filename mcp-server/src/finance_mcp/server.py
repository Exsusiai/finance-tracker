"""Finance Tracker MCP Server — tools for AI agents to query and manage personal finances.

Two tool families:
  • READ  — registered from ``read_tools.py``; they reuse the backend's **async**
            services / serialize helpers / shared SQL fragments, so MCP numbers
            match the REST API & Web UI by construction (no hand-copied SQL).
  • WRITE — ``add_transaction`` / ``parse_bank_statement`` below; V9-R1 moved
            them onto the same backend **async** services (tx_validation,
            ingestion, statement_import) — the old sync sqlite3 mirror and its
            hand-copied invariants are gone.
"""

from __future__ import annotations

import hashlib
import json
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


# ─── Write-path infrastructure (V9-R1) ─────────────────────────────────────
#
# Write tools now reuse the backend's async services (session factory,
# tx_validation, ingestion, statement_import) exactly like the read tools do —
# the old synchronous sqlite3 mirror (hand-copied sign/kind/FX/cashflow rules)
# is gone, so MCP writes can no longer drift from REST behaviour (review V9
# F17 / R1 / R7). All backend imports happen lazily inside the tools so test
# monkeypatching and import order keep working.

MAX_PDF_SIZE_MB = 10


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
    from app.core.errors import AppError
    from app.db import async_session_factory
    from app.models import Account, Transaction
    from app.services.ingestion import ingest_transactions
    from app.services.tx_validation import (
        reject_snapshot_account,
        validate_account_currency,
        validate_kind_match,
    )

    occurred = occurred_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    async with async_session_factory() as db:
        try:
            acct = (await db.get(Account, account_id))
            if acct is None or acct.deleted_at is not None:
                return {"success": False, "error": f"Account {account_id} not found"}
            # Same write-entrance invariants as every REST route (V9-R1).
            await validate_kind_match(db, tx_type=type, category_id=category_id)
            await reject_snapshot_account(db, account_id)
            await validate_account_currency(db, account_id, currency)

            tx = Transaction(
                account_id=account_id,
                amount=Decimal(amount),
                currency=currency,
                type=type,
                occurred_at=occurred,
                description=description,
                counterparty=counterparty,
                category_id=category_id,
                tags_json=json.dumps(tags) if tags else None,
                source="mcp_agent",
                is_pending=is_pending,
            )
            db.add(tx)
            # Sign normalisation, FX fold, L1 categorisation and cashflow
            # recompute all come from the ONE shared pipeline.
            await ingest_transactions(db, [tx], auto_pair=False)
            await db.commit()
            return {
                "success": True,
                "data": {
                    "id": tx.id,
                    "account_id": tx.account_id,
                    "amount": str(tx.amount),
                    "currency": tx.currency,
                    "type": tx.type,
                    "occurred_at": tx.occurred_at,
                    "description": tx.description,
                    "source": tx.source,
                    "is_pending": bool(tx.is_pending),
                    "category_id": tx.category_id,
                    "fx_rate_to_base": str(tx.fx_rate_to_base) if tx.fx_rate_to_base is not None else None,
                    "base_amount": str(tx.base_amount) if tx.base_amount is not None else None,
                },
            }
        except AppError as e:
            await db.rollback()
            return {"success": False, "error": getattr(e, "message", None) or str(e)}
        except Exception as e:
            await db.rollback()
            return {"success": False, "error": str(e)}


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
    from sqlalchemy import select

    from app.db import async_session_factory
    from app.models import Account, PdfImport

    path = Path(file_path)
    if not path.exists():
        return {"success": False, "error": f"File not found: {file_path}"}
    content = path.read_bytes()
    if not content:
        return {"success": False, "error": "Empty file"}
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

    file_hash = hashlib.sha256(content).hexdigest()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async with async_session_factory() as db:
        # Resolve account_id: auto-pick when there is exactly one active account.
        if not account_id:
            active = (await db.execute(
                select(Account).where(
                    Account.deleted_at.is_(None), Account.is_active == True  # noqa: E712
                )
            )).scalars().all()
            if len(active) == 1:
                account_id = active[0].id
            elif len(active) == 0:
                return {"success": False, "error":
                        "No active account exists. Create one first via add_account / Web UI."}
            else:
                return {
                    "success": False,
                    "error": "account_id is required when more than one active account exists.",
                    "available_accounts": [
                        {"id": a.id, "name": a.name, "type": a.type, "currency": a.currency}
                        for a in active
                    ],
                }

        # V9-F17: failed attempts must not block a clean retry — a failed
        # record carries no ledger rows (writes are atomic below), so purge
        # stale failed records for this hash instead of treating them as
        # "already imported".
        stale_failed = (await db.execute(
            select(PdfImport).where(
                PdfImport.file_hash == file_hash, PdfImport.status == "failed"
            )
        )).scalars().all()
        for rec in stale_failed:
            await db.delete(rec)
        existing = (await db.execute(
            select(PdfImport.id).where(PdfImport.file_hash == file_hash)
        )).scalar_one_or_none()
        if existing is not None:
            await db.commit()  # persist the failed-record purge
            return {"success": False, "error": f"PDF already imported (import_id={existing})"}

        storage_dir = settings.pdf_storage_dir
        storage_dir.mkdir(parents=True, exist_ok=True)
        storage_path = storage_dir / f"{file_hash}.pdf"
        storage_path.write_bytes(content)

        pdf_import = PdfImport(
            filename=path.name,
            file_hash=file_hash,
            file_size=len(content),
            storage_path=str(storage_path),
            account_id=account_id,
            transactions_count=0,
            status="parsing",
        )
        db.add(pdf_import)
        try:
            await db.flush()
            import_id = pdf_import.id

            # Canonical backend parser + the ONE shared insert/ingest service —
            # identical to the REST commit path (V9-R1; late-bound imports keep
            # test monkeypatching working).
            from app.services.pdf_parser import engine as parser_engine
            from app.services.statement_import import (
                insert_parsed_transactions,
                load_subaccount_names,
            )

            sub_names = await load_subaccount_names(db, account_id)
            result = await parser_engine.parse_pdf_statement(
                db, pdf_import, content, subaccount_names=sub_names,
            )
            if result.get("error"):
                pdf_import.status = "failed"
                pdf_import.error_message = str(result["error"])[:500]
                await db.commit()
                return {"success": False, "error": result["error"], "import_id": import_id}

            pdf_import.detected_bank = result.get("detected_bank")
            pdf_import.parser_version = result.get("parser_version")
            pdf_import.statement_period = result.get("statement_period")
            pdf_import.raw_text = (result.get("raw_text") or "")[:10000]

            new_txs = await insert_parsed_transactions(db, pdf_import, result, account_id)
            if auto_confirm and new_txs:
                from app.services.cashflow import parse_period, recompute_for_periods

                periods = set()
                for t in new_txs:
                    t.is_pending = False
                    period = parse_period(t.occurred_at)
                    if period:
                        periods.add(period)
                # Pending-state affects the cashflow snapshots ingest just
                # wrote — recompute the touched months after the flip.
                if periods:
                    await recompute_for_periods(db, periods)

            pdf_import.status = "success"
            pdf_import.transactions_count = len(new_txs)
            await db.commit()

            return {
                "success": True,
                "data": {
                    "import_id": import_id,
                    "filename": path.name,
                    "file_size": len(content),
                    "detected_bank": pdf_import.detected_bank,
                    "statement_period": pdf_import.statement_period,
                    "transactions_count": len(new_txs),
                    "transactions": [
                        {
                            "id": t.id,
                            "occurred_at": t.occurred_at,
                            "amount": str(t.amount),
                            "currency": t.currency,
                            "type": t.type,
                            "description": t.description,
                            "category_id": t.category_id,
                            "is_pending": bool(t.is_pending),
                        }
                        for t in new_txs[:20]
                    ],
                    "auto_confirmed": auto_confirm,
                    "all_confirmed": auto_confirm,
                },
            }
        except Exception as e:
            # V9-F17: the whole attempt rolls back atomically (partial rows AND
            # the in-flight import record), then the failure is recorded in a
            # fresh, self-contained transaction so nothing half-lands.
            await db.rollback()
            try:
                async with async_session_factory() as fail_db:
                    fail_db.add(PdfImport(
                        filename=path.name,
                        file_hash=file_hash,
                        file_size=len(content),
                        storage_path=str(storage_path),
                        account_id=account_id,
                        transactions_count=0,
                        status="failed",
                        error_message=str(e)[:500],
                    ))
                    await fail_db.commit()
            except Exception:  # noqa: BLE001 — failure record is best-effort
                pass
            return {"success": False, "error": f"Parse failed: {e}"}


# ─── Entry Point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
