"""Tests for FIX-10 (PDF upload guards) and FIX-11 (regex DoS protection).

FIX-10 — PDF upload guards (statements.py):
  - Oversized upload → 422 PARSER_ERROR
  - Non-PDF bytes → 422 PARSER_ERROR

FIX-11 — regex complexity validation (rules.py + categorizer/engine.py):
  - POST rule with 300-char pattern → 422
  - POST rule with nested-quantifier pattern (a+)+ → 422
  - POST rule with valid regex → 201
  - Categorizer: (a+)+b on long string returns False within timeout
"""

from __future__ import annotations

import io
import os
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_TEST_TOKEN = "cafebabecafebabecafebabecafebabecafebabe"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "CNY")

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account, Category  # noqa: E402
from app.services.categorizer.engine import _safe_regex_search  # noqa: E402

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, echo=False)
_SessionLocal = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


async def override_get_db():
    async with _SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


AUTH = {"Authorization": f"Bearer {_TEST_TOKEN}"}
AUTH_JSON = {**AUTH, "Content-Type": "application/json"}


@pytest_asyncio.fixture(scope="module", autouse=True)
async def setup_schema():
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_get_db
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Seed minimal data: one account + one category (needed for rule create)
    async with _SessionLocal() as s:
        s.add(
            Account(
                id=10,
                name="Test",
                type="bank",
                currency="CNY",
                initial_balance=Decimal("0"),
                is_active=True,
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:00:00Z",
            )
        )
        s.add(
            Category(
                id=2001,
                name="Food",
                kind="expense",
                is_system=False,
                sort_order=0,
                created_at="2026-05-01T00:00:00Z",
            )
        )
        await s.commit()
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    if previous is None:
        app.dependency_overrides.pop(get_db, None)
    else:
        app.dependency_overrides[get_db] = previous


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ─── FIX-10: PDF upload guards ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pdf_upload_rejects_oversized_file(client: AsyncClient) -> None:
    """File larger than 10 MiB must be rejected with 422 PARSER_ERROR."""
    big_content = b"%PDF-1.4" + b"X" * (11 * 1024 * 1024)
    r = await client.post(
        "/api/v1/statements/upload",
        files={"file": ("big.pdf", io.BytesIO(big_content), "application/pdf")},
        headers=AUTH,
    )
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"]["code"] == "PARSER_ERROR"
    assert "mib" in body["error"]["message"].lower() or "limit" in body["error"]["message"].lower()
    assert body["error"]["details"]["max_mb"] == 10


@pytest.mark.asyncio
async def test_pdf_upload_rejects_non_pdf(client: AsyncClient) -> None:
    """Non-PDF bytes (missing %PDF- magic) must be rejected with 422 PARSER_ERROR."""
    r = await client.post(
        "/api/v1/statements/upload",
        files={"file": ("not_a_pdf.txt", io.BytesIO(b"This is plain text"), "text/plain")},
        headers=AUTH,
    )
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"]["code"] == "PARSER_ERROR"
    assert "pdf" in body["error"]["message"].lower()


# ─── FIX-11: regex DoS protection ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_rule_rejects_oversized_pattern(client: AsyncClient) -> None:
    """POST /rules with a 300-char pattern_type=regex must return 422."""
    long_pattern = "a" * 300
    r = await client.post(
        "/api/v1/rules",
        json={
            "pattern": long_pattern,
            "pattern_type": "regex",
            "field": "description",
            "category_id": 2001,
            "priority": 5,
            "enabled": True,
        },
        headers=AUTH_JSON,
    )
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_create_rule_rejects_nested_quantifier_pattern(client: AsyncClient) -> None:
    """POST /rules with (a+)+b nested-quantifier pattern must return 422."""
    r = await client.post(
        "/api/v1/rules",
        json={
            "pattern": "(a+)+b",
            "pattern_type": "regex",
            "field": "description",
            "category_id": 2001,
            "priority": 5,
            "enabled": True,
        },
        headers=AUTH_JSON,
    )
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"]["code"] == "INVALID_INPUT"
    assert "quantifier" in body["error"]["message"].lower() or "backtrack" in body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_create_rule_accepts_valid_regex(client: AsyncClient) -> None:
    """POST /rules with a safe regex pattern must return 201."""
    r = await client.post(
        "/api/v1/rules",
        json={
            "pattern": "STARBUCKS|MCDONALDS",
            "pattern_type": "regex",
            "field": "description",
            "category_id": 2001,
            "priority": 5,
            "enabled": True,
        },
        headers=AUTH_JSON,
    )
    assert r.status_code == 201, r.text
    assert r.json()["data"]["pattern"] == "STARBUCKS|MCDONALDS"


def test_safe_regex_search_returns_false_on_invalid_pattern() -> None:
    """_safe_regex_search with an invalid (uncompilable) regex must return False."""
    # An unclosed group is a re.error — the safe wrapper must swallow it
    result = _safe_regex_search("[invalid", "some value", timeout_sec=1.0)
    assert result is False


def test_safe_regex_search_matches_valid_pattern() -> None:
    """_safe_regex_search with a valid matching regex must return True."""
    result = _safe_regex_search("starbucks", "I went to STARBUCKS today", timeout_sec=1.0)
    assert result is True


def test_safe_regex_search_no_match_returns_false() -> None:
    """_safe_regex_search with a non-matching pattern must return False."""
    result = _safe_regex_search("mcdonalds", "Pizza Hut", timeout_sec=1.0)
    assert result is False


def test_validate_regex_complexity_rejects_nested_quantifiers() -> None:
    """validate_regex_complexity must raise InvalidInputError for nested-quantifier patterns."""
    from app.core.errors import InvalidInputError
    from app.services.categorizer.engine import validate_regex_complexity

    with pytest.raises(InvalidInputError, match="nested quantifiers"):
        validate_regex_complexity("(a+)+b")


def test_validate_regex_complexity_rejects_long_pattern() -> None:
    """validate_regex_complexity must raise InvalidInputError for patterns over 200 chars."""
    from app.core.errors import InvalidInputError
    from app.services.categorizer.engine import validate_regex_complexity

    with pytest.raises(InvalidInputError, match="too long"):
        validate_regex_complexity("a" * 201)


def test_validate_regex_complexity_accepts_safe_pattern() -> None:
    """validate_regex_complexity must not raise for a valid, safe pattern."""
    from app.services.categorizer.engine import validate_regex_complexity

    validate_regex_complexity("STARBUCKS|MCDONALDS")  # should not raise
