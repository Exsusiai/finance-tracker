"""LLM settings + cost endpoints.

Read/write the runtime LLM configuration stored in `app_settings`. The
provider API key may be submitted via PUT /settings as ``gemini_api_key``; it
is encrypted with AES-256-GCM before being stored and is never echoed back in
responses.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_auth
from app.db import get_db
from app.schemas import ApiSuccess, LLMCostOut, LLMSettingsOut, LLMSettingsUpdate
from app.services import app_settings as app_settings_svc
from app.services.llm.cost_tracker import _current_month_key, get_current_cost

router = APIRouter()
_auth = Annotated[str, Depends(require_auth)]


async def _build_settings_out(db: AsyncSession) -> LLMSettingsOut:
    from app.services.bank_sync.crypto import can_decrypt

    runtime = await app_settings_svc.get_llm_settings(db)
    api_key = await app_settings_svc.get_gemini_api_key(db)
    # Distinguish "no key" from "encrypted key present but undecryptable
    # because the encryption key rotated" (ERR-20260607-001). The latter
    # is the silent-abstain trap — flag it so the UI says "re-enter".
    enc = await app_settings_svc.get_setting(db, "gemini_api_key_enc", default=None)
    api_key_stale = bool(enc) and not can_decrypt(enc)
    return LLMSettingsOut(
        enabled=runtime.enabled,
        provider=runtime.provider,
        model=runtime.model,
        monthly_usd_budget=runtime.monthly_usd_budget,
        confidence_threshold=runtime.confidence_threshold,
        use_grounding=runtime.use_grounding,
        max_notes_in_prompt=runtime.max_notes_in_prompt,
        api_key_present=bool(api_key),
        api_key_stale=api_key_stale,
    )


@router.get("/settings", response_model=ApiSuccess[LLMSettingsOut])
async def get_llm_settings(_token: _auth, db: AsyncSession = Depends(get_db)):
    return ApiSuccess(data=await _build_settings_out(db))


@router.put("/settings", response_model=ApiSuccess[LLMSettingsOut])
async def update_llm_settings(
    body: LLMSettingsUpdate,
    _token: _auth,
    db: AsyncSession = Depends(get_db),
):
    payload = body.model_dump(exclude_unset=True)

    # Pull api key out of the dict — encrypted into a separate row and never
    # echoed back. Empty string means "clear it".
    api_key_raw = payload.pop("gemini_api_key", None)
    if api_key_raw is not None:
        trimmed = api_key_raw.strip()
        if trimmed:
            await app_settings_svc.set_gemini_api_key(db, trimmed)
        else:
            await app_settings_svc.delete_setting(db, "gemini_api_key_enc")

    mapping = {
        "enabled": "llm_enabled",
        "model": "llm_model",
        "monthly_usd_budget": "llm_monthly_usd_budget",
        "confidence_threshold": "llm_confidence_threshold",
        "use_grounding": "llm_use_grounding",
        "max_notes_in_prompt": "llm_max_notes_in_prompt",
    }
    for k, v in payload.items():
        await app_settings_svc.set_setting(db, mapping[k], v)

    return ApiSuccess(data=await _build_settings_out(db))


@router.get("/queue", response_model=ApiSuccess[dict])
async def get_llm_queue(_token: _auth):
    """Live classification-queue depth so the UI can show "AI 处理中 · 剩 N 笔"."""
    from app.services.llm import queue as llm_queue
    return ApiSuccess(data=llm_queue.status())


@router.get("/cost", response_model=ApiSuccess[LLMCostOut])
async def get_llm_cost(_token: _auth, db: AsyncSession = Depends(get_db)):
    runtime = await app_settings_svc.get_llm_settings(db)
    used = await get_current_cost(db)
    now = datetime.now(timezone.utc)
    return ApiSuccess(
        data=LLMCostOut(
            used_usd=round(used, 6),
            budget_usd=runtime.monthly_usd_budget,
            remaining_usd=max(0.0, runtime.monthly_usd_budget - used),
            period=f"{now.year:04d}-{now.month:02d}",
        )
    )
