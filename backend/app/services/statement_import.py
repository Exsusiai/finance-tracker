"""Shared statement-import service (V9-R1).

Turning parser output into ledger rows used to live inside
``api/v1/statements.py`` (``_insert_and_ingest``), which meant the MCP
``parse_bank_statement`` tool had to keep a hand-written synchronous mirror of
the whole pipeline — the recurring drift the V9 review flagged (F17, R7).
This module is the single implementation; REST routes and MCP both call it.

Invariants enforced here (not in each caller):
  - V9-F04 currency-unit guard: every parsed row must match the target
    account's currency, or the whole import is rejected atomically.
  - Rows always enter through ``ingest_transactions`` (sign normalisation,
    L1 categorisation, transfer pairing, cashflow recompute).
"""

from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidInputError
from app.models import Account, PdfImport, Transaction


async def load_subaccount_names(db: AsyncSession, account_id: int | None) -> list[str]:
    """The account's user-maintained sub-account name list (e.g. N26 Spaces),
    used by the parser to tag in-bank moves as balance-neutral."""
    if not account_id:
        return []
    acct = (await db.execute(select(Account).where(Account.id == account_id))).scalar_one_or_none()
    if not (acct and acct.metadata_json):
        return []
    try:
        meta = json.loads(acct.metadata_json)
        raw = meta.get("subaccount_names") if isinstance(meta, dict) else None
        return [str(n) for n in raw if str(n).strip()] if isinstance(raw, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


async def insert_parsed_transactions(
    db: AsyncSession, pdf_import: PdfImport, result: dict, account_id: int
) -> list[Transaction]:
    """Create Transaction rows from parser output + run the ingestion pipeline.

    Shared by REST commit / assign-account / reparse and the MCP
    ``parse_bank_statement`` tool so the insert path stays identical
    (amount normalize → categorize → transfer-match → recompute).
    Returns the created rows (ids populated). Caller commits.
    """
    # V9-F04: parsed rows must match the target account's currency — the
    # balance view sums raw amounts in the account's unit, so importing a
    # USD statement into a EUR account silently mixes units. Reject the
    # whole commit atomically; the user picks the right account instead.
    target_currency = (await db.execute(
        select(Account.currency).where(Account.id == account_id)
    )).scalar_one_or_none()
    if target_currency:
        mismatched = sorted({
            (t.get("currency") or "").upper()
            for t in result.get("transactions", [])
            if t.get("currency") and t["currency"].upper() != target_currency.upper()
        })
        if mismatched:
            raise InvalidInputError(
                f"账单币种 {', '.join(mismatched)} 与目标账户币种 {target_currency} 不一致，"
                "已取消导入。请选择对应币种的账户。",
                details={"account_currency": target_currency, "statement_currencies": mismatched},
            )

    new_txs: list[Transaction] = []
    for tx_data in result.get("transactions", []):
        tx = Transaction(
            account_id=account_id,
            occurred_at=tx_data.get("occurred_at", ""),
            amount=Decimal(str(tx_data.get("amount", 0))),
            currency=tx_data.get("currency", "CNY"),
            type=tx_data.get("type", "expense"),
            description=tx_data.get("description"),
            raw_description=tx_data.get("raw_description"),
            counterparty=tx_data.get("counterparty"),
            source="pdf_import",
            pdf_import_id=pdf_import.id,
            external_id=tx_data.get("external_id"),
            metadata_json=tx_data.get("metadata_json"),
            is_pending=True,
        )
        db.add(tx)
        new_txs.append(tx)
    if new_txs:
        from app.services.ingestion import ingest_transactions

        await ingest_transactions(db, new_txs, auto_pair=True)
    return new_txs
