"""P1-4: cross-cutting orchestration for on-chain + CEX balance sync.

This package wires `services/crypto_sync` (chain providers) and
`services/exchange_sync` (CEX providers) onto the DB layer:

- ``upsert.apply_balance_snapshot`` — pure DB write (one chain at a
  time). The orchestrator (A4.2) loops over addresses / connections
  and calls this once per (chain, address) pair.
- ``orchestrator.sync_account`` — coming in A4.2.
"""

from app.services.wallet_sync import orchestrator
from app.services.wallet_sync.orchestrator import (
    SyncResult,
    SyncSummary,
    sync_account,
)
from app.services.wallet_sync.upsert import apply_balance_snapshot

__all__ = [
    "apply_balance_snapshot",
    "orchestrator",
    "SyncResult",
    "SyncSummary",
    "sync_account",
]
