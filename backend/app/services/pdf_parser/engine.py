"""PDF parser engine — detects bank and delegates to bank-specific parser.

Supported banks (verified against real samples in `data/inputpdf_reference/`):
  - AMEX-DE     (American Express Gold Card, Germany)
  - N26         (N26 Bank, EUR)
  - Revolut     (Revolut Bank, EUR)
  - TFBank      (TF Mastercard Gold, Germany)
  - Advanzia    (Hilton Honors / Advanzia Bank, Luxembourg)

Other banks fall back to a generic heuristic parser.
"""

from __future__ import annotations

import asyncio
import io
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PdfImport


# ─── Public entry ──────────────────────────────────────────────────────


async def parse_pdf_statement(
    db: AsyncSession,
    pdf_import: PdfImport,
    content: bytes,
    *,
    subaccount_names: list[str] | None = None,
    force_bank: str | None = None,
) -> dict[str, Any]:
    """Parse a PDF bank statement.

    Args:
      subaccount_names: per-account user-maintained list of sub-account names
        (e.g. ["Investing", "Dream List"]). When a tx description contains any
        of these, we tag the tx as a sub-account move (type='transfer' +
        metadata.subaccount=true) so the balance view skips it.

    Returns dict with:
        detected_bank: str | None
        parser_version: str
        statement_period: str | None
        raw_text: str
        error: str | None
        transactions: list[dict]
    """
    try:
        import pdfplumber  # noqa: F401 (probe only)
    except ImportError:
        return _empty_result(error="pdfplumber not installed")

    # pdfplumber + the SQLite IO that may follow are blocking; run extraction in
    # a worker thread to avoid SQLAlchemy's greenlet check tripping on Py 3.14.
    try:
        raw_text, page_words = await asyncio.to_thread(_extract_text_and_words_sync, content)
    except Exception as e:
        return _empty_result(error=f"Failed to extract text: {e}")
    # Per-task context (async-safe — replaces the old module-level list)
    _names_token = _set_subaccount_names(list(subaccount_names or []))
    try:
        # `force_bank` lets the user override auto-detection from the upload
        # UI (some statements share BICs / domains and mis-detect). Values:
        #   - a known parser key (n26 / revolut / tfbank / advanzia / amex_de)
        #     → skip detection, use that parser
        #   - "other" / "generic" → force the generic parser
        #   - None / "auto" / "" → fall back to text-feature auto-detection
        forced = (force_bank or "").strip().lower()
        if forced in ("other", "generic"):
            detected_bank = None
        elif forced and forced in _BANK_PARSERS:
            # Includes "revolut" — the parse dispatch below special-cases it
            # to the column-aware parser regardless of the _BANK_PARSERS map.
            detected_bank = forced
        else:
            detected_bank = _detect_bank(raw_text)

        # Revolut: column-aware parser (uses word X-coordinates to distinguish
        # Money out / Money in columns — pure-text regex can't tell them apart).
        if detected_bank == "revolut":
            transactions = _parse_revolut_columns(page_words)
        elif detected_bank:
            parser_func = _BANK_PARSERS.get(detected_bank, _parse_generic)
            transactions = parser_func(raw_text)
        else:
            transactions = _parse_generic(raw_text)

        # 2026-05-06: parser external_ids look like "tfbank_1" / "n26_3" — they
        # restart from seq=1 in every PDF, so two TFBank statements (e.g. March
        # and April) BOTH emit "tfbank_1", which collides with the partial
        # unique index `(account_id, external_id) WHERE deleted_at IS NULL`
        # the second time around. Post-process the parser output to prefix the
        # external_id with a per-PDF disambiguator (the SHA-256 prefix of the
        # PDF bytes) so two different PDFs can never produce the same
        # external_id even when their seqs overlap.
        import hashlib

        pdf_disambiguator = hashlib.sha256(content).hexdigest()[:12]
        for tx in transactions:
            eid = tx.get("external_id")
            if eid:
                tx["external_id"] = f"{eid}_{pdf_disambiguator}"

        closing = extract_closing_balance(detected_bank, raw_text)
        return {
            "detected_bank": detected_bank,
            "parser_version": "0.3.1",
            "statement_period": _detect_period(raw_text),
            "raw_text": raw_text[:10000],
            "error": None,
            "transactions": transactions,
            # End-of-period balance AS PRINTED (None if unparsable); caller
            # signs it by account type for anchoring / reconciliation.
            "closing_balance": str(closing) if closing is not None else None,
        }
    finally:
        _reset_subaccount_names(_names_token)


def _extract_text_sync(content: bytes) -> str:
    """Synchronous helper: extract concatenated page text from a PDF."""
    import pdfplumber
    out = ""
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                out += t + "\n"
    return out


def _extract_text_and_words_sync(content: bytes) -> tuple[str, list[list[dict]]]:
    """Extract concatenated text PLUS per-page word lists with x/y coords.

    Word dicts come from pdfplumber's `extract_words()` and look like:
        {"text": "€500.00", "x0": 335.0, "x1": 371.0, "top": 306.0, "bottom": 318.0, ...}

    Column-aware parsers (e.g. Revolut) need this to distinguish multiple
    amount columns that get flattened into a single space-separated text row.
    """
    import pdfplumber
    text_parts: list[str] = []
    pages_words: list[list[dict]] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text_parts.append(t)
            try:
                pages_words.append(page.extract_words(use_text_flow=False))
            except Exception:
                pages_words.append([])
    return "\n".join(text_parts) + "\n", pages_words


def _empty_result(error: str | None = None) -> dict[str, Any]:
    return {
        "detected_bank": None,
        "parser_version": "0.2.0",
        "statement_period": None,
        "raw_text": "",
        "error": error,
        "transactions": [],
        "closing_balance": None,
    }


# ─── Bank detection ────────────────────────────────────────────────────


# Use ONLY bank-issued identifiers (BIC, official domain, exact card-product
# title, registered legal name). Plain bank names are too greedy — e.g. an N26
# statement may carry "AMERICAN EXPRESS" in a SEPA-rejection memo.
# Primary markers: strings the ISSUER prints to identify itself — statement
# title / company name / domain. A *counterparty* never prints these: a
# transfer line only carries the counterparty's BIC, not the issuer's statement
# title or company domain. Detection tries these first (earliest position).
_ISSUER_MARKERS: list[tuple[str, str]] = [
    ("amex_de",  "americanexpress.de"),
    ("amex_de",  "american express gold card"),
    ("n26",      "bank statement nr"),          # N26 statement title (header, always pos 0)
    ("revolut",  "revolut bank uab"),           # Revolut issuer company (header)
    ("tfbank",   "tfbank.de"),                  # issuer domain (not the "TF Bank AB" name a counterparty prints)
    ("advanzia", "hilton honors kreditkarte"),
    ("advanzia", "advanzia bank s.a"),
]

# Fallback: bare BICs. AMBIGUOUS — a BIC appears both as the issuer's own BIC
# (header/footer) AND as a *counterparty* BIC inside "Outgoing Transfers" lines
# (an N26 statement paying into a Revolut account carries `revodeb2` in the
# body, often BEFORE N26's own `ntsbdeb1`). So these are consulted ONLY when no
# issuer marker matched — a counterparty BIC can never override a real issuer.
# (2026-07 fix: N26 June statement mis-detected as Revolut because the earliest
# marker was a counterparty `revodeb2`, not N26's own footer BIC.)
_BIC_FALLBACK_MARKERS: list[tuple[str, str]] = [
    ("n26",      "ntsbdeb1"),                   # N26 BIC
    ("revolut",  "revodeb2"),                   # Revolut Germany BIC
    ("tfbank",   "tf bank ab"),                 # TF Bank company name (counterparty-shared)
]


def _earliest_marker(text_lower: str, markers: list[tuple[str, str]]) -> str | None:
    """Return the bank whose marker appears earliest in `text_lower`, or None."""
    best_bank: str | None = None
    best_pos = len(text_lower) + 1
    for bank, marker in markers:
        pos = text_lower.find(marker)
        if pos != -1 and pos < best_pos:
            best_pos = pos
            best_bank = bank
    return best_bank


def _detect_bank(text: str) -> str | None:
    """Detect the issuing bank: issuer self-identification first, BIC as fallback.

    Two tiers, because a bank's BIC is ambiguous — it identifies the issuer in
    the header AND a counterparty in transfer lines. Tier 1 uses issuer-only
    markers (statement title / company / domain) that a counterparty never
    prints; only if none match does Tier 2 fall back to bare BICs. This fixes
    the N26↔Revolut cross mis-detection where an N26 statement's transfer to a
    Revolut account put `revodeb2` earlier than N26's own footer BIC. No-match → None.
    """
    text_lower = text.lower()
    return _earliest_marker(text_lower, _ISSUER_MARKERS) or _earliest_marker(
        text_lower, _BIC_FALLBACK_MARKERS
    )


def _detect_period(text: str) -> str | None:
    # AMEX-DE: "Abrechnungszeitraum vom DD.MM.YY bis DD.MM.YY" (2-digit year, no space)
    m = re.search(r"vom\s*(\d{2})\.(\d{2})\.(\d{2})\s*bis", text)
    if m:
        return f"20{m.group(3)}-{m.group(2)}"
    # TFBank: "Zeitraum: DD.MM.YYYY - DD.MM.YYYY"
    m = re.search(r"Zeitraum:\s*(\d{2})\.(\d{2})\.(\d{4})", text)
    if m:
        return f"{m.group(3)}-{m.group(2)}"
    # Generic German "vom DD.MM.YYYY bis DD.MM.YYYY"
    m = re.search(r"vom\s*(\d{2})\.(\d{2})\.(\d{4})\s*bis", text)
    if m:
        return f"{m.group(3)}-{m.group(2)}"
    # Revolut English "April 1, 2026 to April 30, 2026"
    m = re.search(r"(\w{3,9})\s+\d{1,2},\s*(\d{4})\s+(?:to|until)", text)
    if m:
        mon = _MONTH_ABBR_EN.get(m.group(1)[:3].title())
        if mon:
            return f"{m.group(2)}-{mon:02d}"
    # N26 "01.04.2026 until 30.04.2026"
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})\s+until", text)
    if m:
        return f"{m.group(3)}-{m.group(2)}"
    return None


# ─── Shared utilities ──────────────────────────────────────────────────


_MONTH_ABBR_EN = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _de_amount(s: str) -> Decimal:
    """Parse German-formatted '1.234,56' / '12,40' or plain '12.40' to Decimal."""
    s = s.strip().replace(" ", "")
    if "," in s and "." in s:
        # 1.234,56 → 1234.56
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    return Decimal(s)


# Sub-account / vault keywords appearing in bank PDFs. When matched in a row's
# description we override `type` to "transfer" and tag `metadata_json` with
# subaccount=true so the balance view skips it (the money stays inside the
# same bank, just changes pocket).
_SUBACCOUNT_KEYWORDS = (
    # N26 Spaces / Vault
    "from saving", "to saving",
    "from spaces", "to spaces",
    "from main account", "to main account",
    # Revolut Pockets / Vaults / Instant Access Savings
    "from instant access savings", "to instant access savings",
    "from vault", "to vault",
    "from pocket", "to pocket",
    # Common user-customizable Space names (heuristic — best-effort defaults)
    "from investing", "to investing",
    "from dream list", "to dream list",
    "from emergency", "to emergency",
    "from saving space", "to saving space",
    # Generic
    "round-up",
    "savings interest",
    "internal transfer",
)
# Note: "interest paid to ..." is INTENTIONALLY excluded — it represents
# interest income credited to a Saving sub-account (net increase), not an
# A↔B internal transfer. It must flow through column-based income/expense
# detection so it actually credits the bank's overall balance.

# A row can MENTION a sub-account (by name or "to/from <space>" keyword) yet be
# interest credited / a fee charged on that space — NOT an internal move.
# Substring-matching the name would otherwise sweep e.g. Revolut
# 'Net interest paid to "Instant Access Savings"' into a subaccount transfer
# (which the balance view then ignores). These must stay income/expense.
# Guard is applied ONLY to sub-account detection, so cross-bank classification
# is unaffected (and a stray "fee" inside e.g. "coffee" can't suppress it).
_SUBACCOUNT_EXCLUDE_KEYWORDS = ("interest", "fee")


def _is_subaccount_excluded(desc: str) -> bool:
    """True when `desc` is interest/fee on a savings space, so it must NOT be
    classified as a sub-account move (it's income/expense)."""
    d = desc.lower()
    return any(k in d for k in _SUBACCOUNT_EXCLUDE_KEYWORDS)

# Per-task subaccount-name context. Was a module-level mutable list, which
# raced under concurrent ingestion (worker A's account names polluting
# worker B's classification). ContextVar gives every asyncio task a fresh
# copy without forcing every parser/_make_tx call site to add a parameter.
import contextvars as _ctxvars  # noqa: E402

_SUBACCOUNT_NAMES_VAR: _ctxvars.ContextVar[list[str]] = _ctxvars.ContextVar(
    "pdf_parser_subaccount_names", default=[]
)


def _set_subaccount_names(names: list[str]) -> _ctxvars.Token:
    """Bind a per-task subaccount-name list. Caller must `reset(token)`
    in a finally to keep the context stack clean."""
    return _SUBACCOUNT_NAMES_VAR.set(
        [n.strip().lower() for n in names if str(n).strip()]
    )


def _reset_subaccount_names(token: _ctxvars.Token) -> None:
    _SUBACCOUNT_NAMES_VAR.reset(token)


# Backward-compatible empty list for any external reader that imported the
# old name. Reading this no longer reflects per-task state.
_SUBACCOUNT_USER_NAMES: list[str] = []

# Cross-bank transfer cues (description-level). When matched we mark the row as
# a transfer right away, even before the matcher pairs it with a counterparty
# in another account.
_CROSS_BANK_TRANSFER_HINTS_BASE = (
    "outgoing transfer",
    "incoming transfer",
    "sepa direct debit",
    # Credit-card "we received your payment" entries — semantically a transfer
    # from a bank account into the credit-card balance, not a real expense
    # even though the AMEX/Advanzia parsers tag them as expense by default.
    # Examples seen in real statements:
    #   - "ZAHLUNG ERHALTEN. BESTEN DANK." (AMEX-DE)
    #   - "ZAHLUNGSEINGANG"                 (Advanzia)
    #   - "PAYMENT RECEIVED - THANK YOU"    (AMEX-EN)
    "zahlung erhalten",
    "zahlungseingang",
    "payment received",
    "thank you for your payment",
)


# Hints that strongly imply transfer DIRECTION (incoming vs outgoing). Used
# by `_classify_transfer` to set `transfer_direction` so the auto-pair
# matcher can put the row into the right outflow / inflow bucket.
_DIRECTION_IN_HINTS = (
    "zahlung erhalten",
    "zahlungseingang",
    "payment received",
    "thank you for your payment",
    "incoming transfer",
)
_DIRECTION_OUT_HINTS = (
    "outgoing transfer",
    "sepa direct debit",
)


def _cross_bank_transfer_hints() -> tuple[str, ...]:
    """Combine static cues with owner-name variants from settings.

    Owner names (e.g. "Jane Doe") are loaded from ``FINANCE_TRACKER_OWNER_NAMES``
    so the codebase ships without any personally identifying string baked in.
    """
    from app.core.config import get_settings

    extras: list[str] = []
    for name in get_settings().owner_names:
        extras.extend(
            [
                f"to {name}",
                f"from {name}",
                f"payment from {name}",
                f"payment to {name}",
            ]
        )
    return _CROSS_BANK_TRANSFER_HINTS_BASE + tuple(extras)


def _classify_transfer(desc: str, default_type: str) -> tuple[str, dict | None]:
    """Decide whether a row is a transfer based on description heuristics.

    Returns (final_type, metadata_dict_or_None).
      - sub-account keyword → ("transfer", {"subaccount": True})
      - cross-bank cue       → ("transfer", {"cross_bank_hint": True})
      - otherwise            → (default_type, None)
    """
    # Normalise: lowercase, strip quotes/punctuation that break substring match
    # (e.g. Revolut writes `to "Instant Access Savings"` with literal quotes,
    # which would otherwise prevent `"to instant access savings"` from matching).
    d = re.sub(r"[\"'`'']", "", desc.lower())
    d = re.sub(r"\s+", " ", d)
    # Interest/fee on a savings space mentions the sub-account but isn't a move
    # — skip sub-account detection so it stays income/expense. Cross-bank
    # detection (step 3) still runs.
    subaccount_ok = not _is_subaccount_excluded(d)
    # 1) User-maintained per-account sub-account names (highest precedence,
    # read from a per-task ContextVar set by `parse_pdf_statement` /
    # callers — async-safe).
    if subaccount_ok:
        for name in _SUBACCOUNT_NAMES_VAR.get():
            if name in d:
                return "transfer", {"subaccount": True, "matched": name, "source": "user_list"}
        # 2) Hard-coded common sub-account keywords
        for kw in _SUBACCOUNT_KEYWORDS:
            if kw in d:
                return "transfer", {"subaccount": True, "matched": kw, "source": "keyword"}
    # 3) Cross-bank cues. Also infer direction so the matcher's outflow /
    # inflow bucketing puts the row on the right side. Without direction,
    # an "ZAHLUNG ERHALTEN" row tagged transfer would land in BOTH buckets
    # via the directionless fallback, but its score against an OUT leg
    # in another account ends up below threshold for various reasons —
    # the explicit direction lifts the pairing reliability.
    for kw in _cross_bank_transfer_hints():
        if kw in d:
            meta: dict = {"cross_bank_hint": True, "matched": kw}
            if any(h in d for h in _DIRECTION_IN_HINTS):
                meta["transfer_direction"] = "in"
            elif any(h in d for h in _DIRECTION_OUT_HINTS):
                meta["transfer_direction"] = "out"
            else:
                # No directional verb in the text — fall back to the debit/credit
                # the statement already told us (encoded in `default_type`). A
                # transfer must ALWAYS carry a direction, else the balance view
                # and the 未配对 panel both default it to 'out' and silently
                # reverse the sign of income-origin transfers.
                meta["transfer_direction"] = "in" if default_type == "income" else "out"
            return "transfer", meta
    return default_type, None


def _make_tx(
    *,
    date_iso: str,
    amount: Decimal,
    tx_type: str,
    description: str,
    counterparty: str,
    seq: int,
    currency: str = "EUR",
    skip_classify: bool = False,
) -> dict:
    """Build a transaction dict.

    Auto-promotes `type` to 'transfer' when the description matches sub-account
    or cross-bank-transfer keywords (unless `skip_classify=True` — set by parsers
    that already know the row's direction from PDF column layout, e.g. Revolut).

    Why the opt-out: PDFs like Revolut record both legs of an internal transfer
    (Account section AND Deposit section both list the same row). If the parser
    pre-tags such a row as `subaccount=True`, the balance view skips it and
    we'd lose the legitimate +500/-500 that should net to zero on its own. The
    column-aware parser knows the direction is reliable and lets the amount-
    matcher pair the two legs into a proper `transfer` afterwards.
    """
    if skip_classify:
        final_type, metadata = tx_type, None
    else:
        final_type, metadata = _classify_transfer(description, tx_type)
    import json as _json
    return {
        "occurred_at": date_iso,
        "amount": str(abs(amount)),
        "currency": currency,
        "type": final_type,
        "description": description.strip(),
        "raw_description": description.strip(),
        "counterparty": None,
        "external_id": f"{counterparty.lower()}_{seq}",
        "metadata_json": _json.dumps(metadata) if metadata else None,
    }


# ─── IBAN extraction helper (2026-05-06) ───────────────────────────────
# Most non-Revolut bank statements put the counterparty IBAN on the line
# AFTER the transaction row, prefixed with "IBAN:" or "IBAN ". This
# helper looks for that pattern in the gap between two consecutive
# transaction matches and stitches the IBAN onto the row's raw_description
# so the transfer matcher can score the +40-pt IBAN-match bonus.
#
# Subtle: PDFs paginate, and page footers / legal sections also contain
# IBAN-shaped tokens (e.g. the user's own account IBAN appears in the
# N26 footer alongside their address; AMEX-DE's legal footer mentions
# the Amex company IBAN). We had a false-positive bug where a tx's tail
# spilled into the next page's header and grabbed the WRONG IBAN, so:
#   1. we require an explicit `IBAN[: ]` prefix before the digits, and
#   2. we truncate the tail at the first page-footer marker we recognise.
_IBAN_LABELED_RE = re.compile(
    r"\bIBAN\s*[:\s]\s*([A-Z]{2}\d{2}[A-Z0-9 ]{11,40})",
    re.IGNORECASE,
)

# Markers that signal we've crossed into a different section (page footer,
# legal disclaimer, next-page header, sub-account overview). Any IBAN
# appearing AFTER one of these inside the tail belongs to the bank /
# account itself, not the transaction's counterparty.
_TX_TAIL_TERMINATORS = (
    "Issued on",
    "Schivelbeiner",                # user's home address in N26 footer
    "Bank Statement",
    "Spaces Overview",
    "Spaces Ov",
    "Karten-Nr",
    "Saldo des laufenden Monats",
    "American Express Europe",      # AMEX-DE legal footer
    "Hinweise zu Ihrer Kartenabrechnung",
    "Postadresse Internet Kontakt",  # TFBank footer
)


def _extract_iban_in_window(text: str, start: int, end: int) -> str | None:
    """Return the cleaned IBAN (uppercase, no spaces) explicitly labeled
    with ``IBAN:`` in ``text[start:end]`` BEFORE any page-footer marker,
    or None.

    Returning None on a section-footer IBAN is intentional — that IBAN
    belongs to the bank / account itself, not to the transaction's
    counterparty."""
    if start >= end:
        return None
    snippet = text[start:end]
    # Truncate at the earliest page-footer marker so we don't grab an
    # IBAN from a footer or the next page's header.
    cut = len(snippet)
    for marker in _TX_TAIL_TERMINATORS:
        idx = snippet.find(marker)
        if idx >= 0 and idx < cut:
            cut = idx
    snippet = snippet[:cut]
    m = _IBAN_LABELED_RE.search(snippet)
    if not m:
        return None
    candidate = re.sub(r"\s+", "", m.group(1).upper())
    if 15 <= len(candidate) <= 34:
        return candidate
    return None


def _attach_iban_to_tx(tx: dict, iban: str | None) -> None:
    """Append `IBAN <X>` to a tx dict's raw_description if we found one.
    Idempotent: doesn't duplicate the IBAN if already present."""
    if not iban:
        return
    raw = tx.get("raw_description") or ""
    if iban in raw.upper().replace(" ", ""):
        return
    tx["raw_description"] = (raw + f" | IBAN {iban}").strip(" |")


# ─── AMEX-DE ────────────────────────────────────────────────────────────


_AMEX_ROW = re.compile(
    r"^(\d{2}\.\d{2})\s+(\d{2}\.\d{2})\s+(.+?)\s+(-?[\d.]+,\d{2})\s*$",
    re.MULTILINE,
)


def _parse_amex_de(text: str) -> list[dict]:
    txs: list[dict] = []

    # Year is in header: "Datum 08.04.26" → 2026
    yr_match = re.search(r"Datum[\s\S]{0,80}?(\d{2})\.(\d{2})\.(\d{2})", text)
    year = 2000 + int(yr_match.group(3)) if yr_match else datetime.now(timezone.utc).year

    seq = 0
    matches = list(_AMEX_ROW.finditer(text))
    for i, m in enumerate(matches):
        d_book, _d_post, desc, amt = m.groups()
        if any(skip in desc for skip in ["Saldo", "Karten-Nr", "Abrechnung"]):
            continue
        try:
            amount = _de_amount(amt)
        except Exception:
            continue
        if amount == 0:
            continue
        is_credit = (
            "GUTSCHRIFT" in desc.upper()
            or "ZAHLUNG/ÜBERWEISUNG ERHALTEN" in desc.upper()
        )
        tx_type = "income" if is_credit else "expense"
        dd, mm = d_book.split(".")
        try:
            date_iso = f"{year}-{mm}-{dd}T00:00:00Z"
        except Exception:
            continue
        seq += 1
        tx = _make_tx(
            date_iso=date_iso, amount=amount, tx_type=tx_type,
            description=desc, counterparty="AMEX-DE", seq=seq,
        )
        tail_start = m.end()
        tail_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        _attach_iban_to_tx(tx, _extract_iban_in_window(text, tail_start, tail_end))
        txs.append(tx)
    return txs


# ─── N26 ────────────────────────────────────────────────────────────────


_N26_ROW = re.compile(
    r"^(.+?)\s+(\d{2}\.\d{2}\.\d{4})\s+([+-][\d.]+,\d{2})€\s*$",
    re.MULTILINE,
)


def _parse_n26(text: str) -> list[dict]:
    txs: list[dict] = []
    seq = 0
    matches = list(_N26_ROW.finditer(text))
    for i, m in enumerate(matches):
        desc, date_str, amt = m.groups()
        try:
            amount = _de_amount(amt)
        except Exception:
            continue
        if amount == 0:
            continue
        dd, mm, yyyy = date_str.split(".")
        date_iso = f"{yyyy}-{mm}-{dd}T00:00:00Z"
        tx_type = "income" if amount > 0 else "expense"
        seq += 1
        tx = _make_tx(
            date_iso=date_iso, amount=amount, tx_type=tx_type,
            description=desc, counterparty="N26", seq=seq,
        )
        # Look for an IBAN in the continuation lines that follow this row
        # but precede the next match — typical N26 layout puts
        # counterparty IBAN one line below the date/amount row.
        tail_start = m.end()
        tail_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        _attach_iban_to_tx(tx, _extract_iban_in_window(text, tail_start, tail_end))
        txs.append(tx)
    return txs


# ─── Revolut ────────────────────────────────────────────────────────────


_REVOLUT_ROW = re.compile(
    r"^(\w{3}) (\d{1,2}), (\d{4})\s+(.+?)\s+€([\d,]+\.\d{2})\s+€[\d,]+\.\d{2}\s*$",
    re.MULTILINE,
)
_REVOLUT_INCOME_KEYWORDS = (
    "deposit", "top-up", "top up", "from ", "payment from", "incoming",
    "from instant access", "from saving", "salary",
)


def _parse_revolut(text: str) -> list[dict]:
    """Legacy text-based Revolut parser. Kept for fallback / dispatch table.

    NOTE: this can't reliably distinguish Money-out vs Money-in columns when
    pdfplumber flattens the table to text — it relies on weak description
    heuristics. The column-aware `_parse_revolut_columns` (used in the public
    `parse_pdf_statement` entry) supersedes this for real Revolut PDFs.
    """
    txs: list[dict] = []
    seq = 0
    for m in _REVOLUT_ROW.finditer(text):
        mon, day, year, desc, amt = m.groups()
        if mon not in _MONTH_ABBR_EN:
            continue
        try:
            amount = Decimal(amt.replace(",", ""))
        except Exception:
            continue
        if amount == 0:
            continue
        date_iso = f"{int(year)}-{_MONTH_ABBR_EN[mon]:02d}-{int(day):02d}T00:00:00Z"
        desc_lower = desc.lower()
        tx_type = "income" if any(k in desc_lower for k in _REVOLUT_INCOME_KEYWORDS) else "expense"
        seq += 1
        txs.append(_make_tx(
            date_iso=date_iso, amount=amount, tx_type=tx_type,
            description=desc, counterparty="Revolut", seq=seq,
        ))


# Column X-coordinate ranges learned from Revolut's standard German statement
# layout (header words "Money out" at x≈335, "Money in" at x≈417, "Balance"
# at x≈526). We use a tolerance band rather than exact match.
_REVOLUT_COL_OUT_X0 = (320.0, 400.0)
_REVOLUT_COL_IN_X0 = (405.0, 475.0)
_REVOLUT_COL_BAL_X0 = (480.0, 580.0)


def _parse_revolut_columns(pages_words: list[list[dict]]) -> list[dict]:
    """Column-aware Revolut parser using word x-coordinates.

    Algorithm:
      1. Group words from all pages by their `top` (y-coordinate, rounded) →
         each group is one logical row of the PDF.
      2. For each row, find the date pattern at the start, the description
         text in the middle, and any €-prefixed amounts whose `x0` falls into
         the Money out / Money in / Balance bands.
      3. Direction is determined by which column the amount sits in — never
         by description text. This eliminates the entire "is 'paid to' an
         outflow or inflow?" ambiguity.

    Sub-account moves (e.g. "Net interest paid to 'Instant Access Savings'")
    still flow through `_make_tx` → `_classify_transfer`, so the generic
    keyword + user-list logic continues to tag them as `subaccount=true`.
    """
    txs: list[dict] = []
    seq = 0
    last_tx: dict | None = None

    # Continuation lines starting with these tokens carry counterparty IBAN /
    # routing context that we want appended to the previous transaction's
    # description, so the transfer matcher can find the IBAN later.
    _CONTINUATION_PREFIXES = ("Reference:", "From:", "To:", "IBAN", "BIC")

    for words in pages_words:
        # Bucket words by rounded `top` (y-position of the line)
        rows: dict[int, list[dict]] = {}
        for w in words:
            key = round(w["top"])
            rows.setdefault(key, []).append(w)

        # Sort rows top→bottom; sort words within each row left→right
        for top in sorted(rows):
            row = sorted(rows[top], key=lambda w: w["x0"])
            row_text = " ".join(w["text"] for w in row)

            # Match leading date "Mon DD, YYYY"
            m = re.match(r"^(\w{3})\s+(\d{1,2}),\s+(\d{4})\s+(.+)", row_text)
            if not m:
                # Possible continuation line: append to last_tx.raw_description
                if last_tx is not None and any(
                    row_text.lstrip().startswith(p) for p in _CONTINUATION_PREFIXES
                ):
                    last_tx["raw_description"] = (last_tx.get("raw_description") or "") + " | " + row_text.strip()
                continue
            mon, day, year, _rest = m.groups()
            if mon not in _MONTH_ABBR_EN:
                continue

            # Pick € amounts whose x0 lies in money-out / money-in column bands
            money_out: Decimal | None = None
            money_in: Decimal | None = None
            for w in row:
                if not w["text"].startswith("€"):
                    continue
                try:
                    amt = Decimal(w["text"][1:].replace(",", ""))
                except Exception:
                    continue
                if _REVOLUT_COL_OUT_X0[0] <= w["x0"] <= _REVOLUT_COL_OUT_X0[1]:
                    money_out = amt
                elif _REVOLUT_COL_IN_X0[0] <= w["x0"] <= _REVOLUT_COL_IN_X0[1]:
                    money_in = amt
                # Balance column is ignored.

            if money_out is None and money_in is None:
                continue
            if money_out and money_in:
                # Shouldn't happen in well-formed rows; pick the larger as primary
                continue

            # Description = words between date and the first € amount
            date_word_count = 3  # Mon, DD,, YYYY
            desc_words: list[str] = []
            for w in row[date_word_count:]:
                if w["text"].startswith("€"):
                    break
                desc_words.append(w["text"])
            description = " ".join(desc_words).strip()
            if not description:
                continue

            if money_in is not None:
                amount = money_in
                tx_type = "income"
            else:
                amount = money_out  # type: ignore[assignment]
                tx_type = "expense"
            if amount is None or amount == 0:
                continue

            try:
                date_iso = f"{int(year)}-{_MONTH_ABBR_EN[mon]:02d}-{int(day):02d}T00:00:00Z"
            except Exception:
                continue
            seq += 1
            tx = _make_tx(
                date_iso=date_iso, amount=amount, tx_type=tx_type,
                description=description, counterparty="Revolut", seq=seq,
                # Revolut PDF lists BOTH legs of internal transfers (one in
                # Account section, one in Deposit section). Trust the column
                # direction; let amount-matcher pair them into transfer/net-0
                # rather than pre-tagging as subaccount (which would skip the
                # balance view and miss legitimate +/− changes).
                skip_classify=True,
            )
            txs.append(tx)
            last_tx = tx
    return txs


# ─── TFBank ─────────────────────────────────────────────────────────────


_TFBANK_ROW = re.compile(
    r"^(\d+)\s+(\d{2}\.\d{2}\.\d{4})\s+(\d{2}\.\d{2}\.\d{4})\s+(.+?)\s+([\d.,]+)\s+EUR\s+(-?[\d.,]+)\s*(D|KR)\b",
    re.MULTILINE,
)


def _parse_tfbank(text: str) -> list[dict]:
    txs: list[dict] = []
    seq = 0
    matches = list(_TFBANK_ROW.finditer(text))
    for i, m in enumerate(matches):
        _txid, d_book, _d_post, desc, _amt_orig, amt_eur, dc = m.groups()
        try:
            amount = _de_amount(amt_eur)
        except Exception:
            continue
        if amount == 0:
            continue
        dd, mm, yyyy = d_book.split(".")
        date_iso = f"{yyyy}-{mm}-{dd}T00:00:00Z"
        # D = Debit (expense), KR = Credit (refund / income)
        tx_type = "income" if dc == "KR" else "expense"
        # Strip "Wechselkurs 1.00000" tail from desc
        desc = re.sub(r"\s*Wechselkurs\s+[\d.]+\s*$", "", desc).strip()
        seq += 1
        tx = _make_tx(
            date_iso=date_iso, amount=amount, tx_type=tx_type,
            description=desc, counterparty="TFBank", seq=seq,
        )
        tail_start = m.end()
        tail_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        _attach_iban_to_tx(tx, _extract_iban_in_window(text, tail_start, tail_end))
        txs.append(tx)
    return txs


# ─── Advanzia ───────────────────────────────────────────────────────────


_ADVANZIA_ROW = re.compile(
    r"^(\d{2}\.\d{2}\.\d{4})\s+(.+?)\s+(-?[\d.]+,\d{2})\s*$",
    re.MULTILINE,
)
_ADVANZIA_SKIP = ("ALTER SALDO", "NEUER SALDO", "MINDESTBETRAG")


def _parse_advanzia(text: str) -> list[dict]:
    txs: list[dict] = []
    seq = 0
    matches = list(_ADVANZIA_ROW.finditer(text))
    for i, m in enumerate(matches):
        date_str, desc, amt = m.groups()
        if any(skip in desc.upper() for skip in _ADVANZIA_SKIP):
            continue
        try:
            amount = _de_amount(amt)
        except Exception:
            continue
        if amount == 0:
            continue
        dd, mm, yyyy = date_str.split(".")
        date_iso = f"{yyyy}-{mm}-{dd}T00:00:00Z"
        # Negative amount = credit (e.g. EINZAHLUNG repayment); positive = purchase (expense).
        tx_type = "income" if amount < 0 else "expense"
        seq += 1
        _adv_tail_start = m.end()
        _adv_tail_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        _adv_iban = _extract_iban_in_window(text, _adv_tail_start, _adv_tail_end)
        tx = _make_tx(
            date_iso=date_iso, amount=amount, tx_type=tx_type,
            description=desc, counterparty="Advanzia", seq=seq,
        )
        _attach_iban_to_tx(tx, _adv_iban)
        txs.append(tx)
    return txs


# ─── Generic fallback ──────────────────────────────────────────────────


def _parse_generic(text: str) -> list[dict]:
    """Heuristic parser for unrecognised banks. Best-effort, no guarantees."""
    txs: list[dict] = []
    seq = 0
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        date_match = re.search(r"(\d{4}[/-]\d{2}[/-]\d{2})", line)
        if not date_match:
            continue
        date = date_match.group(1).replace("/", "-")
        after = line[date_match.end():].strip()
        amount_match = re.search(r"([+-]?\d+[.,]\d{2})", after)
        if not amount_match:
            continue
        amt_str = amount_match.group(1)
        desc = after[:amount_match.start()].strip()
        if not desc:
            continue
        try:
            amount = _de_amount(amt_str)
        except Exception:
            continue
        if amount == 0:
            continue
        tx_type = "income" if amount > 0 else "expense"
        seq += 1
        txs.append(_make_tx(
            date_iso=f"{date}T00:00:00Z", amount=amount, tx_type=tx_type,
            description=desc, counterparty="Generic", seq=seq, currency="EUR",
        ))
    return txs


# ─── Dispatch table ────────────────────────────────────────────────────


_BANK_PARSERS = {
    "amex_de": _parse_amex_de,
    "n26": _parse_n26,
    "revolut": _parse_revolut,
    "tfbank": _parse_tfbank,
    "advanzia": _parse_advanzia,
}


# ─── Closing-balance extraction (for balance anchoring / reconciliation) ──
#
# The statement's end-of-period balance lets us anchor account.initial_balance
# to reality and flag book-keeping drift. Each bank prints it differently and
# the SIGN is bank-specific (asset accounts print the held amount; credit cards
# print the amount owed, sometimes positive, sometimes negative). We return the
# number AS PRINTED here; the caller normalises the sign by account type
# (credit_card → −abs, since the ledger stores card debt as negative).
#
# Returns None when no balance can be parsed (e.g. Revolut, whose closing
# balance lives in a positional column) — callers then simply skip
# reconciliation, degrading gracefully.


def _closing_n26(text: str) -> Decimal | None:
    # N26 prints "Previous balance" / "Your new balance" per pocket (main
    # account + each Space). Money moved into a Space stays inside N26 and the
    # ledger ignores those moves, so the account total == main + all Spaces ==
    # the SUM of every "Your new balance".
    vals = re.findall(r"Your new balance\s+([+-]?[\d.,]+)\s*€", text)
    if not vals:
        return None
    total = Decimal("0")
    for v in vals:
        total += _de_amount(v.lstrip("+"))
    return total


def _closing_amex_de(text: str) -> Decimal | None:
    # "Saldo des laufenden Monats fürHERRN JINGSHENG CHEN 548,72"
    m = re.search(r"Saldo des laufenden Monats für\S.*?([\d.,]+)\s*$", text, re.M)
    return _de_amount(m.group(1)) if m else None


def _closing_tfbank(text: str) -> Decimal | None:
    # "Neuer Saldo: -855.87" (TFBank uses dot decimals + space thousands)
    m = re.search(r"Neuer Saldo:\s*(-?[\d.,\s]+?)\s*(?:\n|$)", text)
    return _de_amount(m.group(1)) if m else None


def _closing_advanzia(text: str) -> Decimal | None:
    # "03.04.2026 NEUER SALDO 371,41"
    m = re.search(r"NEUER SALDO\s+([\d.,]+)", text)
    return _de_amount(m.group(1)) if m else None


_CLOSING_BALANCE_EXTRACTORS = {
    "n26": _closing_n26,
    "amex_de": _closing_amex_de,
    "tfbank": _closing_tfbank,
    "advanzia": _closing_advanzia,
    # "revolut": positional column — skipped (graceful None).
}


def extract_closing_balance(detected_bank: str | None, text: str) -> Decimal | None:
    """Best-effort end-of-period balance, AS PRINTED (caller signs by acct type)."""
    fn = _CLOSING_BALANCE_EXTRACTORS.get(detected_bank or "")
    if fn is None:
        return None
    try:
        return fn(text)
    except (ArithmeticError, ValueError):
        return None
