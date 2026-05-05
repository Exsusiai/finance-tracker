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
    # Stash for transaction-level use (read in `_make_tx`)
    _SUBACCOUNT_USER_NAMES.clear()
    if subaccount_names:
        _SUBACCOUNT_USER_NAMES.extend(n.strip().lower() for n in subaccount_names if n.strip())

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

    return {
        "detected_bank": detected_bank,
        "parser_version": "0.3.0",
        "statement_period": _detect_period(raw_text),
        "raw_text": raw_text[:10000],
        "error": None,
        "transactions": transactions,
    }


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
    }


# ─── Bank detection ────────────────────────────────────────────────────


# Use ONLY bank-issued identifiers (BIC, official domain, exact card-product
# title, registered legal name). Plain bank names are too greedy — e.g. an N26
# statement may carry "AMERICAN EXPRESS" in a SEPA-rejection memo.
_BANK_MARKERS: list[tuple[str, str]] = [
    ("amex_de",  "americanexpress.de"),
    ("amex_de",  "american express gold card"),
    ("n26",      "ntsbdeb1"),                  # N26 BIC
    ("revolut",  "revolut bank uab"),
    ("revolut",  "revodeb2"),                  # Revolut Germany BIC
    ("tfbank",   "tfbank.de"),
    ("tfbank",   "tf bank ab"),
    ("advanzia", "advanzia bank s.a"),
    ("advanzia", "hilton honors kreditkarte"),
]


def _detect_bank(text: str) -> str | None:
    text_lower = text.lower()
    for bank, marker in _BANK_MARKERS:
        if marker in text_lower:
            return bank
    return None


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

# Mutable list filled by `parse_pdf_statement` from the caller-supplied
# per-account `subaccount_names`. Lower-cased.
_SUBACCOUNT_USER_NAMES: list[str] = []

# Cross-bank transfer cues (description-level). When matched we mark the row as
# a transfer right away, even before the matcher pairs it with a counterparty
# in another account.
_CROSS_BANK_TRANSFER_HINTS = (
    "outgoing transfer",
    "incoming transfer",
    "sepa direct debit",
    "to jingsheng chen",         # owner-to-owner self-transfer
    "from jingsheng chen",
    "payment from jingsheng chen",
    "payment to jingsheng chen",
)


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
    # 1) User-maintained per-account sub-account names (highest precedence)
    for name in _SUBACCOUNT_USER_NAMES:
        if name in d:
            return "transfer", {"subaccount": True, "matched": name, "source": "user_list"}
    # 2) Hard-coded common sub-account keywords
    for kw in _SUBACCOUNT_KEYWORDS:
        if kw in d:
            return "transfer", {"subaccount": True, "matched": kw, "source": "keyword"}
    # 3) Cross-bank cues
    for kw in _CROSS_BANK_TRANSFER_HINTS:
        if kw in d:
            return "transfer", {"cross_bank_hint": True, "matched": kw}
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
    for m in _AMEX_ROW.finditer(text):
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
        txs.append(_make_tx(
            date_iso=date_iso, amount=amount, tx_type=tx_type,
            description=desc, counterparty="AMEX-DE", seq=seq,
        ))
    return txs


# ─── N26 ────────────────────────────────────────────────────────────────


_N26_ROW = re.compile(
    r"^(.+?)\s+(\d{2}\.\d{2}\.\d{4})\s+([+-][\d.]+,\d{2})€\s*$",
    re.MULTILINE,
)


def _parse_n26(text: str) -> list[dict]:
    txs: list[dict] = []
    seq = 0
    for m in _N26_ROW.finditer(text):
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
        txs.append(_make_tx(
            date_iso=date_iso, amount=amount, tx_type=tx_type,
            description=desc, counterparty="N26", seq=seq,
        ))
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
            txs.append(_make_tx(
                date_iso=date_iso, amount=amount, tx_type=tx_type,
                description=description, counterparty="Revolut", seq=seq,
                # Revolut PDF lists BOTH legs of internal transfers (one in
                # Account section, one in Deposit section). Trust the column
                # direction; let amount-matcher pair them into transfer/net-0
                # rather than pre-tagging as subaccount (which would skip the
                # balance view and miss legitimate +/− changes).
                skip_classify=True,
            ))
    return txs


# ─── TFBank ─────────────────────────────────────────────────────────────


_TFBANK_ROW = re.compile(
    r"^(\d+)\s+(\d{2}\.\d{2}\.\d{4})\s+(\d{2}\.\d{2}\.\d{4})\s+(.+?)\s+([\d.,]+)\s+EUR\s+(-?[\d.,]+)\s*(D|KR)\b",
    re.MULTILINE,
)


def _parse_tfbank(text: str) -> list[dict]:
    txs: list[dict] = []
    seq = 0
    for m in _TFBANK_ROW.finditer(text):
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
        txs.append(_make_tx(
            date_iso=date_iso, amount=amount, tx_type=tx_type,
            description=desc, counterparty="TFBank", seq=seq,
        ))
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
    for m in _ADVANZIA_ROW.finditer(text):
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
        txs.append(_make_tx(
            date_iso=date_iso, amount=amount, tx_type=tx_type,
            description=desc, counterparty="Advanzia", seq=seq,
        ))
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
