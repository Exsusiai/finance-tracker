"""CSV statement parsers.

Sibling of ``pdf_parser`` for account exports that come as CSV rather than
PDF. Like the PDF side, there is **no universal CSV format** — each provider
gets its own parser keyed off the header signature. First (and currently
only) provider: PayPal.

The public surface mirrors the PDF engine so the upload/staging/ingestion
pipeline can treat both uniformly: a parser returns
``{"detected_source", "transactions": [...], "error", ...}`` where each
transaction dict carries the same keys ``_insert_and_ingest`` already reads
(occurred_at / amount / currency / type / description / raw_description /
counterparty / external_id / metadata_json).
"""

from __future__ import annotations

from app.services.csv_parser.paypal import is_paypal_csv, parse_paypal_csv

__all__ = ["detect_and_parse_csv", "is_paypal_csv", "parse_paypal_csv"]


def detect_and_parse_csv(raw: bytes) -> dict:
    """Detect the CSV provider and dispatch to its parser.

    Returns the parser result dict, or an ``{"error": ...}`` dict when the
    CSV isn't a format we recognise.
    """
    if is_paypal_csv(raw):
        return parse_paypal_csv(raw)
    return {
        "detected_source": None,
        "transactions": [],
        "error": "Unrecognised CSV format (only PayPal activity export is supported)",
    }
