"""Regression tests for the L2 LLM classification system (P1-1).

Coverage:
- Gemini provider JSON parsing (success / malformed / code-fenced)
- Cost tracker monthly accumulation
- Knowledge base note retrieval (token overlap top-N)
- requires_llm propagation: rule-match short-circuits only when
  requires_llm=False; pollution path forces L2 even on hit
- record_note_to_kb: user_note → CategorizationNote + same-keyword rules
  flipped to requires_llm=True
- classify_with_llm with a fake provider: high-confidence落库 / low-conf
  stashes suggestion in metadata / abstain leaves pending
"""
from __future__ import annotations

import os
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_TEST_TOKEN = "feedfacefeedfacefeedfacefeedfacefeedface"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "EUR")

from app.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    AppSetting,
    CategorizationNote,
    CategorizationRule,
    Category,
    Transaction,
    _utcnow_str,
)
from app.services import app_settings as app_settings_svc  # noqa: E402
from app.services.app_settings import seed_defaults  # noqa: E402
from app.services.categorizer.engine import (  # noqa: E402
    MatchResult,
    categorize_transaction,
    record_note_to_kb,
)
from app.services.llm.classifier import classify_with_llm, _resolve_category_path  # noqa: E402
from app.services.llm.cost_tracker import (  # noqa: E402
    _current_month_key,
    get_current_cost,
    record_cost,
)
from app.services.llm.gemini import _parse_classification  # noqa: E402
from app.services.llm.prompt import build_classification_prompt  # noqa: E402
from app.services.llm.provider import ClassificationResult  # noqa: E402


_TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(_TEST_DB_URL, echo=False)
_Session = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture(scope="function")
async def db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with _Session() as session:
        # Seed defaults so app_settings_svc.get_llm_settings works.
        await seed_defaults(session)
        await session.commit()
    async with _Session() as session:
        yield session


# ─── Gemini JSON parsing ───────────────────────────────────────────────────


def test_parse_clean_json():
    raw = '{"category_path": "住家/房租", "confidence": 0.92, "reason": "x", "used_search": false}'
    path, conf, reason, search = _parse_classification(raw)
    assert path == "住家/房租"
    assert conf == 0.92
    assert reason == "x"
    assert search is False


def test_parse_code_fenced_json():
    raw = '```json\n{"category_path": "餐饮", "confidence": 0.7}\n```'
    path, conf, _, _ = _parse_classification(raw)
    assert path == "餐饮"
    assert conf == 0.7


def test_parse_prose_wrapped_json():
    raw = 'Here is my answer: {"category_path": null, "confidence": 0.0, "reason": "abstain"}'
    path, conf, reason, _ = _parse_classification(raw)
    assert path is None
    assert conf == 0.0
    assert reason == "abstain"


def test_parse_garbage_returns_abstain():
    path, conf, _, _ = _parse_classification("not json at all")
    assert path is None
    assert conf == 0.0


def test_parse_clamps_confidence():
    path, conf, _, _ = _parse_classification('{"category_path": "a", "confidence": 1.5}')
    assert conf == 1.0
    path, conf, _, _ = _parse_classification('{"category_path": "a", "confidence": -0.5}')
    assert conf == 0.0


# ─── Cost tracker ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cost_tracker_accumulates(db):
    assert await get_current_cost(db) == 0.0
    new_total = await record_cost(db, 0.001234)
    await db.commit()
    assert abs(new_total - 0.001234) < 1e-9
    new_total = await record_cost(db, 0.0001)
    await db.commit()
    assert abs(new_total - 0.001334) < 1e-9


@pytest.mark.asyncio
async def test_cost_tracker_negative_is_noop(db):
    await record_cost(db, 0.5)
    await db.commit()
    new = await record_cost(db, -1.0)
    await db.commit()
    assert new == 0.5


# ─── Knowledge base + requires_llm ─────────────────────────────────────────


@pytest_asyncio.fixture()
async def seed_paypal_world(db):
    """Seed: 1 account, 4 categories (food/subscription parents+children), 1
    expense-kind rule for "PayPal" → 餐饮·快餐 (no requires_llm).
    """
    db.add(Account(
        id=10, name="N26", type="bank", currency="EUR",
        initial_balance=Decimal("0"), is_active=True,
        created_at=_utcnow_str(), updated_at=_utcnow_str(),
    ))
    food_parent = Category(
        id=20, name="餐饮", kind="expense", is_system=False, sort_order=0,
        created_at=_utcnow_str(), updated_at=_utcnow_str(),
    )
    food_child = Category(
        id=21, name="快餐", kind="expense", parent_id=20,
        is_system=False, sort_order=0,
        created_at=_utcnow_str(), updated_at=_utcnow_str(),
    )
    sub_parent = Category(
        id=30, name="订阅服务", kind="expense", is_system=False, sort_order=0,
        created_at=_utcnow_str(), updated_at=_utcnow_str(),
    )
    sub_child = Category(
        id=31, name="软件订阅", kind="expense", parent_id=30,
        is_system=False, sort_order=0,
        created_at=_utcnow_str(), updated_at=_utcnow_str(),
    )
    db.add_all([food_parent, food_child, sub_parent, sub_child])
    db.add(CategorizationRule(
        id=100, pattern="PayPal", pattern_type="contains",
        field="description", category_id=21, priority=5,
        enabled=True, hit_count=0, requires_llm=False,
        created_at=_utcnow_str(), updated_at=_utcnow_str(),
    ))
    await db.commit()
    return db


@pytest.mark.asyncio
async def test_l1_short_circuits_when_requires_llm_false(seed_paypal_world):
    db = seed_paypal_world
    tx = Transaction(
        account_id=10, occurred_at="2026-05-01T00:00:00Z",
        amount=Decimal("12.5"), currency="EUR", type="expense",
        description="PayPal random merchant", source="pdf_import",
        is_pending=True, created_at=_utcnow_str(), updated_at=_utcnow_str(),
    )
    db.add(tx)
    await db.flush()
    result = await categorize_transaction(db, tx)
    assert isinstance(result, MatchResult)
    assert result.matched is True
    assert result.requires_llm is False
    assert tx.category_id == 21


@pytest.mark.asyncio
async def test_l1_flags_requires_llm_when_rule_polluted(seed_paypal_world):
    db = seed_paypal_world
    # User wrote a note → record_note_to_kb should mark the rule.
    tx = Transaction(
        account_id=10, occurred_at="2026-04-01T00:00:00Z",
        amount=Decimal("2.99"), currency="EUR", type="expense",
        description="PayPal *NetflixSub", source="pdf_import",
        is_pending=False, category_id=21,  # pre-classified by L1 (wrong)
        created_at=_utcnow_str(), updated_at=_utcnow_str(),
    )
    db.add(tx)
    await db.flush()
    note_id = await record_note_to_kb(
        db, tx=tx, new_category_id=31,
        note_text="PayPal 每月 2.99 EUR 是 Netflix 订阅",
    )
    await db.commit()
    assert note_id is not None

    # Verify the rule was flipped
    from sqlalchemy import select
    rule = (await db.execute(
        select(CategorizationRule).where(CategorizationRule.id == 100)
    )).scalar_one()
    assert rule.requires_llm is True

    # Verify the note exists
    notes = (await db.execute(select(CategorizationNote))).scalars().all()
    assert len(notes) == 1
    assert "PayPal" in notes[0].trigger_text
    assert notes[0].category_id == 31

    # Now ingest a NEW PayPal tx → categorize_transaction should still match
    # the rule but requires_llm=True (so caller would route to L2).
    new_tx = Transaction(
        account_id=10, occurred_at="2026-05-01T00:00:00Z",
        amount=Decimal("2.99"), currency="EUR", type="expense",
        description="PayPal *NetflixSub", source="pdf_import",
        is_pending=True, created_at=_utcnow_str(), updated_at=_utcnow_str(),
    )
    db.add(new_tx)
    await db.flush()
    result = await categorize_transaction(db, new_tx)
    assert result.matched is True
    assert result.requires_llm is True


@pytest.mark.asyncio
async def test_record_note_to_kb_skips_empty(seed_paypal_world):
    db = seed_paypal_world
    tx = Transaction(
        account_id=10, occurred_at="2026-05-02T00:00:00Z",
        amount=Decimal("5"), currency="EUR", type="expense",
        description="anything", source="pdf_import",
        created_at=_utcnow_str(), updated_at=_utcnow_str(),
    )
    db.add(tx)
    await db.flush()
    note_id = await record_note_to_kb(db, tx=tx, new_category_id=21, note_text="   ")
    assert note_id is None


# ─── classify_with_llm with fake provider ─────────────────────────────────


class _FakeProvider:
    """Minimal LLMProvider satisfying the Protocol."""

    name = "fake"
    model = "fake-1"

    def __init__(self, result: ClassificationResult):
        self._result = result

    async def classify(self, prompt, *, use_grounding, timeout_s=15.0):
        return self._result


@pytest_asyncio.fixture()
async def enabled_llm(db):
    await app_settings_svc.set_setting(db, "llm_enabled", "true")
    await app_settings_svc.set_setting(db, "llm_confidence_threshold", "0.7")
    await db.commit()
    return db


@pytest.mark.asyncio
async def test_classify_high_confidence_lands_category(enabled_llm, seed_paypal_world):
    db = seed_paypal_world
    await app_settings_svc.set_setting(db, "llm_enabled", "true")
    await app_settings_svc.set_setting(db, "llm_confidence_threshold", "0.7")
    await db.commit()

    tx = Transaction(
        account_id=10, occurred_at="2026-05-04T00:00:00Z",
        amount=Decimal("2.99"), currency="EUR", type="expense",
        description="PayPal subscription unknown", source="pdf_import",
        is_pending=True, created_at=_utcnow_str(), updated_at=_utcnow_str(),
    )
    db.add(tx)
    await db.flush()
    fake = _FakeProvider(ClassificationResult(
        category_path="订阅服务/软件订阅",
        confidence=0.85,
        reason="matched note",
        used_search=False,
        input_tokens=120, output_tokens=50, cost_usd=0.0001,
    ))
    outcome = await classify_with_llm(db, tx, provider=fake)
    await db.commit()

    assert outcome.matched is True
    assert outcome.suggested is True
    assert tx.category_id == 31
    assert tx.is_pending is False
    assert tx.categorization_method == "llm"
    assert tx.categorization_confidence == 0.85
    assert tx.llm_reason == "matched note"


@pytest.mark.asyncio
async def test_classify_low_confidence_stashes_suggestion(enabled_llm, seed_paypal_world):
    import json
    db = seed_paypal_world
    await app_settings_svc.set_setting(db, "llm_enabled", "true")
    await app_settings_svc.set_setting(db, "llm_confidence_threshold", "0.7")
    await db.commit()

    tx = Transaction(
        account_id=10, occurred_at="2026-05-05T00:00:00Z",
        amount=Decimal("12.5"), currency="EUR", type="expense",
        description="UNKNOWN MERCHANT XYZ", source="pdf_import",
        is_pending=True, created_at=_utcnow_str(), updated_at=_utcnow_str(),
    )
    db.add(tx)
    await db.flush()
    fake = _FakeProvider(ClassificationResult(
        category_path="餐饮/快餐",
        confidence=0.4,  # below threshold 0.7
        reason="weak match",
        used_search=True,
        input_tokens=80, output_tokens=30, cost_usd=0.00005,
    ))
    outcome = await classify_with_llm(db, tx, provider=fake)
    await db.commit()

    assert outcome.matched is False
    assert outcome.suggested is True
    assert tx.category_id is None
    assert tx.is_pending is True
    assert tx.metadata_json is not None
    meta = json.loads(tx.metadata_json)
    assert "llm_suggestion" in meta
    assert meta["llm_suggestion"]["category_id"] == 21
    assert meta["llm_suggestion"]["confidence"] == 0.4
    assert meta["llm_suggestion"]["used_search"] is True


@pytest.mark.asyncio
async def test_gemini_key_app_settings_overrides_env(db, monkeypatch):
    """app_settings stored key wins over env. Empty stored key → env fallback."""
    from app.core.config import get_settings as _gs
    from app.services.app_settings import get_gemini_api_key

    # Force the env-side key
    monkeypatch.setattr(_gs(), "gemini_api_key", "env-key")
    # No stored key → falls back to env
    assert (await get_gemini_api_key(db)) == "env-key"
    # Stored key wins
    await app_settings_svc.set_setting(db, "gemini_api_key", "ui-key")
    assert (await get_gemini_api_key(db)) == "ui-key"
    # Clearing returns env again
    await app_settings_svc.delete_setting(db, "gemini_api_key")
    assert (await get_gemini_api_key(db)) == "env-key"


@pytest.mark.asyncio
async def test_classify_disabled_is_noop(seed_paypal_world):
    db = seed_paypal_world
    # llm_enabled defaults to false from seed_defaults
    tx = Transaction(
        account_id=10, occurred_at="2026-05-06T00:00:00Z",
        amount=Decimal("1"), currency="EUR", type="expense",
        description="abc", source="pdf_import",
        is_pending=True, created_at=_utcnow_str(), updated_at=_utcnow_str(),
    )
    db.add(tx)
    await db.flush()
    fake = _FakeProvider(ClassificationResult(
        category_path="餐饮/快餐", confidence=0.99,
        reason="x", used_search=False,
        input_tokens=0, output_tokens=0, cost_usd=0.0,
    ))
    outcome = await classify_with_llm(db, tx, provider=fake)
    assert outcome.matched is False
    assert outcome.result is None
    assert tx.category_id is None


@pytest.mark.asyncio
async def test_classify_kind_mismatch_does_not_land(enabled_llm, seed_paypal_world):
    db = seed_paypal_world
    await app_settings_svc.set_setting(db, "llm_enabled", "true")
    await db.commit()
    tx = Transaction(
        account_id=10, occurred_at="2026-05-07T00:00:00Z",
        amount=Decimal("100"), currency="EUR", type="income",  # income tx
        description="payroll", source="pdf_import",
        is_pending=True, created_at=_utcnow_str(), updated_at=_utcnow_str(),
    )
    db.add(tx)
    await db.flush()
    # LLM proposes an EXPENSE category — kind guard should reject
    fake = _FakeProvider(ClassificationResult(
        category_path="餐饮/快餐", confidence=0.95,
        reason="x", used_search=False,
        input_tokens=10, output_tokens=10, cost_usd=0.00001,
    ))
    outcome = await classify_with_llm(db, tx, provider=fake)
    assert outcome.matched is False
    assert tx.category_id is None
    assert tx.is_pending is True


@pytest.mark.asyncio
async def test_classify_budget_exceeded_short_circuits(enabled_llm, seed_paypal_world):
    db = seed_paypal_world
    await app_settings_svc.set_setting(db, "llm_enabled", "true")
    await app_settings_svc.set_setting(db, "llm_monthly_usd_budget", "0.001")
    await record_cost(db, 0.5)  # blow past budget
    await db.commit()

    tx = Transaction(
        account_id=10, occurred_at="2026-05-08T00:00:00Z",
        amount=Decimal("1"), currency="EUR", type="expense",
        description="abc", source="pdf_import",
        is_pending=True, created_at=_utcnow_str(), updated_at=_utcnow_str(),
    )
    db.add(tx)
    await db.flush()
    fake = _FakeProvider(ClassificationResult(
        category_path="餐饮/快餐", confidence=0.99,
        reason="x", used_search=False,
        input_tokens=0, output_tokens=0, cost_usd=0.0,
    ))
    outcome = await classify_with_llm(db, tx, provider=fake)
    assert outcome.matched is False
    assert outcome.result is None  # Budget gate triggers BEFORE provider.classify


# ─── path resolution ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_category_path_two_level(seed_paypal_world):
    db = seed_paypal_world
    from sqlalchemy import select
    cats = (await db.execute(select(Category))).scalars().all()
    target = _resolve_category_path("餐饮/快餐", list(cats))
    assert target is not None
    assert target.id == 21
    target = _resolve_category_path("订阅服务/软件订阅", list(cats))
    assert target is not None
    assert target.id == 31
    target = _resolve_category_path("hellokitty/nonsense", list(cats))
    assert target is None


# ─── prompt construction ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prompt_includes_categories_and_notes(seed_paypal_world):
    db = seed_paypal_world
    from sqlalchemy import select
    cats = list((await db.execute(select(Category))).scalars().all())
    rules = list((await db.execute(select(CategorizationRule))).scalars().all())
    notes: list[CategorizationNote] = []

    # Add a note manually
    n = CategorizationNote(
        category_id=31,
        trigger_text="PayPal | 2.99 是订阅",
        note_text="PayPal 2.99 EUR is Netflix",
        usage_count=0, enabled=True,
        created_at=_utcnow_str(), updated_at=_utcnow_str(),
    )
    db.add(n)
    await db.flush()
    notes.append(n)

    tx = Transaction(
        account_id=10, occurred_at="2026-05-09T00:00:00Z",
        amount=Decimal("2.99"), currency="EUR", type="expense",
        description="PayPal *NetflixSub", source="pdf_import",
        is_pending=True, created_at=_utcnow_str(), updated_at=_utcnow_str(),
    )
    db.add(tx)
    await db.flush()
    prompt = build_classification_prompt(
        tx, categories=cats, rules=rules, notes=notes,
        account_name="N26", account_currency="EUR",
    )
    assert "## expense" in prompt
    assert "餐饮" in prompt
    assert "PayPal" in prompt
    assert "PayPal *NetflixSub" in prompt
    assert "PayPal 2.99 EUR is Netflix" in prompt
    assert "category_path" in prompt  # JSON schema mentioned


