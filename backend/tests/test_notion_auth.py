"""Auth gate regression tests for /api/v1/notion/* endpoints (FIX-8).

Every Notion endpoint must return 401 when no Authorization header is sent,
and must pass through (any non-401 status) when a valid Bearer token is sent.
"""

from __future__ import annotations

import os
from decimal import Decimal
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_TEST_TOKEN = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "CNY")

from app.core.config import Settings  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, echo=False)
_TestingSessionLocal = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)

# Build a Settings object with auth enabled and known token — bypasses .env and lru_cache.
_auth_settings = Settings(
    finance_tracker_api_token=_TEST_TOKEN,
    auth_disabled=False,
)


async def override_get_db():
    async with _TestingSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


AUTH = {"Authorization": f"Bearer {_TEST_TOKEN}"}


@pytest_asyncio.fixture(scope="module", autouse=True)
async def setup_schema():
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_get_db
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    if previous is None:
        app.dependency_overrides.pop(get_db, None)
    else:
        app.dependency_overrides[get_db] = previous


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    # Patch the module-level `settings` in auth so AUTH_DISABLED=false regardless
    # of what the project .env file contains.
    with patch("app.core.auth.settings", _auth_settings):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c


# ─── 401 without Authorization header ───────────────────────────────────────


@pytest.mark.asyncio
async def test_get_status_without_auth_returns_401(client: AsyncClient) -> None:
    r = await client.get("/api/v1/notion/status")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_post_sync_without_auth_returns_401(client: AsyncClient) -> None:
    r = await client.post("/api/v1/notion/sync")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_post_setup_without_auth_returns_401(client: AsyncClient) -> None:
    r = await client.post("/api/v1/notion/setup", params={"parent_page_id": "abc123"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_post_sync_transactions_without_auth_returns_401(client: AsyncClient) -> None:
    r = await client.post("/api/v1/notion/sync/transactions")
    assert r.status_code == 401


# ─── Auth gate passed with valid token ──────────────────────────────────────


@pytest.mark.asyncio
async def test_get_status_with_valid_token_passes_auth_gate(client: AsyncClient) -> None:
    """Auth gate is passed; response may be 200/400/500 depending on Notion config."""
    r = await client.get("/api/v1/notion/status", headers=AUTH)
    assert r.status_code != 401
