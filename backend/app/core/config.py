"""Application configuration via pydantic-settings."""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent  # backend/../
_DATA_DIR = _PROJECT_ROOT / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Auth ---
    finance_tracker_api_token: str = Field(
        default="",
        description="Bearer token for API auth. MUST be set in production.",
    )
    auth_disabled: bool = Field(
        default=False,
        description="Disable Bearer token check entirely. Local dev only — never enable in production.",
    )

    # --- Base ---
    base_currency: str = "CNY"
    # Sprint 2 FIX-9 (review §P0-2): default to loopback so the API isn't
    # accidentally exposed to the LAN. Override via BACKEND_HOST env var when
    # you really mean to bind 0.0.0.0 (and keep AUTH_DISABLED=false there!).
    backend_host: str = "127.0.0.1"
    backend_port: int = 8000
    log_level: str = "INFO"

    # CORS allow-list. Comma-separated origin URLs. Default covers the local
    # dev frontends on common ports. Production deployments should override.
    allowed_origins: str = "http://localhost:3000,http://localhost:3010,http://127.0.0.1:3000,http://127.0.0.1:3010"

    # --- Personal hints (kept out of source code; loaded from .env) ---
    # Comma-separated list of the account-holder's name variants. Used by
    # transfer matcher / pdf parser / categorizer to recognise self-transfers
    # where the bank statement prints the owner's own name on both legs.
    # Example: "Jane Doe,Doe Jane,J. Doe"
    finance_tracker_owner_names: str = ""

    # --- Database ---
    database_url: str = f"sqlite:///{_DATA_DIR / 'finance.db'}"

    # --- Market data sources ---
    coingecko_api_key: str = ""
    goldapi_key: str = ""
    market_refresh_crypto_sec: int = 300
    market_refresh_stock_sec: int = 900
    market_refresh_fx_sec: int = 3600
    market_refresh_gold_sec: int = 3600

    # --- Notion sync ---
    notion_token: str = ""
    notion_transactions_db_id: str = ""
    notion_cashflow_db_id: str = ""
    notion_asset_page_id: str = ""
    notion_sync_enabled: bool = False

    # --- Bank sync (GoCardless / Nordigen) ---
    # Encrypted credentials are stored per-connection in DB.
    # This key encrypts/decrypts them. Generate once and keep safe!
    # python -c "import os; print(os.urandom(32).hex())"
    finance_bank_encryption_key: str = ""
    bank_sync_enabled: bool = False
    bank_sync_interval_hours: int = 24
    bank_sync_callback_base_url: str = "http://localhost:3000/settings/bank-sync/callback"
    # GoCardless API credentials (for initial setup; stored encrypted in DB per-connection)
    gocardless_secret_id: str = ""
    gocardless_secret_key: str = ""

    @field_validator("finance_tracker_api_token", mode="after")
    @classmethod
    def _ensure_token(cls, v: str) -> str:
        if not v or v == "replace-me-with-a-32-byte-random-hex-string":
            # Dev-only: auto-generate so the app can start locally.
            import warnings

            warnings.warn(
                "FINANCE_TRACKER_API_TOKEN not set — using a random token for this session. "
                "Set it in .env for persistence.",
                stacklevel=2,
            )
            return secrets.token_hex(32)
        return v

    @property
    def owner_names(self) -> list[str]:
        """Parsed owner-name variants for self-transfer detection (lower-cased)."""
        raw = self.finance_tracker_owner_names or ""
        return [n.strip().lower() for n in raw.split(",") if n.strip()]

    @property
    def allowed_origins_list(self) -> list[str]:
        """Parsed CORS origin allow-list."""
        raw = self.allowed_origins or ""
        return [o.strip() for o in raw.split(",") if o.strip()]

    @property
    def host_is_loopback(self) -> bool:
        """Whether ``backend_host`` is restricted to localhost."""
        return self.backend_host in {"127.0.0.1", "::1", "localhost"}

    @property
    def data_dir(self) -> Path:
        return _DATA_DIR

    @property
    def pdf_storage_dir(self) -> Path:
        d = _DATA_DIR / "pdfs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def backup_dir(self) -> Path:
        d = _DATA_DIR / "backups"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def db_path(self) -> Path:
        """Return the SQLite file path from the database_url, anchored to the
        project root for relative paths so the data file is the same regardless
        of cwd (Sprint 3 FIX-17 — review V2 §V2-P2-3).
        """
        url = self.database_url
        if url.startswith("sqlite:////"):
            # 4 slashes → absolute path
            return Path(url[len("sqlite:///"):])
        if url.startswith("sqlite:///"):
            rel = url[len("sqlite:///"):]
            p = Path(rel)
            if p.is_absolute():
                return p
            # Strip a leading "./" so "./data/finance.db" → "data/finance.db"
            cleaned = rel[2:] if rel.startswith("./") else rel
            return _PROJECT_ROOT / cleaned
        return _DATA_DIR / "finance.db"

    @property
    def resolved_database_url(self) -> str:
        """``database_url`` with relative SQLite paths anchored to project root.

        Use this everywhere instead of the raw ``database_url`` so engines
        agree on the same file no matter what cwd they were started from.
        """
        url = self.database_url
        if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
            return f"sqlite:///{self.db_path}"
        return url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
