"""PDF parser engine — detects bank and delegates to bank-specific parser."""

from __future__ import annotations

import io
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PdfImport


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
        import pdfplumber
    except ImportError:
        return {
            "detected_bank": None,
            "parser_version": "0.1.0",
            "statement_period": None,
            "raw_text": "",
            "error": "pdfplumber not installed",
            "transactions": [],
        }

    # Extract text
    raw_text = ""
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    raw_text += page_text + "\n"
    except Exception as e:
        return {
            "detected_bank": None,
            "parser_version": "0.1.0",
            "statement_period": None,
            "raw_text": "",
            "error": f"Failed to extract text: {e}",
            "transactions": [],
        }

    # Detect bank from text features
    detected_bank = _detect_bank(raw_text)

    # Parse transactions based on detected bank
    transactions = []
    if detected_bank:
        transactions = _parse_for_bank(detected_bank, raw_text)
    else:
        # Generic heuristic parser
        transactions = _parse_generic(raw_text)

    return {
        "detected_bank": detected_bank,
        "parser_version": "0.1.0",
        "statement_period": _detect_period(raw_text),
        "raw_text": raw_text[:10000],  # Store first 10k chars
        "error": None,
        "transactions": transactions,
    }


def _detect_bank(text: str) -> str | None:
    """Detect bank from text features."""
    text_lower = text.lower()
    bank_markers = {
        "icbc": ["工商银行", "中国工商银行", "icbc"],
        "cmb": ["招商银行", "china merchants bank", "cmb"],
        "ccb": ["建设银行", "中国建设银行", "ccb"],
        "boc": ["中国银行", "bank of china", "boc"],
        "n26": ["n26", "n26 bank"],
        "revolut": ["revolut"],
    }
    for bank, markers in bank_markers.items():
        for marker in markers:
            if marker.lower() in text_lower:
                return bank
    return None


def _detect_period(text: str) -> str | None:
    """Try to extract statement period from text."""
    import re
    # Match patterns like "2026年04月" or "2026-04" or "April 2026"
    patterns = [
        r"(\d{4})年(\d{2})月",
        r"(\d{4})-(\d{2})",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return f"{m.group(1)}-{m.group(2)}"
    return None


def _parse_for_bank(bank: str, text: str) -> list[dict]:
    """Bank-specific parsing stubs.
    These will be fleshed out in P1 phase with real bank-specific parsers."""
    # For now, fall back to generic
    return _parse_generic(text)


def _parse_generic(text: str) -> list[dict]:
    """Generic heuristic parser — extracts transaction-like lines.
    
    This is a placeholder. Real implementation will use table extraction
    and bank-specific regex patterns.
    """
    transactions = []
    
    # Try to extract tables first
    try:
        import pdfplumber
        import io
        # Note: we'd need to re-open the PDF here for table extraction
        # For now, return empty — the real parser will be in bank-specific files
    except ImportError:
        pass
    
    return transactions
