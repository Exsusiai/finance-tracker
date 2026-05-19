"""Airdrop / scam token filter.

On-chain wallets accumulate masses of unsolicited "claim me" / "visit URL"
ERC-20 / SPL spam. We drop them BEFORE asset rows get created so they
don't pollute the asset table, the price-fetch budget, or the UI.

Rules (only need ONE to match → spam):
1. Symbol or name contains a URL-like pattern (T.ME / WWW. / .XYZ /
   .COM / http / https / domain-with-slash).
2. Symbol or name contains scam verb keywords (CLAIM / VISIT / REWARD
   / ELIGIBLE / VERIFY / STAKE bonus / BONUS / FREE / GIFT).
3. Symbol is excessively long (> 20 chars). Real tickers are ≤ 10.
4. Name contains an explicit prize-y URL ("visit X.Y to ...").

These rules are intentionally conservative — they target the patterns
seen in real airdrop spam (e.g. "ARB | T.ME/S/CLAIMARB | GET REWARD")
and won't false-positive on legitimate verbose names like "Wrapped Ether"
or "Bridged USDC". When in doubt the user can still see flagged items
via a future "show hidden" toggle, but for now we drop them silently.
"""

from __future__ import annotations

import re


# Match any of: T.ME, T.LY, WWW., .XYZ, .COM, .NET, .IO, .CC, .CO, .ME,
# .ORG, .INFO, .APP, .IN, plus full URL prefixes.
_URL_PATTERN = re.compile(
    r"(?:https?://|www\.|t\.me|t\.ly|"
    r"\.xyz|\.com|\.net|\.io|\.cc|\.co|\.me|\.org|\.info|\.app|\.in)\b",
    re.IGNORECASE,
)

# Scam verbs / phrases. Word-boundary anchored so "REWARDS PROGRAM" (a
# legit token name some day) might still pass, but "REWARD" as a token
# name almost certainly won't be real.
_SCAM_WORDS = re.compile(
    r"\b(?:claim|visit|verify|eligible|reward|bonus|gift|free|"
    r"airdrop|stake[d]?\s+to|node[ck]o)\b",
    re.IGNORECASE,
)

_MAX_SYMBOL_LEN = 20


def is_spam_token(symbol: str | None, name: str | None) -> bool:
    """Return True if this token looks like airdrop spam.

    Both arguments are tolerated as None / empty (Solana SPL path
    intentionally has no symbol on first sync — those rows go through
    a placeholder-symbol step and are not spam by themselves).
    """
    s = (symbol or "").strip()
    n = (name or "").strip()

    if not s and not n:
        return False  # nothing to inspect — let the upstream decide.

    # Rule 3: symbol-length sanity. Native + ERC-20 / SPL / TRC-20
    # tickers are universally short.
    if len(s) > _MAX_SYMBOL_LEN:
        return True

    haystack = f"{s}\n{n}"

    # Rule 1: any URL-shaped fragment.
    if _URL_PATTERN.search(haystack):
        return True

    # Rule 2: scam verbs.
    if _SCAM_WORDS.search(haystack):
        return True

    return False
