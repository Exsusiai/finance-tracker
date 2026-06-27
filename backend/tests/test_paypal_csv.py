"""Tests for the PayPal CSV parser (services/csv_parser/paypal.py)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.services.csv_parser import detect_and_parse_csv
from app.services.csv_parser.paypal import (
    _parse_amount,
    is_paypal_csv,
    parse_paypal_csv,
)

FIXTURE = Path(__file__).parent / "fixtures" / "csv_parser" / "paypal_sample.csv"


@pytest.fixture()
def raw() -> bytes:
    return FIXTURE.read_bytes()


def _by_ext(rows: list[dict], ext_id: str) -> dict:
    return next(r for r in rows if r["external_id"] == ext_id)


def test_amount_parser_handles_locales():
    assert _parse_amount("25,80") == Decimal("25.80")
    assert _parse_amount("-620,00") == Decimal("-620.00")
    assert _parse_amount("1.234,56") == Decimal("1234.56")  # German grouping
    assert _parse_amount("1,234.56") == Decimal("1234.56")  # English grouping
    assert _parse_amount("") is None


def test_detection(raw):
    assert is_paypal_csv(raw) is True
    assert is_paypal_csv(b"not,a,paypal,file\n1,2,3,4") is False


def test_skips_non_eur_rows(raw):
    rows = parse_paypal_csv(raw)["transactions"]
    ids = {r["external_id"] for r in rows}
    # The two USD rows (net-zero FX clearing) must not be imported.
    assert "TXNUSDPAY01" not in ids
    assert "TXNUSDCONV01" not in ids
    # 8 EUR rows in the fixture.
    assert len(rows) == 8


def test_withdrawal_is_transfer_out(raw):
    rows = parse_paypal_csv(raw)["transactions"]
    w = _by_ext(rows, "TXNWITHDRAW01")
    assert w["type"] == "transfer"
    assert w["amount"] == "620.00"
    assert '"transfer_direction": "out"' in w["metadata_json"]


def test_deposits_are_transfer_in(raw):
    rows = parse_paypal_csv(raw)["transactions"]
    for ext in ("TXNCARDDEP01", "TXNBANKDEP01"):
        d = _by_ext(rows, ext)
        assert d["type"] == "transfer"
        assert '"transfer_direction": "in"' in d["metadata_json"]


def test_p2p_income_and_expense(raw):
    rows = parse_paypal_csv(raw)["transactions"]
    assert _by_ext(rows, "TXNP2PIN01")["type"] == "income"
    assert _by_ext(rows, "TXNP2POUT01")["type"] == "expense"
    assert _by_ext(rows, "TXNP2POUT01")["amount"] == "125.00"


def test_eur_conversion_enriched_with_foreign_merchant(raw):
    """The EUR currency-conversion leg carries no Name; it must be enriched
    with the USD merchant (GitHub) it settled, and be an expense."""
    rows = parse_paypal_csv(raw)["transactions"]
    conv = _by_ext(rows, "TXNEURCONV01")
    assert conv["type"] == "expense"
    assert conv["amount"] == "8.79"
    assert "GitHub" in (conv["counterparty"] or "")
    assert "github" in conv["metadata_json"].lower() or "GitHub" in conv["description"]


def test_external_id_present_for_dedup(raw):
    rows = parse_paypal_csv(raw)["transactions"]
    assert all(r["external_id"] for r in rows)
    # External ids are unique within a file.
    ids = [r["external_id"] for r in rows]
    assert len(ids) == len(set(ids))


def test_occurred_at_keeps_calendar_date(raw):
    rows = parse_paypal_csv(raw)["transactions"]
    # 29.01 00:02:46 must stay on the 29th (no TZ shift to the 28th).
    assert _by_ext(rows, "TXNP2POUT02")["occurred_at"].startswith("2026-01-29T")


def test_balance_reconciles(raw):
    """Sum of signed EUR nets must equal the fixture's final running balance
    (0 opening → -718,28) — proves we keep exactly the balance-affecting EUR
    rows with the right signs and skip the net-zero USD pair."""
    rows = parse_paypal_csv(raw)["transactions"]
    total = Decimal("0")
    for r in rows:
        amt = Decimal(r["amount"])
        if r["type"] == "expense":
            total -= amt
        elif r["type"] == "income":
            total += amt
        else:  # transfer
            md = r["metadata_json"]
            total += amt if '"transfer_direction": "in"' in md else -amt
    assert total == Decimal("-718.28")


def test_detect_and_parse_dispatch(raw):
    res = detect_and_parse_csv(raw)
    assert res["detected_source"] == "paypal"
    assert res["error"] is None
    assert len(res["transactions"]) == 8


_HEADER = (
    '"Date","Time","Time Zone","Description","Currency","Gross","Fee","Net",'
    '"Balance","Transaction ID","From Email Address","Name","Bank Name",'
    '"Bank Account","Shipping and Handling Amount","Sales Tax","Invoice ID",'
    '"Reference Txn ID"'
)


def test_opening_balance_is_balance_minus_net_of_earliest_row():
    """Opening balance = (earliest row's Balance − its Net)."""
    csv = (
        _HEADER + "\n"
        '"01.02.2026","10:00:00","Europe/Berlin","Mobile Payment","EUR","50,00","0,00","50,00","150,00","T2","","P","","","0,00","0,00","",""' + "\n"
        '"01.01.2026","10:00:00","Europe/Berlin","Mobile Payment","EUR","-20,00","0,00","-20,00","100,00","T1","","P","","","0,00","0,00","",""' + "\n"
    ).encode()
    res = parse_paypal_csv(csv)
    # earliest = Jan 1 (balance 100, net -20) → opening = 100 − (−20) = 120.
    assert Decimal(res["opening_balance"]) == Decimal("120.00")


def test_duplicate_transaction_id_gets_suffixed():
    """PayPal can reuse a Transaction ID (ACH withdrawal + its reversal). Each
    row must still get a unique external_id (deterministic #N suffix)."""
    csv = (
        _HEADER + "\n"
        '"18.03.2026","23:54:33","Europe/Berlin","User Initiated Withdrawal","EUR","-10,69","0,00","-10,69","0,00","DUP","","","","","0,00","0,00","",""' + "\n"
        '"18.03.2026","23:54:58","Europe/Berlin","Reversal of ACH Withdrawal Transaction","EUR","10,69","0,00","10,69","10,69","DUP","","","","","0,00","0,00","",""' + "\n"
    ).encode()
    rows = parse_paypal_csv(csv)["transactions"]
    ext = [r["external_id"] for r in rows]
    assert ext == ["DUP", "DUP#2"]
    assert len(set(ext)) == len(ext)
