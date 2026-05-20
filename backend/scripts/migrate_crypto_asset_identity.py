"""A3 data migration: split crypto Asset rows per (chain, contract).

Background
----------
Until the A1+A2 schema/service change (2026-05-20), crypto Asset identity
was ``(asset_class, symbol)``. So USDT-on-Ethereum and USDT-on-Arbitrum
shared one Asset row + one MarketPrice — different-contract prices would
silently overwrite each other, corrupting valuation.

A1 added ``chain`` + ``contract`` columns; A2 rewired the sync upsert
to write per-(chain, contract) Assets. But existing rows still have
``chain=''`` and ``contract=''`` — they need to be split by historical
holding chains.

Strategy
--------
- **Native** (``data_source='native'`` or no contract): keep one row,
  no split. ETH/BTC/SOL/etc. across L1+L2 share unified pricing.
- **Onchain** (``data_source='onchain'`` with contract):
    * If holdings span exactly ONE non-empty chain → ``UPDATE`` the row
      to that chain + lowercased contract.
    * If holdings span multiple non-empty chains → keep the most-common
      chain on the original row; create new Asset rows for the others
      and re-point those holdings.
    * If the same Asset has BOTH onchain holdings AND CEX holdings
      (chain=''), the CEX holdings get relocated to a separate
      ``chain='', contract=''`` native-flavored Asset so the onchain
      row's contract truly identifies its position.

Soft-archive: Assets whose only holdings end up moved AND that have
``data_source='onchain'`` are NOT archived (they keep their newly-set
chain/contract identity for future syncs to re-discover). The
``is_active`` flag is reserved for explicit user-driven archives.

Usage
-----
    ../.venv/bin/python -m scripts.migrate_crypto_asset_identity --dry-run
    ../.venv/bin/python -m scripts.migrate_crypto_asset_identity --apply

The script is idempotent: a second ``--apply`` run finds nothing to do.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB = _PROJECT_ROOT / "data" / "finance.db"


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _find_or_create_native_asset(
    con: sqlite3.Connection,
    symbol: str,
    *,
    apply: bool,
) -> int:
    """Return id of an Asset with (symbol, asset_class='crypto', chain='',
    contract=''). Creates it if missing."""
    row = con.execute(
        "SELECT id FROM assets WHERE asset_class='crypto' "
        "AND symbol = ? AND chain = '' AND contract = '' LIMIT 1",
        (symbol,),
    ).fetchone()
    if row is not None:
        return row["id"]
    if not apply:
        return -1  # placeholder; not actually inserted in dry-run
    now = _utcnow()
    cur = con.execute(
        "INSERT INTO assets "
        "(symbol, name, asset_class, currency, chain, contract, "
        " is_active, decimals, data_source, created_at, updated_at) "
        "VALUES (?, ?, 'crypto', 'USDT', '', '', 1, 2, 'native', ?, ?)",
        (symbol, symbol, now, now),
    )
    return cur.lastrowid


def _migrate_asset(
    con: sqlite3.Connection, asset: sqlite3.Row, *, apply: bool
) -> list[str]:
    """Return human-readable action lines for this one Asset."""
    actions: list[str] = []
    asset_id = asset["id"]
    symbol = asset["symbol"]
    contract_raw = asset["data_source_id"]
    data_source = asset["data_source"]

    chain_counts: Counter[str] = Counter(
        r["chain"]
        for r in con.execute(
            "SELECT chain FROM asset_holdings WHERE asset_id = ?", (asset_id,)
        )
    )
    if not chain_counts:
        return [f"  [skip] asset {asset_id} {symbol!r}: no holdings"]

    # Skip if already migrated (has chain/contract set non-empty).
    if asset["chain"] or asset["contract"]:
        return [
            f"  [skip] asset {asset_id} {symbol!r}: already has "
            f"chain={asset['chain']!r} contract={asset['contract']!r}"
        ]

    # Native row: don't touch — unified across chains by design.
    if data_source != "onchain" or not contract_raw:
        return [
            f"  [keep-native] asset {asset_id} {symbol!r}: native / "
            f"unified across {sorted(chain_counts)}"
        ]

    contract = contract_raw.strip().lower()
    onchain_chains = {c: n for c, n in chain_counts.items() if c}
    cex_count = chain_counts.get("", 0)

    if not onchain_chains:
        # User entered an onchain Asset but only holds via CEX. Treat as
        # a corner case: leave the Asset's chain='' but set contract so
        # next sync can match. Conservative.
        actions.append(
            f"  [legacy-cex-only] asset {asset_id} {symbol!r}: only CEX "
            "holdings; setting contract on existing row"
        )
        if apply:
            con.execute(
                "UPDATE assets SET contract = ?, updated_at = ? WHERE id = ?",
                (contract, _utcnow(), asset_id),
            )
        return actions

    # Choose the dominant (most-held) chain for the original row.
    primary_chain, _ = max(onchain_chains.items(), key=lambda kv: kv[1])
    actions.append(
        f"  [primary] asset {asset_id} {symbol!r}: "
        f"UPDATE chain='{primary_chain}' contract='{contract[:18]}…'"
    )
    if apply:
        con.execute(
            "UPDATE assets SET chain = ?, contract = ?, updated_at = ? "
            "WHERE id = ?",
            (primary_chain, contract, _utcnow(), asset_id),
        )

    # Move CEX (chain='') holdings off this row → a separate
    # native-flavoured Asset row so the onchain row's contract really
    # identifies its position.
    if cex_count:
        cex_asset_id = _find_or_create_native_asset(con, symbol, apply=apply)
        actions.append(
            f"  [cex-split] {cex_count} CEX holdings of {symbol!r} "
            f"→ asset {cex_asset_id} (native)"
        )
        if apply:
            con.execute(
                "UPDATE asset_holdings SET asset_id = ?, updated_at = ? "
                "WHERE asset_id = ? AND chain = ''",
                (cex_asset_id, _utcnow(), asset_id),
            )

    # Holdings on OTHER non-empty chains (rare in practice) → new rows.
    for chain, _n in onchain_chains.items():
        if chain == primary_chain:
            continue
        # Contract for this chain is unknown from existing DB; conservatively
        # use the same contract value (often wrong, but no other source).
        # User will need to re-sync to populate the true contract.
        new_asset_id = -1
        row = con.execute(
            "SELECT id FROM assets WHERE asset_class='crypto' "
            "AND symbol = ? AND chain = ? AND contract = ?",
            (symbol, chain, contract),
        ).fetchone()
        if row:
            new_asset_id = row["id"]
        elif apply:
            now = _utcnow()
            cur = con.execute(
                "INSERT INTO assets "
                "(symbol, name, asset_class, currency, chain, contract, "
                " is_active, decimals, data_source, data_source_id, "
                " created_at, updated_at) "
                "VALUES (?, ?, 'crypto', 'USDT', ?, ?, 1, 2, 'onchain', ?, ?, ?)",
                (symbol, symbol, chain, contract, contract, now, now),
            )
            new_asset_id = cur.lastrowid
        actions.append(
            f"  [extra-chain-split] {symbol!r} on {chain}: holdings → asset {new_asset_id}"
        )
        if apply:
            con.execute(
                "UPDATE asset_holdings SET asset_id = ?, updated_at = ? "
                "WHERE asset_id = ? AND chain = ?",
                (new_asset_id, _utcnow(), asset_id, chain),
            )

    return actions


def migrate(db_path: Path, *, apply: bool) -> int:
    con = _connect(db_path)
    try:
        crypto_assets = con.execute(
            "SELECT id, symbol, data_source, data_source_id, chain, contract "
            "FROM assets WHERE asset_class = 'crypto' ORDER BY id"
        ).fetchall()
        print(f"Found {len(crypto_assets)} crypto Asset rows to inspect")
        for a in crypto_assets:
            for line in _migrate_asset(con, a, apply=apply):
                print(line)
        if apply:
            con.commit()
            print("\n✓ committed")
        else:
            print("\n(dry-run — no changes written; rerun with --apply)")
    finally:
        con.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--dry-run", action="store_true")
    grp.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    return migrate(args.db, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
