"""GoCardless Bank Account Data (formerly Nordigen) provider implementation.

API Reference: https://developer.gocardless.com/bank-account-data/
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

import httpx

from app.services.bank_sync.providers.base import (
    AccountInfo,
    BalanceInfo,
    BankProvider,
    BankTransaction,
    Institution,
)

logger = logging.getLogger(__name__)

GC_API_BASE = "https://bankaccountdata.gocardless.com"
GC_TOKEN_NEW = "/api/v2/token/new/"
GC_TOKEN_REFRESH = "/api/v2/token/refresh/"
GC_INSTITUTIONS = "/api/v2/institutions/"
GC_AGREEMENTS = "/api/v2/agreements/enduser/"
GC_REQUISITIONS = "/api/v2/requisitions/"
GC_ACCOUNTS = "/api/v2/accounts/"


class GoCardlessProvider(BankProvider):
    """GoCardless Bank Account Data provider.

    Authentication flow:
        1. secret_id + secret_key → refresh_token (long-lived, ~30 days)
        2. refresh_token → access_token (short-lived, ~24 hours)
        3. access_token → all API calls
    """

    def __init__(
        self,
        secret_id: str,
        secret_key: str,
        refresh_token: str | None = None,
    ):
        self._secret_id = secret_id
        self._secret_key = secret_key
        self._refresh_token = refresh_token
        self._access_token: str | None = None
        self._access_expires_at: datetime | None = None
        self._client = httpx.AsyncClient(
            base_url=GC_API_BASE,
            timeout=30.0,
            headers={"accept": "application/json"},
        )

    @property
    def provider_name(self) -> str:
        return "gocardless"

    # ─── Authentication ─────────────────────────────────────────────────

    async def authenticate(self) -> str:
        """Get or refresh an access token."""
        if self._access_token and self._access_expires_at:
            if datetime.now(timezone.utc) < self._access_expires_at:
                return self._access_token

        if not self._refresh_token:
            # Initial auth: get refresh token from secrets
            resp = await self._client.post(
                GC_TOKEN_NEW,
                json={
                    "secret_id": self._secret_id,
                    "secret_key": self._secret_key,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._refresh_token = data["refresh"]
            logger.info("gocardless_obtained_refresh_token")
        else:
            # Refresh: exchange refresh token for new access token
            resp = await self._client.post(
                GC_TOKEN_REFRESH,
                json={"refresh": self._refresh_token},
            )
            if resp.status_code == 400:
                logger.warning("gocardless_refresh_token_expired, requesting_new")
                # Refresh token expired, get a new one
                self._refresh_token = None
                return await self.authenticate()

            resp.raise_for_status()
            data = resp.json()

        self._access_token = data["access"]
        expires_in = data.get("access_expires", 86400)
        self._access_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=expires_in - 300  # Refresh 5 min early
        )
        self._client.headers["Authorization"] = f"Bearer {self._access_token}"
        return self._access_token

    @property
    def refresh_token(self) -> str | None:
        return self._refresh_token

    @refresh_token.setter
    def refresh_token(self, value: str | None):
        self._refresh_token = value

    # ─── Institutions ───────────────────────────────────────────────────

    async def list_institutions(self, country: str) -> list[Institution]:
        await self.authenticate()
        resp = await self._client.get(
            GC_INSTITUTIONS,
            params={"country": country.lower()},
        )
        resp.raise_for_status()
        institutions = []
        for item in resp.json():
            institutions.append(
                Institution(
                    id=item["id"],
                    name=item["name"],
                    bic=item.get("bic"),
                    country=country.upper(),
                    logo_url=item.get("logo"),
                    transaction_total_days=int(item.get("transaction_total_days", 90)),
                    max_access_valid_for_days=int(
                        item.get("max_access_valid_for_days", 90)
                    ),
                )
            )
        return institutions

    # ─── Agreements ─────────────────────────────────────────────────────

    async def create_agreement(
        self,
        institution_id: str,
        max_historical_days: int = 540,
        access_valid_for_days: int = 90,
        access_scope: list[str] | None = None,
    ) -> str:
        await self.authenticate()
        if access_scope is None:
            access_scope = ["balances", "details", "transactions"]
        resp = await self._client.post(
            GC_AGREEMENTS,
            json={
                "institution_id": institution_id,
                "max_historical_days": str(max_historical_days),
                "access_valid_for_days": str(access_valid_for_days),
                "access_scope": access_scope,
            },
        )
        resp.raise_for_status()
        return resp.json()["id"]

    # ─── Requisitions ───────────────────────────────────────────────────

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
        await self.authenticate()
        payload: dict[str, Any] = {
            "institution_id": institution_id,
            "redirect": redirect_url,
            "user_language": user_language,
        }
        if reference:
            payload["reference"] = reference
        if agreement_id:
            payload["agreement"] = agreement_id

        resp = await self._client.post(GC_REQUISITIONS, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return {
            "requisition_id": data["id"],
            "link": data["link"],
            "status": data.get("status", "CR"),
            "agreement_id": data.get("agreement"),
            "reference": data.get("reference"),
        }

    async def get_requisition(self, requisition_id: str) -> dict[str, Any]:
        await self.authenticate()
        resp = await self._client.get(f"{GC_REQUISITIONS}{requisition_id}/")
        resp.raise_for_status()
        data = resp.json()
        return {
            "requisition_id": data["id"],
            "status": data.get("status"),
            "accounts": data.get("accounts", []),
            "agreement": data.get("agreement"),
            "reference": data.get("reference"),
            "institution_id": data.get("institution_id"),
            "redirect": data.get("redirect"),
            "created": data.get("created"),
        }

    async def delete_requisition(self, requisition_id: str) -> bool:
        await self.authenticate()
        resp = await self._client.delete(f"{GC_REQUISITIONS}{requisition_id}/")
        return resp.status_code in (200, 204)

    # ─── Account Data ───────────────────────────────────────────────────

    async def get_account_details(self, account_id: str) -> AccountInfo:
        await self.authenticate()
        resp = await self._client.get(f"{GC_ACCOUNTS}{account_id}/")
        resp.raise_for_status()
        data = resp.json()
        return AccountInfo(
            id=account_id,
            iban=data.get("iban"),
            bban=data.get("bban"),
            bic=data.get("bic"),
            name=data.get("name"),
            display_name=data.get("displayName"),
            owner_name=data.get("ownerName"),
            currency=data.get("currency"),
            product=data.get("product"),
            status=data.get("status"),
            details=data.get("details"),
        )

    async def get_balances(self, account_id: str) -> list[BalanceInfo]:
        await self.authenticate()
        resp = await self._client.get(f"{GC_ACCOUNTS}{account_id}/balances/")
        resp.raise_for_status()
        data = resp.json()
        balances = []
        for bal in data.get("balances", []):
            amt = bal.get("balanceAmount", {})
            balances.append(
                BalanceInfo(
                    balance_type=bal.get("balanceType", "unknown"),
                    amount=Decimal(str(amt.get("amount", 0))),
                    currency=amt.get("currency", "EUR"),
                    reference_date=bal.get("referenceDate"),
                )
            )
        return balances

    async def get_transactions(
        self,
        account_id: str,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> tuple[list[BankTransaction], dict[str, Any]]:
        await self.authenticate()
        params: dict[str, str] = {}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to

        resp = await self._client.get(
            f"{GC_ACCOUNTS}{account_id}/transactions/",
            params=params,
        )

        # Parse rate limit headers
        rate_info: dict[str, Any] = {}
        for header, key in [
            ("HTTP_X_RATELIMIT_REMAINING", "remaining"),
            ("HTTP_X_RATELIMIT_RESET", "reset_seconds"),
            ("HTTP_X_RATELIMIT_LIMIT", "limit"),
            (
                "HTTP_X_RATELIMIT_ACCOUNT_SUCCESS_REMAINING",
                "account_success_remaining",
            ),
            (
                "HTTP_X_RATELIMIT_ACCOUNT_SUCCESS_RESET",
                "account_success_reset_seconds",
            ),
        ]:
            val = resp.headers.get(header)
            if val:
                try:
                    rate_info[key] = int(val)
                except (ValueError, TypeError):
                    pass

        if resp.status_code == 429:
            return [], rate_info

        resp.raise_for_status()
        data = resp.json()

        transactions: list[BankTransaction] = []
        txs = data.get("transactions", {})

        for book_type in ("booked", "pending"):
            for tx in txs.get(book_type, []):
                amt = tx.get("transactionAmount", {})
                description = tx.get("remittanceInformationUnstructured") or tx.get(
                    "additionalInformation", ""
                )

                # Counterparty: prefer creditor for debits, debtor for credits
                amount_val = Decimal(str(amt.get("amount", 0)))
                if amount_val < 0:
                    counterparty = tx.get("creditorName") or tx.get(
                        "debtorName"
                    )
                else:
                    counterparty = tx.get("debtorName") or tx.get(
                        "creditorName"
                    )

                transactions.append(
                    BankTransaction(
                        transaction_id=tx.get("transactionId", ""),
                        booking_date=tx.get("bookingDate"),
                        value_date=tx.get("valueDate"),
                        amount=amount_val,
                        currency=amt.get("currency", "EUR"),
                        description=description[:500] if description else None,
                        raw_description=description[:2000] if description else None,
                        counterparty=counterparty[:255] if counterparty else None,
                        debtor_account_iban=tx.get("debtorAccount", {}).get("iban"),
                        creditor_account_iban=tx.get("creditorAccount", {}).get(
                            "iban"
                        ),
                        bank_transaction_code=tx.get("bankTransactionCode"),
                        status="booked" if book_type == "booked" else "pending",
                        internal_id=tx.get("internalTransactionId"),
                        additional_info=tx.get("additionalInformation"),
                        end_to_end_id=tx.get("endToEndId"),
                        entry_reference=tx.get("entryReference"),
                    )
                )

        return transactions, rate_info

    # ─── Cleanup ────────────────────────────────────────────────────────

    async def close(self):
        await self._client.aclose()
