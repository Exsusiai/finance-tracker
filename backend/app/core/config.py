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

    # --- Base ---
    base_currency: str = "CNY"
    backend_host: str = "0.0.0.0"
    backend_port: int = 8000
    log_level: str = "INFO"

    # --- Database ---
    database_url: str = f"sqlite:///{_DATA_DIR / 'finance.db'}"

    # --- Market data sources ---
    coingecko_api_key: str = ""
    goldapi_key: str = ""
    market_refresh_crypto_sec: int = 300
    market_refresh_stock_sec: int = 900
    market_refresh_fx_sec: int = 3600
    market_refresh_gold_sec: int = 3600

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
        """Return the SQLite file path from the database_url."""
        url = self.database_url
        if url.startswith("sqlite:///"):
            rel = url[len("sqlite:///") :]
            return Path(rel) if not rel.startswith("/") else Path(rel)
        if url.startswith("sqlite:////"):
            return Path(url[len("sqlite:///"):])
        return _DATA_DIR / "finance.db"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
