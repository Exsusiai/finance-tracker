"""PayPal activity-export CSV parser.

PayPal's web export (Activity → Reports → Statements / Activity download → CSV)
is a clean, structured file — far more reliable to parse than the PDF. Columns
(English export; the account language decides header language):

    Date, Time, Time Zone, Description, Currency, Gross, Fee, Net, Balance,
    Transaction ID, From Email Address, Name, Bank Name, Bank Account,
    Shipping and Handling Amount, Sales Tax, Invoice ID, Reference Txn ID

Design decisions (see memory ``paypal-integration-direction`` + the V8/PayPal
investigation):

- **Balance-faithful**: every EUR balance-affecting row is emitted, so the
  PayPal account balance reconciles to PayPal's own ``Balance`` column.
- **Row classes → ledger meaning**:
  - ``Bank Deposit to PP Account`` / ``General Card Deposit`` (money pulled
    from your bank/card to fund a payment) → ``transfer`` IN.
  - ``User Initiated Withdrawal`` (money sent out to your bank) → ``transfer``
    OUT. (ACH reversals follow their sign.)
  - everything else (Mobile Payment / Express Checkout / PreApproved Bill /
    Payment Refund / EUR currency-conversion leg) → ``income`` if net > 0 else
    ``expense`` (the real economic event).
- **Foreign-currency purchases**: a USD GitHub charge appears as a net-zero
  USD ``Payment`` + ``Currency Conversion`` pair PLUS an EUR
  ``General Currency Conversion`` debit (the real cost). We **skip the
  net-zero non-EUR rows** and keep the EUR conversion as the expense, enriched
  with the merchant name from the USD payment row (linked via Reference Txn ID).
- **Dedup**: PayPal's ``Transaction ID`` is a stable per-row key → used as
  ``external_id``. The import layer skips rows whose external_id already exists
  on the account, so re-uploading overlapping date ranges is safe.
- **No date-range assumption**: the parser just reads whatever rows are in the
  file (any range, multiple months).

Amounts use ``Net`` (the balance impact). Stored ABS — direction lives in
``type`` + ``metadata.transfer_direction`` (project convention).
"""

from __future__ import annotations

import csv
import io
import json
from decimal import Decimal, InvalidOperation

PARSER_VERSION = "paypal-csv-1"

# Expected header columns that identify a PayPal activity export.
_SIGNATURE_COLUMNS = {"Transaction ID", "Gross", "Net", "Balance", "Description"}

# Descriptions where money moves between PayPal and the user's own bank/card.
# These become transfers (they pair with the bank-side statement leg).
_DEPOSIT_DESCRIPTIONS = {
    "Bank Deposit to PP Account",
    "General Card Deposit",
    "Reversal of ACH Withdrawal Transaction",
}
_WITHDRAWAL_DESCRIPTIONS = {
    "User Initiated Withdrawal",
    "Reversal of ACH Deposit",
}


def is_paypal_csv(raw: bytes) -> bool:
    """True if ``raw`` looks like a PayPal activity-export CSV (by header)."""
    header = _read_header(raw)
    if not header:
        return False
    cols = {c.strip() for c in header}
    return _SIGNATURE_COLUMNS.issubset(cols)


def _read_header(raw: bytes) -> list[str] | None:
    try:
        text = raw.decode("utf-8-sig")  # strips BOM
    except UnicodeDecodeError:
        try:
            text = raw.decode("latin-1")
        except UnicodeDecodeError:
            return None
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        return row
    return None


def _parse_amount(value: str) -> Decimal | None:
    """Parse a PayPal money string. Handles German grouping (1.234,56) and
    plain (1234.56). Returns None when blank/unparseable."""
    v = (value or "").strip()
    if not v:
        return None
    # If both separators present, the LAST one is the decimal separator.
    if "," in v and "." in v:
        if v.rfind(",") > v.rfind("."):
            v = v.replace(".", "").replace(",", ".")  # German: 1.234,56
        else:
            v = v.replace(",", "")  # English grouping: 1,234.56
    elif "," in v:
        v = v.replace(",", ".")  # comma decimal: 25,80
    try:
        return Decimal(v)
    except InvalidOperation:
        return None


def _parse_occurred_at(date_str: str, time_str: str) -> str | None:
    """``DD.MM.YYYY`` + ``HH:MM:SS`` → ``YYYY-MM-DDTHH:MM:SSZ``.

    We store PayPal's wall-clock time as-is (no timezone math) to keep the
    calendar date stable — matching how bank-statement dates are handled and
    avoiding DST edge cases shifting a transaction across a month boundary.
    """
    d = (date_str or "").strip()
    parts = d.split(".")
    if len(parts) != 3:
        return None
    day, month, year = (p.strip() for p in parts)
    if not (len(year) == 4 and day and month):
        return None
    t = (time_str or "").strip() or "00:00:00"
    if t.count(":") == 1:
        t += ":00"
    return f"{year}-{int(month):02d}-{int(day):02d}T{t}Z"


def parse_paypal_csv(raw: bytes) -> dict:
    """Parse a PayPal activity-export CSV into the common parser-result shape."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    rows = [{(k or "").strip(): (v or "") for k, v in r.items()} for r in reader]

    if not rows:
        return {"detected_source": "paypal", "transactions": [], "error": "Empty CSV"}

    # First pass: index NON-EUR payment rows by Transaction ID so we can enrich
    # the EUR currency-conversion leg (which carries no merchant name) with the
    # foreign merchant it paid (GitHub etc.). Linked via Reference Txn ID.
    foreign_by_txn: dict[str, dict[str, str]] = {}
    for r in rows:
        if (r.get("Currency") or "").strip().upper() != "EUR":
            txn_id = (r.get("Transaction ID") or "").strip()
            if txn_id:
                foreign_by_txn[txn_id] = {
                    "name": (r.get("Name") or "").strip(),
                    "description": (r.get("Description") or "").strip(),
                }

    out: list[dict] = []
    periods: set[str] = set()
    # PayPal occasionally REUSES a Transaction ID across two rows (e.g. an ACH
    # withdrawal and its reversal share one id). external_id must stay unique
    # per row, so we append a deterministic ``#N`` suffix to later occurrences
    # — same file always yields the same ids, so cross-upload dedup still works.
    seen_txn_counts: dict[str, int] = {}
    # Track (occurred_at, net, balance) of the earliest EUR row to derive the
    # statement's opening balance (= balance_before_first = Balance − Net).
    earliest: tuple[str, object, object] | None = None
    for r in rows:
        currency = (r.get("Currency") or "").strip().upper()
        # Skip non-EUR rows: they are the net-zero FX clearing pair for a
        # foreign-currency purchase. The real cost is the EUR conversion leg,
        # which we keep below.
        if currency != "EUR":
            continue

        net = _parse_amount(r.get("Net") or r.get("Gross") or "")
        if net is None:
            continue

        occurred_at = _parse_occurred_at(r.get("Date", ""), r.get("Time", ""))
        if occurred_at is None:
            continue
        periods.add(occurred_at[:7])

        balance = _parse_amount(r.get("Balance") or "")
        if balance is not None and (earliest is None or occurred_at < earliest[0]):
            earliest = (occurred_at, net, balance)

        description = (r.get("Description") or "").strip()
        name = (r.get("Name") or "").strip()
        ref_txn = (r.get("Reference Txn ID") or "").strip()
        txn_id = (r.get("Transaction ID") or "").strip()
        if txn_id:
            n = seen_txn_counts.get(txn_id, 0) + 1
            seen_txn_counts[txn_id] = n
            external_id = txn_id if n == 1 else f"{txn_id}#{n}"
        else:
            external_id = None

        # Classify on the ORIGINAL description (before any enrichment that
        # appends text — otherwise an enriched "Bank Deposit to PP Account
        # (...)" stops matching the deposit set and is mis-typed as income).
        if description in _DEPOSIT_DESCRIPTIONS:
            tx_type, direction = "transfer", "in"
        elif description in _WITHDRAWAL_DESCRIPTIONS:
            tx_type, direction = "transfer", "out"
        else:
            tx_type, direction = ("income" if net > 0 else "expense"), None

        # Enrich the EUR currency-conversion leg (no Name) with the foreign
        # merchant it settled, so the EXPENSE reads as "GitHub" not a bare
        # conversion. Transfers keep their generic bank counterparty.
        if tx_type != "transfer" and not name and ref_txn and ref_txn in foreign_by_txn:
            fm = foreign_by_txn[ref_txn]
            name = fm["name"]
            if fm["description"]:
                description = f"{description} ({fm['description']})"

        meta: dict[str, object] = {
            "paypal_kind": description,
            "source_format": "paypal_csv",
        }
        if ref_txn:
            meta["reference_txn_id"] = ref_txn
        if direction is not None:
            meta["transfer_direction"] = direction

        counterparty = name or (r.get("From Email Address") or "").strip() or None
        disp = description if not name else f"{description} · {name}"

        out.append({
            "occurred_at": occurred_at,
            "amount": str(abs(net)),
            "currency": "EUR",
            "type": tx_type,
            "description": disp,
            "raw_description": description,
            "counterparty": counterparty,
            "external_id": external_id,
            "metadata_json": json.dumps(meta, ensure_ascii=False),
        })

    statement_period = None
    if periods:
        lo, hi = min(periods), max(periods)
        statement_period = lo if lo == hi else f"{lo}~{hi}"

    opening_balance = None
    if earliest is not None:
        # balance_before_first_row = Balance − Net
        opening_balance = str(earliest[2] - earliest[1])

    return {
        "detected_source": "paypal",
        "parser_version": PARSER_VERSION,
        "statement_period": statement_period,
        "opening_balance": opening_balance,
        "transactions": out,
        "error": None,
    }
