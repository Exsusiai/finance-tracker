"""Import a parsed CSV statement into the ledger, with cross-upload dedup.

Shared by the ``/statements/upload-csv`` endpoint and the one-time PayPal
back-fill. Unlike the PDF flow (whole-file hash dedup), CSV uploads are
expected to OVERLAP in date range — the user may export several months at
once and re-upload overlapping windows. So we dedup at the ROW level using
each row's ``external_id`` (PayPal's stable Transaction ID): any row whose
external_id already exists (live) on the account is skipped.

Returns a summary dict the caller can surface to the UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Transaction
from app.services.ingestion import ingest_transactions

logger = structlog.get_logger(__name__)


@dataclass
class CsvImportSummary:
    detected_source: str | None = None
    parsed: int = 0
    imported: int = 0
    skipped_duplicate: int = 0
    skipped_no_external_id: int = 0
    error: str | None = None
    period: str | None = None
    opening_balance_seeded: str | None = None
    new_ids: list[int] = field(default_factory=list)


async def import_csv_rows(
    db: AsyncSession,
    account_id: int,
    parse_result: dict,
) -> CsvImportSummary:
    """Insert the parser's transaction dicts into ``account_id``, skipping rows
    whose ``external_id`` already exists on the account. Caller commits."""
    summary = CsvImportSummary(
        detected_source=parse_result.get("detected_source"),
        period=parse_result.get("statement_period"),
    )
    if parse_result.get("error"):
        summary.error = parse_result["error"]
        return summary

    rows = parse_result.get("transactions", [])
    summary.parsed = len(rows)
    if not rows:
        return summary

    # Existing external_ids on this account (live rows only) → dedup set.
    existing = set(
        (
            await db.execute(
                select(Transaction.external_id).where(
                    Transaction.account_id == account_id,
                    Transaction.deleted_at.is_(None),
                    Transaction.external_id.isnot(None),
                )
            )
        ).scalars().all()
    )

    # Seed the opening balance on the VERY FIRST import so the account balance
    # matches the provider's real balance (we only import a window; whatever
    # was in the account before it is the opening balance). Guarded to a fresh,
    # unseeded account so later/overlapping uploads never shift it.
    opening = parse_result.get("opening_balance")
    if opening is not None and not existing:
        from app.models import Account
        acct = (
            await db.execute(select(Account).where(Account.id == account_id))
        ).scalar_one_or_none()
        if acct is not None and Decimal(str(acct.initial_balance or 0)) == 0:
            any_tx = (
                await db.execute(
                    select(Transaction.id)
                    .where(Transaction.account_id == account_id,
                           Transaction.deleted_at.is_(None))
                    .limit(1)
                )
            ).first()
            if any_tx is None:
                acct.initial_balance = Decimal(opening)
                summary.opening_balance_seeded = opening

    new_txs: list[Transaction] = []
    seen_in_batch: set[str] = set()
    for r in rows:
        ext = r.get("external_id")
        if not ext:
            summary.skipped_no_external_id += 1
            continue
        if ext in existing or ext in seen_in_batch:
            summary.skipped_duplicate += 1
            continue
        seen_in_batch.add(ext)
        new_txs.append(
            Transaction(
                account_id=account_id,
                occurred_at=r.get("occurred_at", ""),
                amount=Decimal(str(r.get("amount", 0))),
                currency=r.get("currency", "EUR"),
                type=r.get("type", "expense"),
                description=r.get("description"),
                raw_description=r.get("raw_description"),
                counterparty=r.get("counterparty"),
                # Reuse the statement-import source tag (LLM-eligible, same as
                # PDF imports). CSV imports carry no pdf_import_id.
                source="pdf_import",
                external_id=ext,
                metadata_json=r.get("metadata_json"),
                is_pending=True,
            )
        )

    if new_txs:
        db.add_all(new_txs)  # ingest_transactions expects rows already attached
        # auto_pair=False: cross-account pairing is handled explicitly (the
        # PayPal funding/withdrawal legs pair with bank records during
        # reconciliation). Running the fuzzy matcher here risks mis-pairing
        # same-amount/same-day PayPal-internal legs (e.g. the refund cluster).
        await ingest_transactions(db, new_txs, auto_pair=False)
        summary.new_ids = [t.id for t in new_txs if t.id is not None]

    summary.imported = len(new_txs)
    logger.info(
        "csv_import_complete",
        account_id=account_id,
        parsed=summary.parsed,
        imported=summary.imported,
        skipped_duplicate=summary.skipped_duplicate,
    )
    return summary
