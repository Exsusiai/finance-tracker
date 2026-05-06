"""Core bank sync engine — orchestrates providers, incremental sync, dedup.

This engine handles:
- Provider instantiation from encrypted credentials
- Incremental transaction sync (only new transactions since last sync)
- Deduplication via external_id
- Transaction mapping from provider format to Finance Tracker format
- Sync scheduling and status tracking
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Account, Transaction
from app.services.bank_sync.providers.base import (
    BalanceInfo,
    BankProvider,
    BankTransaction,
    SyncResult,
)
from app.services.bank_sync.providers.gocardless import GoCardlessProvider
from app.services.bank_sync.crypto import (
    decrypt_credentials,
    encrypt_credentials,
)

logger = logging.getLogger(__name__)


def _utcnow_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _determine_tx_type(amount: Decimal, counterparty: str | None) -> str:
    """Determine transaction type from amount sign and context.

    Negative amount = expense, positive = income.
    Transfers are harder to detect without more context.
    """
    if amount < 0:
        return "expense"
    elif amount > 0:
        return "income"
    return "adjustment"


def _clean_description(raw: str | None) -> str | None:
    """Clean up raw bank description for display."""
    if not raw:
        return None
    # Remove excessive whitespace
    cleaned = " ".join(raw.split())
    # Truncate to 500 chars
    return cleaned[:500] if cleaned else None


class BankSyncEngine:
    """Orchestrates bank data synchronization.

    Usage:
        engine = BankSyncEngine(db_session)
        result = await engine.sync_connection(connection_id=1)
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    # ─── Provider Factory ───────────────────────────────────────────────

    def _create_provider(
        self, provider_name: str, encrypted_creds: str
    ) -> BankProvider:
        """Instantiate a provider from encrypted stored credentials."""
        creds = decrypt_credentials(encrypted_creds)

        if provider_name == "gocardless":
            return GoCardlessProvider(
                secret_id=creds["secret_id"],
                secret_key=creds["secret_key"],
                refresh_token=creds.get("refresh_token"),
            )
        else:
            raise ValueError(f"Unknown provider: {provider_name}")

    def _save_provider_state(
        self, provider_name: str, provider: BankProvider, existing_creds: str
    ) -> str:
        """Re-encrypt credentials with updated state (e.g. new refresh token)."""
        creds = decrypt_credentials(existing_creds)

        if provider_name == "gocardless" and isinstance(provider, GoCardlessProvider):
            if provider.refresh_token:
                creds["refresh_token"] = provider.refresh_token

        return encrypt_credentials(creds)

    # ─── Provider Setup ─────────────────────────────────────────────────

    async def setup_gocardless(
        self, secret_id: str, secret_key: str
    ) -> dict[str, Any]:
        """Verify GoCardless credentials and return initial token info.

        Returns encrypted credentials string for storage.
        """
        provider = GoCardlessProvider(secret_id=secret_id, secret_key=secret_key)
        try:
            access_token = await provider.authenticate()
            encrypted = encrypt_credentials(
                {
                    "secret_id": secret_id,
                    "secret_key": secret_key,
                    "refresh_token": provider.refresh_token,
                }
            )
            return {
                "success": True,
                "has_access_token": bool(access_token),
                "has_refresh_token": bool(provider.refresh_token),
                "encrypted_credentials": encrypted,
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
            }
        finally:
            await provider.close()

    # ─── Institution Listing ────────────────────────────────────────────

    async def list_institutions(
        self, provider_name: str, encrypted_creds: str, country: str
    ) -> list[dict[str, Any]]:
        """List available institutions for a country."""
        provider = self._create_provider(provider_name, encrypted_creds)
        try:
            institutions = await provider.list_institutions(country)
            return [
                {
                    "id": inst.id,
                    "name": inst.name,
                    "bic": inst.bic,
                    "country": inst.country,
                    "logo_url": inst.logo_url,
                    "transaction_total_days": inst.transaction_total_days,
                    "max_access_valid_for_days": inst.max_access_valid_for_days,
                }
                for inst in institutions
            ]
        finally:
            await provider.close()

    # ─── Connection Creation ────────────────────────────────────────────

    async def create_connection(
        self,
        provider_name: str,
        encrypted_creds: str,
        institution_id: str,
        redirect_url: str,
        reference: str | None = None,
        max_historical_days: int = 540,
    ) -> dict[str, Any]:
        """Create a bank connection (initiate OAuth flow).

        Returns requisition info including the redirect link.
        """
        provider = self._create_provider(provider_name, encrypted_creds)
        try:
            await provider.authenticate()

            # Create agreement with maximum history
            agreement_id = await provider.create_agreement(
                institution_id=institution_id,
                max_historical_days=max_historical_days,
                access_valid_for_days=90,
            )

            # Create requisition
            req = await provider.create_requisition(
                institution_id=institution_id,
                redirect_url=redirect_url,
                reference=reference,
                agreement_id=agreement_id,
                max_historical_days=max_historical_days,
            )

            # Save provider state (may have new refresh token)
            updated_creds = self._save_provider_state(
                provider_name, provider, encrypted_creds
            )

            return {
                **req,
                "agreement_id": agreement_id,
                "updated_credentials": updated_creds,
            }
        finally:
            await provider.close()

    # ─── Callback Handling ──────────────────────────────────────────────

    async def handle_callback(
        self,
        provider_name: str,
        encrypted_creds: str,
        requisition_id: str,
    ) -> dict[str, Any]:
        """Handle OAuth callback — check requisition status and get accounts.

        Returns:
            Dict with status, accounts, and account details.
        """
        provider = self._create_provider(provider_name, encrypted_creds)
        try:
            req = await provider.get_requisition(requisition_id)
            status = req.get("status")
            account_ids = req.get("accounts", [])

            if status != "LN":
                return {
                    "status": status,
                    "linked": False,
                    "accounts": [],
                    "error": f"Requisition status: {status} (expected LN=linked)",
                }

            # Get details for each account
            accounts = []
            for acc_id in account_ids:
                try:
                    details = await provider.get_account_details(acc_id)
                    accounts.append({
                        "id": details.id,
                        "iban": details.iban,
                        "name": details.name or details.display_name,
                        "owner_name": details.owner_name,
                        "currency": details.currency,
                        "product": details.product,
                        "status": details.status,
                    })
                except Exception as e:
                    logger.warning(
                        "bank_sync_failed_to_get_account_details",
                        account_id=acc_id,
                        error=str(e),
                    )
                    accounts.append({"id": acc_id, "error": str(e)})

            return {
                "status": status,
                "linked": True,
                "accounts": accounts,
                "agreement_id": req.get("agreement"),
                "institution_id": req.get("institution_id"),
                "reference": req.get("reference"),
            }
        finally:
            await provider.close()

    # ─── Core Sync ──────────────────────────────────────────────────────

    async def sync_transactions(
        self,
        provider_name: str,
        encrypted_creds: str,
        gc_account_id: str,
        local_account_id: int,
        last_sync_at: str | None = None,
    ) -> SyncResult:
        """Sync transactions from a bank account to the local database.

        Args:
            provider_name: Provider identifier.
            encrypted_creds: Encrypted provider credentials.
            gc_account_id: GoCardless account ID.
            local_account_id: Local finance_tracker accounts.id.
            last_sync_at: ISO timestamp of last successful sync (for incremental).

        Returns:
            SyncResult with counts and status.
        """
        provider = self._create_provider(provider_name, encrypted_creds)
        try:
            await provider.authenticate()

            # Determine date range for incremental sync
            date_from = None
            if last_sync_at:
                # Use last sync date minus 3 days for safety margin
                try:
                    last_dt = datetime.fromisoformat(
                        last_sync_at.replace("Z", "+00:00")
                    )
                    date_from = (last_dt - timedelta(days=3)).strftime("%Y-%m-%d")
                except ValueError:
                    date_from = None

            # Fetch transactions
            transactions, rate_info = await provider.get_transactions(
                account_id=gc_account_id,
                date_from=date_from,
            )

            result = SyncResult(
                rate_limit_remaining=rate_info.get("remaining"),
                rate_limit_reset_seconds=rate_info.get("reset_seconds"),
            )

            # Fetch balance
            try:
                balances = await provider.get_balances(gc_account_id)
                if balances:
                    # Prefer "interimAvailable" > "interimBooked" > first available
                    preferred = next(
                        (b for b in balances if b.balance_type == "interimAvailable"),
                        next(
                            (
                                b
                                for b in balances
                                if b.balance_type == "interimBooked"
                            ),
                            balances[0] if balances else None,
                        ),
                    )
                    result.balance = preferred
            except Exception as e:
                logger.warning("bank_sync_failed_to_get_balances", error=str(e))

            # Insert transactions (dedup by external_id)
            new_count = 0
            existing_count = 0
            pending_count = 0
            new_txs: list[Transaction] = []

            for tx in transactions:
                # Check for existing transaction
                stmt = select(Transaction).where(
                    and_(
                        Transaction.account_id == local_account_id,
                        Transaction.external_id == tx.transaction_id,
                        Transaction.deleted_at.is_(None),
                    )
                )
                existing = (await self.db.execute(stmt)).scalar_one_or_none()

                if existing:
                    # Update pending status if changed
                    if tx.status == "booked" and existing.is_pending:
                        existing.is_pending = False
                        existing.updated_at = _utcnow_str()
                    existing_count += 1
                    continue

                # Create new transaction. The ingestion service will normalise
                # the amount sign (review V1 §P1-5 — bank API delivers signed
                # amounts; the rest of the system stores positives + uses
                # `type` for direction).
                tx_type = _determine_tx_type(tx.amount, tx.counterparty)
                new_tx = Transaction(
                    account_id=local_account_id,
                    amount=tx.amount,
                    currency=tx.currency,
                    type=tx_type,
                    occurred_at=tx.booking_date or tx.value_date or _utcnow_str(),
                    posted_at=tx.value_date,
                    raw_description=tx.raw_description,
                    description=_clean_description(tx.raw_description),
                    counterparty=tx.counterparty,
                    source="bank_api",
                    external_id=tx.transaction_id,
                    is_pending=(tx.status == "pending"),
                    metadata_json=json.dumps(
                        {
                            "provider": provider_name,
                            "gc_account_id": gc_account_id,
                            "bank_transaction_code": tx.bank_transaction_code,
                            "end_to_end_id": tx.end_to_end_id,
                            "entry_reference": tx.entry_reference,
                            "internal_id": tx.internal_id,
                            "debtor_account_iban": tx.debtor_account_iban,
                            "creditor_account_iban": tx.creditor_account_iban,
                        }
                    ),
                )
                self.db.add(new_tx)
                new_txs.append(new_tx)

                if tx.status == "pending":
                    pending_count += 1
                else:
                    new_count += 1

            # Sprint 1 FIX-4 (review V1 §P1-7): bank-sync was bypassing
            # categorisation, transfer matching, amount normalisation, and
            # cashflow recompute. Route through the unified ingestion pipeline
            # so the same invariants apply across PDF / manual / bank API.
            if new_txs:
                from app.services.ingestion import ingest_transactions

                await ingest_transactions(self.db, new_txs, auto_pair=True)

            result.success = True
            result.transactions_new = new_count
            result.transactions_existing = existing_count
            result.transactions_pending = pending_count
            result.next_sync_at = (
                datetime.now(timezone.utc) + timedelta(hours=24)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")

            return result

        except Exception as e:
            logger.error("bank_sync_error", error=str(e), exc_info=True)
            return SyncResult(success=False, error=str(e))
        finally:
            await provider.close()

    # ─── Revoke Connection ──────────────────────────────────────────────

    async def revoke_connection(
        self,
        provider_name: str,
        encrypted_creds: str,
        requisition_id: str,
    ) -> bool:
        """Revoke bank access by deleting the requisition."""
        provider = self._create_provider(provider_name, encrypted_creds)
        try:
            return await provider.delete_requisition(requisition_id)
        finally:
            await provider.close()
