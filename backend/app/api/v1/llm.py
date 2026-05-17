"""LLM settings + cost endpoints.

Read/write the runtime LLM configuration stored in `app_settings`. The
provider API key (GEMINI_API_KEY) is read from env only — never echoed
in responses, never accepted via this API.
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
    runtime = await app_settings_svc.get_llm_settings(db)
    api_key = await app_settings_svc.get_gemini_api_key(db)
    return LLMSettingsOut(
        enabled=runtime.enabled,
        provider=runtime.provider,
        model=runtime.model,
        monthly_usd_budget=runtime.monthly_usd_budget,
        confidence_threshold=runtime.confidence_threshold,
        use_grounding=runtime.use_grounding,
        max_notes_in_prompt=runtime.max_notes_in_prompt,
        api_key_present=bool(api_key),
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

    # Pull api key out of the dict — it goes to a separate row (and is
    # never echoed back). Empty string means "clear it".
    api_key_raw = payload.pop("gemini_api_key", None)
    if api_key_raw is not None:
        trimmed = api_key_raw.strip()
        if trimmed:
            await app_settings_svc.set_setting(db, "gemini_api_key", trimmed)
        else:
            await app_settings_svc.delete_setting(db, "gemini_api_key")

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
