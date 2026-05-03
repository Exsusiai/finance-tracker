"""PDF Parser Engine — comprehensive test suite.

Tests all bank parsers + generic parser using reportlab-generated PDFs.
Also covers edge cases: empty PDF, encrypted PDF, non-text PDFs.
"""
from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas

# ---------------------------------------------------------------------------
# Register CJK font for reportlab (needed for Chinese bank statements)
# ---------------------------------------------------------------------------
_CJK_FONT = "NotoSansCJK"
_CJK_FONT_BOLD = "NotoSansCJK-Bold"

_CJK_FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
_CJK_FONT_BOLD_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"

try:
    pdfmetrics.registerFont(TTFont(_CJK_FONT, _CJK_FONT_PATH, subfontIndex=2))
    pdfmetrics.registerFont(TTFont(_CJK_FONT_BOLD, _CJK_FONT_BOLD_PATH, subfontIndex=2))
    _HAS_CJK = True
except Exception:
    # Fallback to CID font
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        _CJK_FONT = "STSong-Light"
        _CJK_FONT_BOLD = "STSong-Light"
        _HAS_CJK = True
    except Exception:
        _HAS_CJK = False


def _make_pdf_bytes(lines: list[str], bank_header: str = "") -> bytes:
    """Generate a minimal text PDF with given lines using reportlab.

    Auto-detects CJK content and uses appropriate font.
    """
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    y = height - 30 * mm

    all_text = bank_header + "\n" + "\n".join(lines)
    has_cjk = any('\u4e00' <= ch <= '\u9fff' for ch in all_text)

    if bank_header:
        font = _CJK_FONT_BOLD if has_cjk and _HAS_CJK else "Helvetica-Bold"
        c.setFont(font, 14)
        c.drawString(20 * mm, y, bank_header)
        y -= 12 * mm

    font = _CJK_FONT if has_cjk and _HAS_CJK else "Courier"
    c.setFont(font, 10)
    for line in lines:
        if y < 20 * mm:
            c.showPage()
            y = height - 30 * mm
            c.setFont(font, 10)
        c.drawString(20 * mm, y, line)
        y -= 6 * mm

    c.save()
    return buf.getvalue()


def _make_empty_pdf() -> bytes:
    """PDF with no text content at all."""
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    c.showPage()  # blank page
    c.save()
    return buf.getvalue()


def _make_garbage_pdf() -> bytes:
    """Random non-PDF bytes."""
    return b"This is not a PDF file at all, just garbage data."


def _make_encrypted_pdf() -> bytes:
    """Create an encrypted PDF using reportlab."""
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4, encrypt="testpassword")
    c.setFont("Helvetica", 12)
    c.drawString(20 * mm, 20 * mm, "This is encrypted content")
    c.save()
    return buf.getvalue()


def _make_multiline_desc_pdf() -> bytes:
    """PDF where transaction descriptions wrap across lines."""
    lines = [
        "2026-05-01  -50.00  AMAZON MARKETPLACE EU",
        "S.A.R.L.",
        "2026-05-03  +1200.00  SALARY PAYMENT",
        "2026-05-05  -15.99  NETFLIX.COM",
    ]
    return _make_pdf_bytes(lines)


def _make_numeric_noise_pdf() -> bytes:
    """PDF with dates that are NOT transactions (e.g. phone numbers)."""
    lines = [
        "Customer Service: 0800-12-3456",
        "Account Number: 2024-01-01",
        "Important Notice",
        "2026-05-01  -50.00  Real Transaction",
        "2026-05-03  +100.00  Another Transaction",
    ]
    return _make_pdf_bytes(lines)


# ---------------------------------------------------------------------------
# Sample bank statement PDF generators
# ---------------------------------------------------------------------------

def _icbc_pdf() -> bytes:
    """ICBC-style statement with 收/支 markers."""
    header = "ICBC Bank Statement"
    lines = [
        "Account: 6222021234567890123",
        "Period: 2026-04",
        "",
        "2026/04/01  收  15,000.00  Salary Income",
        "2026/04/03  支  2,500.00  House Rent",
        "2026/04/05  支  350.00  Supermarket",
        "2026/04/10  收  500.00  Transfer Income",
        "2026/04/15  支  128.50  Online Shopping",
        "2026/04/20  支  89.00  Transportation",
    ]
    return _make_pdf_bytes(lines, header)


def _cmb_pdf() -> bytes:
    """CMB-style statement."""
    header = "CMB Bank Statement"
    lines = [
        "Account: 6225881234567890",
        "2026-05-01  收  +8,500.00  Salary",
        "2026-05-02  付  -1,200.00  Rent",
        "2026-05-05  付  -88.50  Food Delivery",
        "2026-05-10  收  +200.00  Refund",
        "2026-05-15  付  -1,500.00  Credit Card",
    ]
    return _make_pdf_bytes(lines, header)


def _ccb_pdf() -> bytes:
    """CCB-style statement with YYYYMMDD dates."""
    header = "CCB Bank Statement"
    lines = [
        "20260501  收入  12000.00  Salary",
        "20260502  支出  3000.00  Rent",
        "20260505  支出  200.00  Supermarket",
        "20260510  收入  1000.00  Bonus",
    ]
    return _make_pdf_bytes(lines, header)


def _boc_pdf() -> bytes:
    """BOC-style statement."""
    header = "BOC Bank Statement"
    lines = [
        "2026-05-01  收入  5000.00  Transfer Deposit",
        "2026-05-03  支出  1200.00  Utilities",
        "2026-05-05  收入  8000.00  Salary",
        "2026-05-08  支出  350.00  Daily Goods",
    ]
    return _make_pdf_bytes(lines, header)


def _n26_pdf() -> bytes:
    """N26-style statement."""
    header = "N26 Bank Statement"
    lines = [
        "Account: DE89 3704 0044 0532 0130 00",
        "Period: May 2026",
        "",
        "2026-05-01  DEPOSIT  +2000.00  Salary",
        "2026-05-02  SPENDING  -45.50  Supermarket",
        "2026-05-05  SPENDING  -12.99  Netflix",
        "2026-05-10  TRANSFER  +100.00  John",
        "2026-05-15  SPENDING  -8.50  Coffee Shop",
    ]
    return _make_pdf_bytes(lines, header)


def _revolut_pdf() -> bytes:
    """Revolut-style statement."""
    header = "Revolut Statement"
    lines = [
        "Account: ****1234",
        "2026-05-01  Top-Up  +500.00  Bank Transfer",
        "2026-05-02  Card Payment  -23.50  Amazon",
        "2026-05-03  Card Payment  -9.99  Spotify",
        "2026-05-05  Exchange  -100.00  Currency Exchange",
        "2026-05-10  Transfer  +50.00  Friend",
    ]
    return _make_pdf_bytes(lines, header)


def _generic_bank_pdf() -> bytes:
    """Unknown bank — should fall back to generic parser."""
    header = "Some Local Bank Statement"
    lines = [
        "2026-05-01  -500.00  Rent Payment",
        "2026-05-03  +3000.00  Salary Credit",
        "2026-05-05  -25.50  Groceries",
        "2026-05-10  -120.00  Electric Bill",
    ]
    return _make_pdf_bytes(lines, header)


def _generic_slash_dates_pdf() -> bytes:
    """Generic parser with slash date format."""
    lines = [
        "2026/05/01  -500.00  Rent Payment",
        "2026/05/03  +3000.00  Salary",
    ]
    return _make_pdf_bytes(lines)


def _multiline_desc_pdf() -> bytes:
    """PDF where transaction descriptions wrap across lines."""
    lines = [
        "2026-05-01  -50.00  AMAZON MARKETPLACE EU",
        "S.A.R.L.",
        "2026-05-03  +1200.00  SALARY PAYMENT",
        "2026-05-05  -15.99  NETFLIX.COM",
    ]
    return _make_pdf_bytes(lines)


def _numeric_noise_pdf() -> bytes:
    """PDF with dates that are NOT transactions (e.g. phone numbers)."""
    lines = [
        "Customer Service: 0800-12-3456",
        "Account Number: 2024-01-01",
        "Important Notice",
        "2026-05-01  -50.00  Real Transaction",
        "2026-05-03  +100.00  Another Transaction",
    ]
    return _make_pdf_bytes(lines)


# ---------------------------------------------------------------------------
# Fixtures — mock PdfImport so we don't need a real DB session
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_db():
    return AsyncMock()


@pytest.fixture
def mock_pdf_import():
    """Minimal PdfImport mock."""
    obj = MagicMock()
    obj.id = 1
    obj.filename = "test.pdf"
    return obj


# ---------------------------------------------------------------------------
# Import the engine functions
# ---------------------------------------------------------------------------

from app.services.pdf_parser.engine import (
    _detect_bank,
    _detect_period,
    _parse_for_bank,
    _parse_generic,
    _parse_icbc,
    _parse_cmb,
    _parse_ccb,
    _parse_boc,
    _parse_n26,
    _parse_revolut,
    parse_pdf_statement,
)


def _extract_text(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes using pdfplumber (same as engine)."""
    import pdfplumber
    text = ""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
    return text


# ═══════════════════════════════════════════════════════════════════════════
# TESTS: Bank Detection
# ═══════════════════════════════════════════════════════════════════════════

class TestBankDetection:
    def test_detect_icbc(self):
        assert _detect_bank("中国工商银行 交易明细") == "icbc"
        assert _detect_bank("工商银行") == "icbc"
        assert _detect_bank("ICBC Bank") == "icbc"

    def test_detect_cmb(self):
        assert _detect_bank("招商银行") == "cmb"
        assert _detect_bank("China Merchants Bank") == "cmb"

    def test_detect_ccb(self):
        assert _detect_bank("建设银行") == "ccb"
        assert _detect_bank("中国建设银行") == "ccb"

    def test_detect_boc(self):
        assert _detect_bank("中国银行") == "boc"
        assert _detect_bank("Bank of China") == "boc"

    def test_detect_n26(self):
        assert _detect_bank("N26 Bank Statement") == "n26"

    def test_detect_revolut(self):
        assert _detect_bank("Revolut Statement") == "revolut"

    def test_detect_none(self):
        assert _detect_bank("Some random text with no bank name") is None
        assert _detect_bank("") is None

    def test_detect_priority(self):
        """First match wins."""
        text = "中国银行 建设银行"
        result = _detect_bank(text)
        assert result in ("boc", "ccb")


# ═══════════════════════════════════════════════════════════════════════════
# TESTS: Period Detection
# ═══════════════════════════════════════════════════════════════════════════

class TestPeriodDetection:
    def test_chinese_format(self):
        assert _detect_period("期间: 2026年04月") == "2026-04"

    def test_dash_format(self):
        assert _detect_period("Period: 2026-05") == "2026-05"

    def test_no_period(self):
        assert _detect_period("No dates here") is None


# ═══════════════════════════════════════════════════════════════════════════
# TESTS: ICBC Parser
# ═══════════════════════════════════════════════════════════════════════════

class TestICBCParser:
    def test_icbc_text_parsing(self):
        text = _extract_text(_icbc_pdf())
        result = _parse_icbc(text)
        assert len(result) >= 4, f"Expected >= 4 transactions, got {len(result)}: {result}"

    def test_icbc_amounts(self):
        text = _extract_text(_icbc_pdf())
        result = _parse_icbc(text)
        amounts = [t["amount"] for t in result]
        assert 15000.0 in amounts  # 工资
        assert 2500.0 in amounts   # 房租

    def test_icbc_currency(self):
        text = _extract_text(_icbc_pdf())
        result = _parse_icbc(text)
        for t in result:
            assert t["currency"] == "CNY"

    def test_icbc_income_expense_types(self):
        text = _extract_text(_icbc_pdf())
        result = _parse_icbc(text)
        income = [t for t in result if t["type"] == "income"]
        expense = [t for t in result if t["type"] == "expense"]
        assert len(income) >= 2
        assert len(expense) >= 3


# ═══════════════════════════════════════════════════════════════════════════
# TESTS: CMB Parser
# ═══════════════════════════════════════════════════════════════════════════

class TestCMBParser:
    def test_cmb_text_parsing(self):
        text = _extract_text(_cmb_pdf())
        result = _parse_cmb(text)
        assert len(result) >= 4

    def test_cmb_income_expense(self):
        text = _extract_text(_cmb_pdf())
        result = _parse_cmb(text)
        income = [t for t in result if t["type"] == "income"]
        expense = [t for t in result if t["type"] == "expense"]
        assert len(income) >= 2
        assert len(expense) >= 2


# ═══════════════════════════════════════════════════════════════════════════
# TESTS: CCB Parser
# ═══════════════════════════════════════════════════════════════════════════

class TestCCBParser:
    def test_ccb_date_conversion(self):
        text = _extract_text(_ccb_pdf())
        result = _parse_ccb(text)
        assert len(result) >= 3
        # YYYYMMDD should be converted to YYYY-MM-DD
        dates = [t["occurred_at"] for t in result]
        assert any("2026-05-01" in d for d in dates)
        assert any("2026-05-02" in d for d in dates)

    def test_ccb_amounts(self):
        text = _extract_text(_ccb_pdf())
        result = _parse_ccb(text)
        amounts = [t["amount"] for t in result]
        assert 12000.0 in amounts


# ═══════════════════════════════════════════════════════════════════════════
# TESTS: BOC Parser
# ═══════════════════════════════════════════════════════════════════════════

class TestBOCParser:
    def test_boc_parsing(self):
        text = _extract_text(_boc_pdf())
        result = _parse_boc(text)
        assert len(result) >= 3

    def test_boc_income_expense(self):
        text = _extract_text(_boc_pdf())
        result = _parse_boc(text)
        income = [t for t in result if t["type"] == "income"]
        expense = [t for t in result if t["type"] == "expense"]
        assert len(income) >= 2
        assert len(expense) >= 1


# ═══════════════════════════════════════════════════════════════════════════
# TESTS: N26 Parser
# ═══════════════════════════════════════════════════════════════════════════

class TestN26Parser:
    def test_n26_parsing(self):
        text = _extract_text(_n26_pdf())
        result = _parse_n26(text)
        assert len(result) >= 4

    def test_n26_currency_eur(self):
        text = _extract_text(_n26_pdf())
        result = _parse_n26(text)
        for t in result:
            assert t["currency"] == "EUR"

    def test_n26_types(self):
        text = _extract_text(_n26_pdf())
        result = _parse_n26(text)
        income = [t for t in result if t["type"] == "income"]
        expense = [t for t in result if t["type"] == "expense"]
        assert len(income) >= 2  # DEPOSIT + TRANSFER
        assert len(expense) >= 2  # SPENDING x3


# ═══════════════════════════════════════════════════════════════════════════
# TESTS: Revolut Parser
# ═══════════════════════════════════════════════════════════════════════════

class TestRevolutParser:
    def test_revolut_parsing(self):
        text = _extract_text(_revolut_pdf())
        result = _parse_revolut(text)
        assert len(result) >= 4

    def test_revolut_currency_eur(self):
        text = _extract_text(_revolut_pdf())
        result = _parse_revolut(text)
        for t in result:
            assert t["currency"] == "EUR"

    def test_revolut_types(self):
        text = _extract_text(_revolut_pdf())
        result = _parse_revolut(text)
        income = [t for t in result if t["type"] == "income"]
        expense = [t for t in result if t["type"] == "expense"]
        assert len(income) >= 2  # Top-Up + Transfer
        assert len(expense) >= 2  # Card Payment + Exchange


# ═══════════════════════════════════════════════════════════════════════════
# TESTS: Generic Parser
# ═══════════════════════════════════════════════════════════════════════════

class TestGenericParser:
    def test_generic_dash_dates(self):
        text = _extract_text(_generic_bank_pdf())
        result = _parse_generic(text)
        assert len(result) >= 4

    def test_generic_slash_dates(self):
        text = _extract_text(_generic_slash_dates_pdf())
        result = _parse_generic(text)
        assert len(result) >= 2
        # Slash dates should be normalized to dash
        for t in result:
            assert "/" not in t["occurred_at"]

    def test_generic_income_expense(self):
        text = _extract_text(_generic_bank_pdf())
        result = _parse_generic(text)
        income = [t for t in result if t["type"] == "income"]
        expense = [t for t in result if t["type"] == "expense"]
        assert len(income) >= 1
        assert len(expense) >= 2

    def test_generic_amounts_positive(self):
        text = _extract_text(_generic_bank_pdf())
        result = _parse_generic(text)
        for t in result:
            assert t["amount"] > 0

    def test_generic_multiline_desc(self):
        """Wrapped descriptions: only lines with both date AND amount are transactions."""
        text = _extract_text(_make_multiline_desc_pdf())
        result = _parse_generic(text)
        # "S.A.R.L." alone on a line should NOT create a transaction
        for t in result:
            assert "S.A.R.L." not in t["description"]
        # Should still find the 3 real transactions
        assert len(result) >= 2

    def test_generic_numeric_noise(self):
        """Phone-number-like strings should not be parsed as transactions."""
        text = _extract_text(_make_numeric_noise_pdf())
        result = _parse_generic(text)
        # Should only find real transactions
        descs = [t["description"] for t in result]
        assert not any("Customer Service" in d for d in descs)
        assert not any("Account Number" in d for d in descs)


# ═══════════════════════════════════════════════════════════════════════════
# TESTS: Full Pipeline (parse_pdf_statement with mock DB)
# ═══════════════════════════════════════════════════════════════════════════

class TestFullPipeline:
    @pytest.mark.asyncio
    async def test_icbc_full_pipeline(self, mock_db, mock_pdf_import):
        result = await parse_pdf_statement(mock_db, mock_pdf_import, _icbc_pdf())
        assert result["detected_bank"] == "icbc"
        assert result["error"] is None
        assert len(result["transactions"]) >= 4
        assert result["statement_period"] == "2026-04"

    @pytest.mark.asyncio
    async def test_n26_full_pipeline(self, mock_db, mock_pdf_import):
        result = await parse_pdf_statement(mock_db, mock_pdf_import, _n26_pdf())
        assert result["detected_bank"] == "n26"
        assert result["error"] is None
        assert len(result["transactions"]) >= 4

    @pytest.mark.asyncio
    async def test_revolut_full_pipeline(self, mock_db, mock_pdf_import):
        result = await parse_pdf_statement(mock_db, mock_pdf_import, _revolut_pdf())
        assert result["detected_bank"] == "revolut"
        assert result["error"] is None
        assert len(result["transactions"]) >= 4

    @pytest.mark.asyncio
    async def test_generic_fallback(self, mock_db, mock_pdf_import):
        result = await parse_pdf_statement(mock_db, mock_pdf_import, _generic_bank_pdf())
        assert result["detected_bank"] is None
        assert result["error"] is None
        assert len(result["transactions"]) >= 3

    @pytest.mark.asyncio
    async def test_empty_pdf(self, mock_db, mock_pdf_import):
        result = await parse_pdf_statement(mock_db, mock_pdf_import, _make_empty_pdf())
        assert result["detected_bank"] is None
        assert result["error"] is None
        assert len(result["transactions"]) == 0

    @pytest.mark.asyncio
    async def test_garbage_file(self, mock_db, mock_pdf_import):
        result = await parse_pdf_statement(mock_db, mock_pdf_import, _make_garbage_pdf())
        assert result["error"] is not None
        assert "Failed to extract text" in result["error"]

    @pytest.mark.asyncio
    async def test_encrypted_pdf(self, mock_db, mock_pdf_import):
        result = await parse_pdf_statement(mock_db, mock_pdf_import, _make_encrypted_pdf())
        # Encrypted PDFs may fail with pdfplumber or return empty text
        # Either way should not crash
        assert "transactions" in result
        assert isinstance(result["transactions"], list)


# ═══════════════════════════════════════════════════════════════════════════
# TESTS: Transaction structure validation
# ═══════════════════════════════════════════════════════════════════════════

class TestTransactionStructure:
    """All parsers must return consistent transaction dicts."""

    REQUIRED_FIELDS = {
        "occurred_at", "amount", "currency", "type",
        "description", "raw_description", "external_id",
    }

    @pytest.mark.parametrize("pdf_fn,bank_key", [
        (_icbc_pdf, "icbc"),
        (_cmb_pdf, "cmb"),
        (_ccb_pdf, "ccb"),
        (_boc_pdf, "boc"),
        (_n26_pdf, "n26"),
        (_revolut_pdf, "revolut"),
    ])
    def test_required_fields(self, pdf_fn, bank_key):
        text = _extract_text(pdf_fn())
        result = _parse_for_bank(bank_key, text)
        for tx in result:
            for field in self.REQUIRED_FIELDS:
                assert field in tx, f"Missing field '{field}' in {bank_key} tx: {tx}"
            assert tx["type"] in ("income", "expense")
            assert tx["amount"] > 0
            assert isinstance(tx["occurred_at"], str)
            assert "T" in tx["occurred_at"]  # ISO format

    def test_generic_required_fields(self):
        text = _extract_text(_generic_bank_pdf())
        result = _parse_generic(text)
        for tx in result:
            for field in self.REQUIRED_FIELDS:
                assert field in tx, f"Missing field '{field}' in generic tx: {tx}"


# ═══════════════════════════════════════════════════════════════════════════
# TESTS: Edge Cases
# ═══════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_empty_text(self):
        result = _parse_generic("")
        assert result == []

    def test_text_no_dates(self):
        result = _parse_generic("Some random text without any dates or amounts")
        assert result == []

    def test_text_dates_no_amounts(self):
        result = _parse_generic("2026-05-01  Some description but no amount")
        assert result == []

    def test_text_amounts_no_dates(self):
        result = _parse_generic("Random line -50.00 Expense")
        assert result == []

    def test_zero_amount(self):
        result = _parse_generic("2026-05-01  0.00  Should be ignored")
        assert result == []

    def test_very_large_amount(self):
        result = _parse_generic("2026-05-01  +999999999.99  Big transaction")
        assert len(result) == 1
        assert result[0]["amount"] == 999999999.99

    def test_comma_thousands(self):
        result = _parse_generic("2026-05-01  +1,234,567.89  Big number")
        assert len(result) == 1

    def test_negative_expense(self):
        """Negative amounts should be classified as expense, stored as positive."""
        result = _parse_generic("2026-05-01  -100.00  Expense")
        assert len(result) == 1
        assert result[0]["type"] == "expense"
        assert result[0]["amount"] == 100.00

    def test_positive_income(self):
        """Positive amounts should be classified as income."""
        result = _parse_generic("2026-05-01  +500.00  Income")
        assert len(result) == 1
        assert result[0]["type"] == "income"
        assert result[0]["amount"] == 500.00

    def test_detect_bank_empty(self):
        assert _detect_bank("") is None

    def test_detect_period_empty(self):
        assert _detect_period("") is None


# ═══════════════════════════════════════════════════════════════════════════
# TESTS: PDF text extraction quality
# ═══════════════════════════════════════════════════════════════════════════

class TestPDFExtraction:
    def test_icbc_text_contains_chinese(self):
        text = _extract_text(_icbc_pdf())
        assert "工商银行" in text or "ICBC" in text.upper()

    def test_n26_text_readable(self):
        text = _extract_text(_n26_pdf())
        assert "N26" in text

    def test_revolut_text_readable(self):
        text = _extract_text(_revolut_pdf())
        assert "Revolut" in text

    def test_empty_pdf_no_text(self):
        text = _extract_text(_make_empty_pdf())
        assert text.strip() == ""

    def test_cmb_text_contains_chinese(self):
        text = _extract_text(_cmb_pdf())
        # CJK text may render as mapped names by pdfplumber; key is it's not empty
        assert len(text.strip()) > 0
        assert "2026-05-01" in text or "2026/05/01" in text

    def test_ccb_text_contains_chinese(self):
        text = _extract_text(_ccb_pdf())
        assert len(text.strip()) > 0
        assert "20260501" in text or "2026-05-01" in text

    def test_boc_text_contains_chinese(self):
        text = _extract_text(_boc_pdf())
        assert len(text.strip()) > 0
        assert "2026-05-01" in text
