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

# Allow-list for symbols that match a scam-word literally but are real
# CoinGecko-listed projects. The check runs as `symbol.upper() in set`
# BEFORE the regex, so it only short-circuits the *symbol-only* false
# positive — a name like "Claim Your FREE 1000 USDT" still gets flagged
# because the name still contains other scam words.
_SYMBOL_SAFELIST: frozenset[str] = frozenset({
    "FREE",    # FreeRossDAO
    "GIFT",    # Gifto
    "REWARD",  # placeholder for any future real token with this ticker
})

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

    # Rule 2: scam verbs — UNLESS the symbol is a known legit ticker
    # that happens to match a scam word verbatim (FREE / GIFT / REWARD).
    # The safelist only saves the symbol → if the surrounding NAME also
    # screams airdrop the regex will still match it from `name`.
    if _SCAM_WORDS.search(haystack):
        sym_upper = s.upper()
        if sym_upper in _SYMBOL_SAFELIST:
            # Re-run the regex against name alone to catch
            # "FREE 1000 USDT CLAIM HERE" style spam that uses the
            # legit symbol as cover.
            if n and _SCAM_WORDS.search(n):
                # Drop the symbol token from the name match so a name
                # like "Free Ross DAO" (just literal "Free") passes;
                # only spam-around-it ("Free 1000 USDT visit ...") trips.
                stripped = re.sub(rf"\b{re.escape(s)}\b", "", n, flags=re.IGNORECASE)
                if _SCAM_WORDS.search(stripped) or _URL_PATTERN.search(stripped):
                    return True
            return False
        return True

    return False
