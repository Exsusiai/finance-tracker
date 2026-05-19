"""P1.1: airdrop / scam token filter.

Live wallets accumulate enormous amounts of unsolicited "claim me!" /
"visit URL!" airdrop tokens. We don't want them polluting the asset
table or breaking the price-fetch budget. Pure-function tests so they
can run as fast unit tests.
"""

from __future__ import annotations

import pytest

from app.services.wallet_sync.spam_filter import is_spam_token

LEGIT = [
    # (symbol,  name)
    ("ETH",     "Ethereum"),
    ("BTC",     "Bitcoin"),
    ("USDC",    "USD Coin"),
    ("USDC.E",  "Bridged USDC"),
    ("USDT",    "Tether USD"),
    ("DAI",     "Dai Stablecoin"),
    ("ARB",     "Arbitrum"),
    ("WBTC",    "Wrapped BTC"),
    ("SOL",     "Solana"),
    ("MATIC",   "Polygon"),
    ("LINK",    "Chainlink"),
    ("UNI",     "Uniswap"),
    ("WETH",    "Wrapped Ether"),
    ("ACHIVX",  "ACHIVX"),  # unknown but symbol-only is not enough to mark spam
    # ───── Real CoinGecko-listed tokens whose ticker matches a scam-word
    #       (see ERR-20260519-001 / Py-HIGH spam-filter false-positive).
    ("FREE",    "FreeRossDAO"),
    ("GIFT",    "Gifto"),
]

SPAM = [
    # Even safelisted tickers must still be caught when the NAME is
    # obviously spam (covers attempts to use legit symbols as cover).
    ("FREE", "Visit free-airdrop.xyz to claim 1000 USDT"),
    ("GIFT", "CLAIM YOUR FREE GIFT BONUS AT t.me/spam"),
    # URL / domain shillers (the bulk of real airdrop spam).
    ("ARB | T.ME/S/CLAIMARB | GET REWARD", "ARB | T.ME/S/CLAIMARB | GET REWARD"),
    ("ARB | T.ME/S/CLAIMARB | *VISIT TO CLAIM", "ARB | T.ME/S/CLAIMARB | *VISIT TO CLAIM"),
    ("ARB | T.ME/S/CLAIMARB | VIST TO CLAIM", "ARB | T.ME/S/CLAIMARB | VIST TO CLAIM"),
    ("ARB - [ T.LY/ARB ] *CLAIM WITHIN 7 DAYS", "ARB - [ T.LY/ARB ] *CLAIM WITHIN 7 DAYS"),
    ("NC-ELIGIBLE (VERIFY: WWW.NODECO.IN)", "NC-ELIGIBLE (VERIFY: WWW.NODECO.IN)"),
    ("VISIT STBOT.XYZ TO GET 1-2 ETH PER DAY", "VISIT STBOT.XYZ TO GET 1-2 ETH PER DAY"),
    # Bare verbs without URL
    ("CLAIM-NOW", "CLAIM NOW"),
    ("$REWARD", "$REWARD"),
    # Generic URL shapes
    ("XYZ", "Visit https://airdrop.xyz to claim"),
    ("STAKE", "stake.foo.com bonus"),
    # Excessively long symbols (real tickers max ~10 chars)
    ("A" * 25, "A" * 25),
]


class TestLegitTokens:
    @pytest.mark.parametrize("symbol,name", LEGIT)
    def test_not_spam(self, symbol: str, name: str) -> None:
        assert not is_spam_token(symbol, name), f"{symbol!r} ({name!r}) wrongly flagged spam"


class TestSpamTokens:
    @pytest.mark.parametrize("symbol,name", SPAM)
    def test_is_spam(self, symbol: str, name: str) -> None:
        assert is_spam_token(symbol, name), f"{symbol!r} ({name!r}) missed by spam filter"


class TestEdgeCases:
    def test_none_inputs_safe(self) -> None:
        # Solana SPL flow can pass `symbol=None`. Must NOT crash and must
        # not be flagged as spam on its own (the wallet_sync pipeline
        # already turns these into placeholder symbols downstream).
        assert is_spam_token(None, None) is False

    def test_empty_strings_safe(self) -> None:
        assert is_spam_token("", "") is False

    def test_normal_lowercase_passes(self) -> None:
        assert is_spam_token("eth", "Ethereum") is False
