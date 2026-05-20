"""Upsert chain/exchange balance snapshots into ``asset_holdings``.

Contract (decided 2026-05-18 with user):

- One Asset row per ``(symbol, asset_class='crypto')``. Holdings on
  different chains share that Asset and discriminate via
  ``asset_holdings.chain``.
- BalanceItems without a symbol (Solana SPL tokens whose mint we
  haven't resolved yet) get a placeholder symbol derived from the
  contract address so they don't collide. A future P2 step can
  upgrade these to real symbols via CoinGecko-by-contract.
- Re-sync semantics:
    present this round → quantity=<fetched>, is_active=True
    missing this round → quantity=0,         is_active=False
- The reset scope is **per (account, chain)** — a re-sync of one
  chain must not zero holdings on another chain.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Asset, AssetHolding, _utcnow_str, touch_updated_at
from app.services.crypto_sync import BalanceItem
from app.services.wallet_sync.spam_filter import is_spam_token


def _contract_label(contract: str | None) -> str:
    """Pass the contract address as a side-channel into the spam check
    when the symbol is missing — never makes a SPL mint look spammy on
    its own (mints don't carry English text), but keeps the door open
    to scoring on contract metadata later."""
    return contract or ""


def _placeholder_symbol(contract: str) -> str:
    """Stable, short, contract-derived label for unresolved tokens.

    Keeps Asset.symbol non-empty + unique-per-contract. Format chosen so
    a human glancing at the UI sees "?…" plus 8 chars of contract — enough
    to disambiguate without pretending to be a real ticker.
    """
    head = (contract or "").strip()
    if not head:
        return "?UNKNOWN"
    return f"?{head[:8]}".upper()


async def _get_or_create_asset(
    db: AsyncSession,
    *,
    symbol: str | None,
    contract: str | None,
    chain: str,
) -> Asset:
    """Resolve a BalanceItem to a single Asset row.

    A-sprint 2026-05-20 (V5-P1-1): Asset identity is now
    (asset_class, symbol, chain, contract).

    Storage rules:
      - **On-chain token** (contract present): store ``chain`` as given,
        ``contract`` lower-cased canonical (mixed-case hex addresses
        must hash the same). USDT-ethereum and USDT-arbitrum become
        distinct rows; same symbol on the same chain with different
        contracts (USDC-native vs USDC.E-bridged on Arbitrum) also
        become distinct rows.
      - **Native chain coin** (no contract): store ``chain=''``,
        ``contract=''``. Per decision #1, ETH-on-L1 and ETH-on-L2
        share ONE Asset row — CoinGecko prices ETH unified, and the
        per-holding chain discriminator lives on ``AssetHolding.chain``.
        Caller passes ``chain`` only to make this normalisation visible.
    """
    canonical_symbol = (
        symbol.strip().upper() if symbol else _placeholder_symbol(contract or "")
    )
    if contract:
        # Onchain token: identity is full (asset_class, symbol, chain, contract).
        store_chain = chain
        store_contract = contract.strip().lower()
        data_source = "onchain"
        data_source_id = store_contract
        display_name = canonical_symbol if symbol else (contract or "unresolved")
    else:
        # Native coin or contract-less: normalise to chain='', contract=''
        # so all chains share one row.
        store_chain = ""
        store_contract = ""
        data_source = "native"
        data_source_id = None
        display_name = canonical_symbol

    existing = (
        await db.execute(
            select(Asset).where(
                Asset.symbol == canonical_symbol,
                Asset.asset_class == "crypto",
                Asset.chain == store_chain,
                Asset.contract == store_contract,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    a = Asset(
        symbol=canonical_symbol,
        name=display_name,
        asset_class="crypto",
        currency="USDT",  # crypto holdings always quote in USDT per project decision
        chain=store_chain,
        contract=store_contract,
        data_source=data_source,
        data_source_id=data_source_id,
    )
    db.add(a)
    await db.flush()
    return a


async def apply_balance_snapshot(
    db: AsyncSession,
    account_id: int,
    chain: str,
    items: Iterable[BalanceItem],
) -> int:
    """Apply one fresh fetch result for (account_id, chain).

    Returns the number of present rows in this snapshot (zero rows do
    NOT count — they're the disappeared-token tail).
    """
    # Drop airdrop / scam tokens before they ever become Asset rows. See
    # services/wallet_sync/spam_filter.py for the rules. This runs *here*
    # rather than inside the providers because a future "show hidden"
    # toggle could re-route these elsewhere without re-fetching from the
    # network.
    items = [it for it in items if not is_spam_token(it.symbol, _contract_label(it.contract))]
    now = _utcnow_str()
    present_asset_ids: set[int] = set()
    present_count = 0

    for item in items:
        asset = await _get_or_create_asset(
            db, symbol=item.symbol, contract=item.contract, chain=chain
        )
        present_asset_ids.add(asset.id)
        present_count += 1

        holding = (
            await db.execute(
                select(AssetHolding).where(
                    AssetHolding.account_id == account_id,
                    AssetHolding.asset_id == asset.id,
                    AssetHolding.chain == chain,
                )
            )
        ).scalar_one_or_none()

        if holding is None:
            holding = AssetHolding(
                account_id=account_id,
                asset_id=asset.id,
                chain=chain,
                quantity=item.quantity,
                is_active=True,
                last_synced_at=now,
            )
            db.add(holding)
        else:
            holding.quantity = item.quantity
            holding.is_active = True
            holding.last_synced_at = now
            touch_updated_at(holding)

    # Zero-out anything previously seen for this (account, chain) that
    # didn't appear in this round. Bulk UPDATE to avoid N+1 loops.
    if present_asset_ids:
        await db.execute(
            update(AssetHolding)
            .where(
                AssetHolding.account_id == account_id,
                AssetHolding.chain == chain,
                AssetHolding.asset_id.notin_(present_asset_ids),
                AssetHolding.is_active == True,  # noqa: E712 — SQLAlchemy column compare
            )
            .values(
                quantity=Decimal("0"),
                is_active=False,
                last_synced_at=now,
                updated_at=now,
            )
        )
    else:
        # Empty snapshot → nothing is present this round → zero them all
        # for this chain.
        await db.execute(
            update(AssetHolding)
            .where(
                AssetHolding.account_id == account_id,
                AssetHolding.chain == chain,
                AssetHolding.is_active == True,  # noqa: E712
            )
            .values(
                quantity=Decimal("0"),
                is_active=False,
                last_synced_at=now,
                updated_at=now,
            )
        )

    await db.flush()
    return present_count
