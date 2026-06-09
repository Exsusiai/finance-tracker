"""End-to-end LLM classification orchestrator.

Flow:
    1. Read llm_settings; bail if disabled / over budget / key missing.
    2. Load the category tree, enabled keyword rules, top-N relevant notes.
    3. Build the prompt; call the provider with grounding per setting.
    4. Resolve `category_path` ("住家/房租") to a Category row.
    5. Write tx fields + record cost. Bump note.usage_count for the
       knowledge-base entries we surfaced (best-effort).
"""
from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CategorizationNote,
    CategorizationRule,
    Category,
    Transaction,
    touch_updated_at,
)
from app.services import app_settings as app_settings_svc
from app.services.llm.cost_tracker import check_budget, record_cost
from app.services.llm.gemini import GeminiProvider
from app.services.llm.prompt import build_classification_prompt
from app.services.llm.provider import (
    ClassificationResult,
    LLMProvider,
    LLMUnavailableError,
)

logger = structlog.get_logger(__name__)


@dataclass
class LLMClassifyOutcome:
    matched: bool          # category_id was set (above threshold)
    suggested: bool        # LLM produced a path but below threshold
    result: ClassificationResult | None
    note_ids_used: list[int]


def _tokens(text: str) -> set[str]:
    if not text:
        return set()
    out = set()
    for t in text.replace(",", " ").replace(".", " ").split():
        cleaned = t.strip("\"'()[]{}*/-:|;,").lower()
        if len(cleaned) >= 3:
            out.add(cleaned)
    return out


async def _select_relevant_notes(
    db: AsyncSession,
    tx: Transaction,
    *,
    limit: int,
) -> list[CategorizationNote]:
    """Token-overlap top-N over enabled notes.

    MVP heuristic — replace with embeddings later if recall becomes a
    problem. We score by overlap between the tx's text and the note's
    trigger_text. Ties broken by usage_count, then most recent.
    """
    rows = (
        await db.execute(
            select(CategorizationNote).where(CategorizationNote.enabled.is_(True))
        )
    ).scalars().all()
    if not rows:
        return []

    tx_tokens = _tokens(" ".join([
        tx.description or "",
        tx.raw_description or "",
        tx.counterparty or "",
    ]))
    if not tx_tokens:
        # No tokens to match — return most-used notes as global context.
        return sorted(rows, key=lambda n: (-n.usage_count, -n.id))[:limit]

    scored: list[tuple[int, int, CategorizationNote]] = []
    for note in rows:
        overlap = len(tx_tokens & _tokens(note.trigger_text + " " + note.note_text))
        scored.append((overlap, note.usage_count, note))
    scored.sort(key=lambda t: (-t[0], -t[1], -t[2].id))
    # Keep notes with at least 1 token overlap; if we still have room, fill with global top
    relevant = [n for score, _, n in scored if score > 0][:limit]
    if len(relevant) < limit:
        for _, _, n in scored:
            if n not in relevant:
                relevant.append(n)
                if len(relevant) >= limit:
                    break
    return relevant


def _resolve_category_path(
    path: str | None,
    categories: list[Category],
) -> Category | None:
    if not path:
        return None
    parts = [p.strip() for p in path.split("/") if p.strip()]
    if not parts:
        return None
    by_id = {c.id: c for c in categories}
    if len(parts) == 1:
        # Top-level only — pick the parent category if it exists
        for c in categories:
            if c.parent_id is None and c.name == parts[0]:
                return c
        return None
    parent_name, child_name = parts[0], parts[1]
    for c in categories:
        if c.parent_id is None or c.name != child_name:
            continue
        parent = by_id.get(c.parent_id) if c.parent_id else None
        if parent is not None and parent.name == parent_name:
            return c
    # Fallback: child name unique anywhere in the tree
    by_child_name = [c for c in categories if c.name == child_name and c.parent_id is not None]
    if len(by_child_name) == 1:
        return by_child_name[0]
    return None


async def _build_provider(db: AsyncSession) -> LLMProvider:
    runtime = await app_settings_svc.get_llm_settings(db)
    if not runtime.enabled:
        raise LLMUnavailableError("llm_disabled")
    if runtime.provider != "gemini":
        raise LLMUnavailableError(f"unsupported_provider:{runtime.provider}")
    api_key = await app_settings_svc.get_gemini_api_key(db)
    if not api_key:
        raise LLMUnavailableError("gemini_api_key_missing")
    return GeminiProvider(model=runtime.model, api_key=api_key)


async def classify_with_llm(
    db: AsyncSession,
    tx: Transaction,
    *,
    provider: LLMProvider | None = None,
) -> LLMClassifyOutcome:
    """Run the full LLM classification flow for one transaction.

    `provider` may be supplied for testing (any object satisfying
    LLMProvider). When None, we build the configured one (Gemini today).
    """
    runtime = await app_settings_svc.get_llm_settings(db)
    if not runtime.enabled:
        return LLMClassifyOutcome(matched=False, suggested=False, result=None, note_ids_used=[])

    within_budget, used, budget = await check_budget(db)
    if not within_budget:
        logger.info("llm_skip_budget_exceeded", used_usd=used, budget_usd=budget)
        return LLMClassifyOutcome(matched=False, suggested=False, result=None, note_ids_used=[])

    try:
        prov = provider or await _build_provider(db)
    except LLMUnavailableError as exc:
        logger.info("llm_unavailable", reason=str(exc))
        return LLMClassifyOutcome(matched=False, suggested=False, result=None, note_ids_used=[])

    categories = (await db.execute(select(Category))).scalars().all()
    rules = (
        await db.execute(
            select(CategorizationRule).where(CategorizationRule.enabled.is_(True))
        )
    ).scalars().all()
    notes = await _select_relevant_notes(db, tx, limit=runtime.max_notes_in_prompt)

    # Account context (best-effort)
    from app.models import Account
    acc = (
        await db.execute(select(Account).where(Account.id == tx.account_id))
    ).scalar_one_or_none()
    account_name = acc.name if acc else "?"
    account_currency = acc.currency if acc else (tx.currency or "?")

    prompt = build_classification_prompt(
        tx,
        categories=list(categories),
        rules=list(rules),
        notes=notes,
        account_name=account_name,
        account_currency=account_currency,
    )

    # Grounding (Google Search) adds 5-15 s latency on top of the model
    # call. The default 15 s budget is enough for non-grounded calls but
    # routinely times out grounded ones — bump to 45 s when grounding is on.
    timeout_s = 45.0 if runtime.use_grounding else 15.0
    result = await prov.classify(prompt, use_grounding=runtime.use_grounding, timeout_s=timeout_s)

    if result.cost_usd > 0:
        await record_cost(db, result.cost_usd)

    # Bump usage_count for surfaced notes (best-effort, non-fatal)
    note_ids = [n.id for n in notes]
    for n in notes:
        n.usage_count = (n.usage_count or 0) + 1
        touch_updated_at(n)
    await db.flush()

    # Stamp LLM attempt timestamp on every reachable row — even abstains.
    # Without this, refresh-matching keeps re-dispatching the same hopeless
    # rows every time the user clicks the button, burning the monthly
    # budget on rows Gemini can't classify (e.g. credit-card payment
    # entries that should really be transfers).
    import json as _json
    from datetime import datetime, timezone

    def _stamp_attempt(extra: dict | None = None) -> None:
        try:
            meta_now = _json.loads(tx.metadata_json) if tx.metadata_json else {}
            if not isinstance(meta_now, dict):
                meta_now = {}
        except (ValueError, TypeError):
            meta_now = {}
        attempt = {
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model": getattr(prov, "model", ""),
            "outcome": (extra or {}).get("outcome", "abstain"),
        }
        if extra:
            attempt.update({k: v for k, v in extra.items() if k != "outcome"})
        meta_now["llm_attempt"] = attempt
        tx.metadata_json = _json.dumps(meta_now, ensure_ascii=False, sort_keys=True)
        touch_updated_at(tx)

    if result.category_path is None:
        # Don't cache transient failures (timeout / network error / rate
        # limit / quota) — those should retry on the next refresh once the
        # condition clears. Only persistent "model can't classify this"
        # abstains get stamped, so we stop wasting calls on hopeless rows.
        # NOTE: "llm_rate_limited" MUST be here — otherwise a quota-exhausted
        # batch gets permanently stamped and refresh-matching skips it
        # forever even after quota resets (ERR-20260607-002).
        reason = result.reason or ""
        is_transient = (
            reason in ("llm_timeout", "llm_rate_limited", "llm_no_response")
            or reason.startswith("llm_error:")
        )
        if not is_transient:
            _stamp_attempt({"outcome": "abstain", "reason": reason})
            await db.flush()
        return LLMClassifyOutcome(matched=False, suggested=False, result=result, note_ids_used=note_ids)

    target = _resolve_category_path(result.category_path, list(categories))
    if target is None:
        logger.warning(
            "llm_path_unresolved",
            tx_id=tx.id,
            path=result.category_path,
        )
        _stamp_attempt({"outcome": "path_unresolved", "path": result.category_path})
        await db.flush()
        return LLMClassifyOutcome(matched=False, suggested=True, result=result, note_ids_used=note_ids)

    # Kind guard — reuse the same invariant as L1
    if target.kind != tx.type:
        logger.warning(
            "llm_kind_mismatch",
            tx_id=tx.id,
            tx_type=tx.type,
            category_kind=target.kind,
        )
        _stamp_attempt({
            "outcome": "kind_mismatch",
            "path": result.category_path,
            "tx_type": tx.type,
            "category_kind": target.kind,
        })
        await db.flush()
        return LLMClassifyOutcome(matched=False, suggested=True, result=result, note_ids_used=note_ids)

    if result.confidence >= runtime.confidence_threshold:
        tx.category_id = target.id
        tx.is_pending = False
        tx.categorization_method = "llm"
        tx.categorization_confidence = result.confidence
        tx.llm_reason = result.reason
        _stamp_attempt({
            "outcome": "matched",
            "path": result.category_path,
            "confidence": result.confidence,
        })
        await db.flush()
        return LLMClassifyOutcome(matched=True, suggested=True, result=result, note_ids_used=note_ids)

    # Below threshold → keep pending, but stash the suggestion for inbox UI
    try:
        meta = _json.loads(tx.metadata_json) if tx.metadata_json else {}
        if not isinstance(meta, dict):
            meta = {}
    except (ValueError, TypeError):
        meta = {}
    meta["llm_suggestion"] = {
        "category_id": target.id,
        "category_path": result.category_path,
        "confidence": result.confidence,
        "reason": result.reason,
        "used_search": result.used_search,
    }
    meta["llm_attempt"] = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": getattr(prov, "model", ""),
        "outcome": "low_confidence",
        "path": result.category_path,
        "confidence": result.confidence,
    }
    tx.metadata_json = _json.dumps(meta, ensure_ascii=False, sort_keys=True)
    touch_updated_at(tx)
    await db.flush()
    return LLMClassifyOutcome(matched=False, suggested=True, result=result, note_ids_used=note_ids)
