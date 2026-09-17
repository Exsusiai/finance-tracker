"""Single definition of "the user has settled this row" (V9-F16).

Automation (refresh-matching re-detection / re-categorisation, LLM write-back,
transfer candidacy) must never override an explicit human decision. Before
this module each entry point kept its own partial check (`source='manual'`,
`user_note IS NOT NULL`, …) and none of them knew about
`categorization_method='user'` — so a row the user confirmed in the inbox
could still be silently rewritten by the next global refresh or by an LLM
response that returned after the confirmation.

Keep the rule in ONE place. If a new "user touched this" signal appears,
add it here, not in an individual caller.
"""

from __future__ import annotations

from sqlalchemy import or_

from app.models import Transaction


def is_user_locked(tx: Transaction) -> bool:
    """True when the user explicitly settled this row.

    Signals (any one suffices):
      - created by hand (`source='manual'`) — the row IS the user's intent;
      - carries a user note — written by a human during confirm/edit;
      - categorization_method='user' — stamped by inbox confirm / manual
        category edits (2026-09).
    """
    return (
        (tx.source or "") == "manual"
        or bool(tx.user_note)
        or (tx.categorization_method or "") == "user"
    )


def not_user_locked_clause():
    """SQLAlchemy WHERE fragment mirroring :func:`is_user_locked` (negated),
    for queries that pre-filter candidate rows in SQL."""
    return or_(
        Transaction.categorization_method.is_(None),
        Transaction.categorization_method != "user",
    )
