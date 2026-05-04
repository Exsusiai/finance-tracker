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
) -> dict[str, Any]:
    """Parse a PDF bank statement.

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
        raw_text = await asyncio.to_thread(_extract_text_sync, content)
    except Exception as e:
        return _empty_result(error=f"Failed to extract text: {e}")

    detected_bank = _detect_bank(raw_text)

    if detected_bank:
        parser_func = _BANK_PARSERS.get(detected_bank, _parse_generic)
        transactions = parser_func(raw_text)
    else:
        transactions = _parse_generic(raw_text)

    return {
        "detected_bank": detected_bank,
        "parser_version": "0.2.0",
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


def _make_tx(
    *,
    date_iso: str,
    amount: Decimal,
    tx_type: str,
    description: str,
    counterparty: str,
    seq: int,
    currency: str = "EUR",
) -> dict:
    """Build a transaction dict.

    `counterparty` here is the **issuing bank** (AMEX-DE / N26 / ...), used only
    for `external_id` namespacing — NOT written to the `counterparty` column,
    because that column is semantically the actual merchant. The merchant name
    sits in `description` for these PDFs.
    """
    return {
        "occurred_at": date_iso,
        "amount": str(abs(amount)),
        "currency": currency,
        "type": tx_type,
        "description": description.strip(),
        "raw_description": description.strip(),
        # Leave counterparty NULL so auto-learn extracts keywords from `description`
        # (the real merchant text) rather than from the issuing bank's name.
        "counterparty": None,
        "external_id": f"{counterparty.lower()}_{seq}",
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
