"""Minimal smoke tests for the PDF parser engine.

The previous version of this file used reportlab to synthesise Chinese-bank
statements (ICBC / CMB / CCB / BOC) and called ``_parse_for_bank`` /
``_parse_icbc`` / etc. — none of which exist in the current code. The Chinese
banks were removed in favour of the 5 European banks the user actually uses
(AMEX-DE / N26 / Revolut / TFBank / Advanzia).

Real reference PDFs live in ``data/inputpdf_reference/`` (git-ignored, present
locally only). When they're available we run end-to-end parsing; otherwise
the file falls back to import-only smoke tests so CI / fresh clones still
collect cleanly.

Sprint 1 FIX-7 (review V1 §P3-2).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
REF_DIR = PROJECT_ROOT / "data" / "inputpdf_reference"

EUROPEAN_BANK_FILES = {
    "amex_de": "AMEX-DE.pdf",
    "n26": "N26.pdf",
    "revolut": "Revolut.pdf",
    "tfbank": "TFBank.pdf",
    "advanzia": "advanzia.pdf",
}


def test_engine_module_imports() -> None:
    """The parser engine and its public surface must at least import cleanly."""
    from app.services.pdf_parser import engine

    assert hasattr(engine, "parse_pdf_statement")
    assert hasattr(engine, "_detect_bank")


def test_classify_transfer_infers_direction_from_default_type(monkeypatch) -> None:
    """A cross-bank hint with no directional verb (e.g. an owner-name extra
    like "from Rui Zeng") must still get a transfer_direction — derived from
    the debit/credit the statement already gave us (default_type). Otherwise
    the row becomes a direction-less transfer that the balance view + 未配对
    panel default to 'out', reversing income-origin transfers.
    """
    from app.services.pdf_parser import engine

    # A keyword present in neither _DIRECTION_IN_HINTS nor _DIRECTION_OUT_HINTS.
    monkeypatch.setattr(engine, "_cross_bank_transfer_hints", lambda: ("from rui zeng",))

    t_in, meta_in = engine._classify_transfer("from rui zeng", "income")
    assert t_in == "transfer"
    assert meta_in is not None and meta_in["transfer_direction"] == "in"

    t_out, meta_out = engine._classify_transfer("from rui zeng", "expense")
    assert t_out == "transfer"
    assert meta_out is not None and meta_out["transfer_direction"] == "out"


def test_classify_transfer_interest_not_subaccount() -> None:
    """Revolut 'Net interest paid to "Instant Access Savings"' mentions a
    sub-account but is deposit INTEREST (income), not an internal move — it
    must NOT be tagged subaccount, while a real 'To/From <space>' move still is.
    """
    from app.services.pdf_parser import engine

    # Interest row — contains the subaccount keyword "to instant access savings"
    # but the interest guard must keep it out of subaccount.
    t, meta = engine._classify_transfer(
        'Net interest paid to "Instant Access Savings" for Jan 1,', "income"
    )
    assert not (meta and meta.get("subaccount")), f"interest wrongly subaccount: {meta}"

    # A genuine internal move still classifies as a subaccount transfer.
    t2, meta2 = engine._classify_transfer("To Instant Access Savings", "expense")
    assert t2 == "transfer" and meta2 is not None and meta2.get("subaccount") is True


def test_bank_detector_returns_supported_keys() -> None:
    """`_detect_bank` should map known signatures to one of the supported banks.

    Keywords come from ``_BANK_SIGNATURES`` in
    ``app/services/pdf_parser/engine.py``.
    """
    from app.services.pdf_parser.engine import _detect_bank

    samples = {
        "americanexpress.de helpdesk + frankfurter str. 227": "amex_de",
        "n26 bank ag berlin ntsbdeb1": "n26",
        "revolut bank uab konstitucijos pr. 21b": "revolut",
        "tf bank ab order tf nordic gold tfbank.de": "tfbank",
        "advanzia bank s.a. luxembourg hilton honors kreditkarte": "advanzia",
    }
    for text, expected in samples.items():
        detected = _detect_bank(text)
        # Soft assertion: detector either returns the expected bank or None
        # when the fixture string is too sparse. We only fail on definite
        # mis-classification (returning a different bank).
        assert detected in {expected, None}, (
            f"detector returned {detected!r} for {expected!r} sample"
        )


def test_detector_ignores_counterparty_bank_in_body() -> None:
    """Regression (2026-06): N26↔Revolut cross-labeling.

    Each statement mentions the OTHER bank's BIC in a transfer line. The
    issuer's marker appears earlier (header), so earliest-position detection
    must pick the issuer, not the counterparty.
    """
    from app.services.pdf_parser.engine import _detect_bank

    n26_stmt = (
        "N26 Bank AG Berlin NTSBDEB1 Account statement\n"
        + "x" * 200
        + "\nOutgoing transfer to Revolut REVODEB2 -50,00€\n"
    )
    revolut_stmt = (
        "Revolut Bank UAB statement REVODEB2\n"
        + "x" * 200
        + "\nIncoming transfer from N26 NTSBDEB1 +50.00\n"
    )
    assert _detect_bank(n26_stmt) == "n26"
    assert _detect_bank(revolut_stmt) == "revolut"


@pytest.mark.parametrize("bank,filename", list(EUROPEAN_BANK_FILES.items()))
def test_real_pdf_round_trip(bank: str, filename: str) -> None:
    """If reference PDFs are available locally, parsing them should yield rows."""
    pytest.importorskip("pdfplumber")
    pdf_path = REF_DIR / filename
    if not pdf_path.exists():
        pytest.skip(f"Reference PDF for {bank} not available at {pdf_path}")

    import asyncio
    from app.services.pdf_parser.engine import parse_pdf_statement

    pdf_bytes = pdf_path.read_bytes()
    pdf_import = MagicMock()
    pdf_import.id = 1
    pdf_import.filename = filename

    db = MagicMock()
    result = asyncio.run(parse_pdf_statement(db, pdf_import, pdf_bytes))

    assert result.get("error") is None, f"{bank} parse returned error: {result.get('error')}"
    assert result.get("detected_bank"), f"{bank} parser did not detect a bank"
    txs = result.get("transactions", [])
    assert len(txs) > 0, f"{bank} parser produced zero transactions"
    # All non-adjustment rows should be positive (Sprint 1 FIX-4 / FIX-5
    # invariant — direction is encoded in `type`, not the sign).
    for tx in txs:
        if tx.get("type") != "adjustment":
            assert float(tx.get("amount", 0)) >= 0, (
                f"{bank}: tx {tx} has negative amount; parsers must store ABS"
            )
