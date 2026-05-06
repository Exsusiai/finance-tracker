"""Tests for Sprint 2 FIX-9 security defaults (review V1 §P0-2).

Verifies:
- Settings ship with loopback default for `backend_host`.
- `host_is_loopback` correctly classifies common values.
- `allowed_origins_list` parses the CSV correctly and is not the wildcard.
- The lifespan refuses to start when ``AUTH_DISABLED=true`` is combined
  with a non-loopback bind.
"""

from __future__ import annotations

import os

import pytest


def test_default_backend_host_is_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pop user's .env-driven override and re-instantiate Settings to read the
    # raw class default rather than whatever happens to be in .env locally.
    monkeypatch.delenv("BACKEND_HOST", raising=False)
    monkeypatch.setenv("FINANCE_TRACKER_API_TOKEN", "a" * 64)
    from app.core.config import Settings

    s = Settings(_env_file=None)  # type: ignore[arg-type]
    assert s.backend_host == "127.0.0.1"
    assert s.host_is_loopback is True


@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("localhost", True),
        ("0.0.0.0", False),
        ("192.168.1.10", False),
        ("10.0.0.1", False),
    ],
)
def test_host_is_loopback_classification(host: str, expected: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINANCE_TRACKER_API_TOKEN", "a" * 64)
    monkeypatch.setenv("BACKEND_HOST", host)
    from app.core.config import Settings

    s = Settings(_env_file=None)  # type: ignore[arg-type]
    assert s.host_is_loopback is expected


def test_allowed_origins_parses_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINANCE_TRACKER_API_TOKEN", "a" * 64)
    monkeypatch.setenv(
        "ALLOWED_ORIGINS",
        "http://localhost:3000, http://example.com , https://app.test",
    )
    from app.core.config import Settings

    s = Settings(_env_file=None)  # type: ignore[arg-type]
    assert s.allowed_origins_list == [
        "http://localhost:3000",
        "http://example.com",
        "https://app.test",
    ]
    assert "*" not in s.allowed_origins_list


@pytest.mark.asyncio
async def test_lifespan_refuses_auth_disabled_with_public_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dangerous combination must fail loudly at startup."""
    monkeypatch.setenv("FINANCE_TRACKER_API_TOKEN", "a" * 64)
    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setenv("BACKEND_HOST", "0.0.0.0")

    # Force the cached Settings to refresh by clearing the lru_cache.
    from app.core.config import get_settings

    get_settings.cache_clear()
    # Re-import main so its module-level `settings = get_settings()` picks up
    # the patched env vars.
    import importlib
    import app.main

    importlib.reload(app.main)

    with pytest.raises(RuntimeError, match="AUTH_DISABLED"):
        async with app.main.lifespan(app.main.app):
            pass

    # Restore — clear cache + reload main so subsequent tests see clean state.
    get_settings.cache_clear()
    importlib.reload(app.main)


@pytest.mark.asyncio
async def test_lifespan_allows_auth_disabled_on_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local dev with AUTH_DISABLED=true on 127.0.0.1 should still boot."""
    monkeypatch.setenv("FINANCE_TRACKER_API_TOKEN", "a" * 64)
    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setenv("BACKEND_HOST", "127.0.0.1")

    from app.core.config import get_settings

    get_settings.cache_clear()
    import importlib
    import app.main

    importlib.reload(app.main)

    # Lifespan returns an async context — we just verify it enters cleanly.
    # Ignore database / scheduler side-effects by aborting right after entry.
    try:
        async with app.main.lifespan(app.main.app):
            pass
    finally:
        get_settings.cache_clear()
        importlib.reload(app.main)
