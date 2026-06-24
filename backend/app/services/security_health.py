"""Encryption-credential health check.

When ``FINANCE_BANK_ENCRYPTION_KEY`` is rotated (or accidentally
regenerated), every credential previously encrypted with the old key
becomes undecryptable. The failure mode used to be *silent*: LLM
classification quietly abstained and CEX sync threw only at sync time,
with nothing at startup telling the user the key changed.

This module scans all stored encrypted credentials and reports which no
longer decrypt with the current key, so:
  - lifespan can emit a loud WARNING at startup, and
  - the settings endpoints can show "key changed — please re-enter"
    instead of conflating it with "not set".

See .learnings ERR-20260607-001.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BrokerConnection, ExchangeConnection
from app.services.app_settings import get_setting
from app.services.bank_sync.crypto import can_decrypt


@dataclass(frozen=True)
class CredentialHealth:
    """Result of a credential decryption sweep.

    ``stale``: human-readable labels of credentials that exist but won't
    decrypt with the current key. ``ok_count``: how many decrypted fine.
    """

    stale: list[str] = field(default_factory=list)
    ok_count: int = 0


async def verify_credentials_health(db: AsyncSession) -> CredentialHealth:
    """Try to decrypt every stored encrypted credential. Never raises."""
    stale: list[str] = []
    ok = 0

    # 1. Gemini API key (app_settings.gemini_api_key_enc)
    try:
        gemini_enc = await get_setting(db, "gemini_api_key_enc", default=None)
    except Exception:
        gemini_enc = None
    if gemini_enc:
        if can_decrypt(gemini_enc):
            ok += 1
        else:
            stale.append("Gemini API key (智能分类)")

    # 2. Exchange connections (api_key / api_secret / passphrase)
    try:
        rows = (await db.execute(select(ExchangeConnection))).scalars().all()
    except Exception:
        rows = []
    for row in rows:
        label = f"{row.exchange} 交易所凭据 (account #{row.account_id})"
        blobs = [row.api_key_enc, row.api_secret_enc]
        if row.api_passphrase_enc:
            blobs.append(row.api_passphrase_enc)
        if all(can_decrypt(b) for b in blobs):
            ok += 1
        else:
            stale.append(label)

    # 3. Broker connections (Flex token)
    try:
        broker_rows = (await db.execute(select(BrokerConnection))).scalars().all()
    except Exception:
        broker_rows = []
    for row in broker_rows:
        label = f"{row.provider} 券商凭据 (account #{row.account_id})"
        if can_decrypt(row.token_enc):
            ok += 1
        else:
            stale.append(label)

    return CredentialHealth(stale=stale, ok_count=ok)
