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

import json
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

    # Existing rows on this account (live only). We need CONTENT, not just the
    # external_id string: PayPal reuses a Transaction ID for related events
    # (a withdrawal and its later reversal), and the parser's per-file `#N`
    # suffix is only stable WITHIN one export. When the two events land in
    # different export windows each gets the bare ID, so pure string dedup
    # skipped the second, distinct event as a "duplicate" (V9-F12). Identity =
    # base provider ID + the stable event fields (timestamp, amount, type,
    # direction).
    existing_rows = (
        await db.execute(
            select(
                Transaction.external_id,
                Transaction.occurred_at,
                Transaction.amount,
                Transaction.type,
                Transaction.metadata_json,
            ).where(
                Transaction.account_id == account_id,
                Transaction.deleted_at.is_(None),
                Transaction.external_id.isnot(None),
            )
        )
    ).all()
    existing = {row[0] for row in existing_rows}

    def _direction(meta_json: str | None) -> str | None:
        if not meta_json:
            return None
        try:
            m = json.loads(meta_json)
            return m.get("transfer_direction") if isinstance(m, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None

    def _content_key(ext: str, occurred_at: str, amount, tx_type: str, meta_json: str | None):
        base = ext.split("#", 1)[0]
        amt = Decimal(str(amount or 0)).copy_abs().normalize()
        return (base, occurred_at, amt, tx_type, _direction(meta_json))

    existing_content = {
        _content_key(row[0], row[1], row[2], row[3], row[4]) for row in existing_rows
    }

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
    batch_content: set = set()
    for r in rows:
        ext = r.get("external_id")
        if not ext:
            summary.skipped_no_external_id += 1
            continue
        key = _content_key(
            ext, r.get("occurred_at", ""), r.get("amount", 0),
            r.get("type", "expense"), r.get("metadata_json"),
        )
        # Duplicate = same provider ID AND same event content. A reused ID
        # with different content (the reversal of an earlier withdrawal) is a
        # distinct event and must import — under a free suffix so the partial
        # unique index (account_id, external_id) stays satisfied.
        if key in existing_content or key in batch_content:
            summary.skipped_duplicate += 1
            continue
        if ext in existing or ext in seen_in_batch:
            base = ext.split("#", 1)[0]
            n = 2
            while f"{base}#{n}" in existing or f"{base}#{n}" in seen_in_batch:
                n += 1
            ext = f"{base}#{n}"
            r = {**r, "external_id": ext}
        seen_in_batch.add(ext)
        batch_content.add(key)
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
