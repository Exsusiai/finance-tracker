"""Abstract base class for bank data providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional


@dataclass
class Institution:
    """A bank/financial institution in the provider's system."""

    id: str
    name: str
    bic: str | None = None
    country: str | None = None
    logo_url: str | None = None
    transaction_total_days: int = 90
    max_access_valid_for_days: int = 90


@dataclass
class AccountInfo:
    """Bank account details returned by the provider."""

    id: str  # Provider's account ID
    iban: str | None = None
    bban: str | None = None
    bic: str | None = None
    name: str | None = None
    display_name: str | None = None
    owner_name: str | None = None
    currency: str | None = None
    product: str | None = None
    status: str | None = None
    details: str | None = None


@dataclass
class BalanceInfo:
    """Account balance."""

    balance_type: str  # "interimAvailable", "interimBooked", "closingBooked", etc.
    amount: Decimal
    currency: str
    reference_date: str | None = None


@dataclass
class BankTransaction:
    """A single bank transaction."""

    transaction_id: str  # Provider's unique transaction ID
    booking_date: str | None = None
    value_date: str | None = None
    amount: Decimal = Decimal("0")
    currency: str = "EUR"
    description: str | None = None
    raw_description: str | None = None
    counterparty: str | None = None
    debtor_account_iban: str | None = None
    creditor_account_iban: str | None = None
    bank_transaction_code: str | None = None
    status: str = "booked"  # "booked" or "pending"
    internal_id: str | None = None
    additional_info: str | None = None
    end_to_end_id: str | None = None
    entry_reference: str | None = None


@dataclass
class SyncResult:
    """Result of a bank sync operation."""

    success: bool = False
    transactions_new: int = 0
    transactions_existing: int = 0
    transactions_pending: int = 0
    balance: BalanceInfo | None = None
    error: str | None = None
    next_sync_at: str | None = None
    rate_limit_remaining: int | None = None
    rate_limit_reset_seconds: int | None = None

    def summary(self) -> str:
        if self.error:
            return f"Sync failed: {self.error}"
        return (
            f"Sync OK: {self.transactions_new} new, "
            f"{self.transactions_existing} existing, "
            f"{self.transactions_pending} pending"
        )


class BankProvider(ABC):
    """Abstract interface for bank data providers.

    Each provider (GoCardless, Tink, etc.) implements this interface.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Unique provider identifier (e.g. 'gocardless')."""
        ...

    @abstractmethod
    async def authenticate(self) -> str:
        """Authenticate and return an access token.

        Returns:
            Access token string.
        """
        ...

    @abstractmethod
    async def list_institutions(self, country: str) -> list[Institution]:
        """List available financial institutions for a country.

        Args:
            country: ISO 3166-1 alpha-2 country code (e.g. 'DE', 'GB').

        Returns:
            List of supported institutions.
        """
        ...

    @abstractmethod
    async def create_requisition(
        self,
        institution_id: str,
        redirect_url: str,
        reference: str | None = None,
        agreement_id: str | None = None,
        max_historical_days: int = 540,
        access_valid_for_days: int = 90,
        user_language: str = "EN",
    ) -> dict[str, Any]:
        """Create a bank connection requisition.

        Returns a dict with at least:
            - 'requisition_id': str
            - 'link': str (URL to redirect user to)
        """
        ...

    @abstractmethod
    async def get_requisition(self, requisition_id: str) -> dict[str, Any]:
        """Get requisition status and linked accounts.

        Returns a dict with at least:
            - 'status': str ('CR' = created, 'LN' = linked, 'RJ' = rejected, etc.)
            - 'accounts': list[str] (provider account IDs)
            - 'agreement': str (agreement ID)
        """
        ...

    @abstractmethod
    async def delete_requisition(self, requisition_id: str) -> bool:
        """Delete a requisition (revoke bank access)."""
        ...

    @abstractmethod
    async def get_account_details(self, account_id: str) -> AccountInfo:
        """Get account details."""
        ...

    @abstractmethod
    async def get_balances(self, account_id: str) -> list[BalanceInfo]:
        """Get account balances."""
        ...

    @abstractmethod
    async def get_transactions(
        self,
        account_id: str,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> tuple[list[BankTransaction], dict[str, Any]]:
        """Get account transactions.

        Args:
            account_id: Provider's account ID.
            date_from: Start date (YYYY-MM-DD). None = from beginning.
            date_to: End date (YYYY-MM-DD). None = now.

        Returns:
            Tuple of (transactions list, rate_limit_info dict).
        """
        ...

    @abstractmethod
    async def create_agreement(
        self,
        institution_id: str,
        max_historical_days: int = 540,
        access_valid_for_days: int = 90,
        access_scope: list[str] | None = None,
    ) -> str:
        """Create an end user agreement.

        Returns:
            Agreement ID.
        """
        ...
