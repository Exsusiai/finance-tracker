"""SQLAlchemy ORM model for bank_connections table.

This model tracks bank connections (OAuth/PSD2 authorizations) and their sync state.
Credentials are encrypted at rest using AES-256-GCM (see services/bank_sync/crypto.py).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _utcnow_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class BankConnection(Base):
    __tablename__ = "bank_connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # ─── Provider info ──────────────────────────────────────────────
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    institution_id: Mapped[str] = mapped_column(String(100), nullable=False)
    institution_name: Mapped[str] = mapped_column(String(255), nullable=False)
    institution_bic: Mapped[str | None] = mapped_column(String(50))
    institution_country: Mapped[str] = mapped_column(String(10), nullable=False)
    institution_logo: Mapped[str | None] = mapped_column(String(500))

    # ─── GoCardless specific ───────────────────────────────────────
    gc_requisition_id: Mapped[str | None] = mapped_column(String(100))
    gc_agreement_id: Mapped[str | None] = mapped_column(String(100))
    gc_account_ids_json: Mapped[str | None] = mapped_column(Text)
    # JSON array: ["acc1", "acc2"] — provider-side account IDs

    # ─── Local mapping ─────────────────────────────────────────────
    account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="SET NULL")
    )
    currency: Mapped[str] = mapped_column(String(10), nullable=False)

    # ─── Sync state ────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )
    last_sync_at: Mapped[str | None] = mapped_column(String(30))
    last_sync_status: Mapped[str | None] = mapped_column(String(20))
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    next_sync_at: Mapped[str | None] = mapped_column(String(30))
    sync_interval_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    total_transactions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ─── Encrypted credentials ─────────────────────────────────────
    encrypted_creds: Mapped[str | None] = mapped_column(Text)

    # ─── Metadata ──────────────────────────────────────────────────
    metadata_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)
    updated_at: Mapped[str] = mapped_column(String(30), nullable=False, default=_utcnow_str)
    deleted_at: Mapped[str | None] = mapped_column(String(30))

    __table_args__ = (
        CheckConstraint(
            "provider IN ('gocardless')",
            name="ck_bank_conn_provider",
        ),
        CheckConstraint(
            "status IN ('pending','connecting','active','expired','error','revoked')",
            name="ck_bank_conn_status",
        ),
        Index("idx_bank_conn_status", "status"),
        Index("idx_bank_conn_provider", "provider"),
    )
