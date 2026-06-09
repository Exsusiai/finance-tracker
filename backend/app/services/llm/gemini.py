"""Gemini provider — the only LLM backend currently wired up.

Uses the `google-genai` SDK. API key is read from the env var
`GEMINI_API_KEY`. The provider is constructed once per ingestion job
(cheap; just stores a client handle) and discarded.

Pricing fallbacks (USD per 1M tokens) are used when the API doesn't
return billing data; numbers come from Google's published prices for
gemini-2.5-flash as of 2026-04. Adjust via `_PRICING` when models change.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass

import structlog

from app.services.llm.provider import ClassificationResult, LLMUnavailableError

logger = structlog.get_logger(__name__)


# Per 1M tokens, USD. Conservative defaults; override per model as needed.
_PRICING: dict[str, tuple[float, float]] = {
    # (input, output)
    "gemini-2.5-flash": (0.075, 0.30),
    "gemini-2.5-flash-lite": (0.0375, 0.15),  # cheapest; widest free quota
    "gemini-2.5-pro": (1.25, 5.0),
    "gemini-2.0-flash": (0.075, 0.30),
}


_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")

# Transient upstream errors worth retrying with backoff. 429 =
# rate-limit / quota-per-minute, 503 = model temporarily overloaded.
# A genuinely-exhausted daily quota will keep returning 429 and we'll
# give up after the retries (correct — backoff can't conjure quota).
_TRANSIENT_MARKERS = ("429", "resource_exhausted", "503", "unavailable", "overloaded")


def _is_transient(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _TRANSIENT_MARKERS)


def _strip_code_fences(text: str) -> str:
    """Remove ```json … ``` wrappers some Gemini variants emit anyway."""
    text = text.strip()
    if text.startswith("```"):
        # drop the first fence line
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _parse_classification(raw: str) -> tuple[str | None, float, str, bool]:
    """Best-effort parse of the model's JSON output.

    Returns (category_path, confidence, reason, used_search). Falls back
    to an abstain verdict when the output is malformed — never raises.
    """
    cleaned = _strip_code_fences(raw)
    # If the model wrapped JSON in prose, try to pull it out
    if not cleaned.startswith("{"):
        m = _JSON_BLOCK_RE.search(cleaned)
        if m:
            cleaned = m.group(0)
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("llm_json_parse_failed", raw=raw[:200])
        return None, 0.0, "llm_output_unparseable", False
    if not isinstance(obj, dict):
        return None, 0.0, "llm_output_not_object", False
    path = obj.get("category_path")
    if path is not None and not isinstance(path, str):
        path = None
    confidence = obj.get("confidence", 0.0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reason = str(obj.get("reason", ""))[:1000]
    used_search = bool(obj.get("used_search", False))
    return path, confidence, reason, used_search


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    in_price, out_price = _PRICING.get(model, _PRICING["gemini-2.5-flash"])
    return (input_tokens * in_price + output_tokens * out_price) / 1_000_000


@dataclass
class GeminiProvider:
    name: str = "gemini"
    model: str = "gemini-2.5-flash"
    api_key: str = ""

    def __post_init__(self):
        if not self.api_key:
            raise LLMUnavailableError("GEMINI_API_KEY missing")

    async def classify(
        self,
        prompt: str,
        *,
        use_grounding: bool,
        timeout_s: float = 15.0,
    ) -> ClassificationResult:
        # Local import — keeps `google-genai` an optional runtime dep.
        try:
            from google import genai
            from google.genai import types as genai_types
        except ImportError as exc:  # pragma: no cover
            raise LLMUnavailableError(f"google-genai not installed: {exc}") from exc

        client = genai.Client(api_key=self.api_key)

        config_kwargs: dict = {
            "response_mime_type": "application/json",
            "temperature": 0.0,
        }
        if use_grounding:
            try:
                config_kwargs["tools"] = [
                    genai_types.Tool(google_search=genai_types.GoogleSearch())
                ]
                # response_mime_type isn't compatible with grounding tools
                # in older SDK versions; let it fall back to plain text.
                config_kwargs.pop("response_mime_type", None)
            except AttributeError:
                logger.warning("gemini_grounding_unavailable_in_sdk")

        # Retry transient 429 (rate-limit) / 503 (overload) with exponential
        # backoff + jitter. Free-tier Gemini is bursty; a single retry pass
        # recovers most spikes. Non-transient errors abstain immediately.
        _MAX_ATTEMPTS = 3
        _BASE_DELAY = 2.0
        response = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        client.models.generate_content,
                        model=self.model,
                        contents=prompt,
                        config=genai_types.GenerateContentConfig(**config_kwargs),
                    ),
                    timeout=timeout_s,
                )
                break
            except asyncio.TimeoutError:
                logger.warning("gemini_timeout", model=self.model, timeout_s=timeout_s)
                return ClassificationResult(
                    category_path=None, confidence=0.0, reason="llm_timeout",
                    used_search=False, input_tokens=0, output_tokens=0, cost_usd=0.0,
                )
            except Exception as exc:  # noqa: BLE001 — broad fallback by design
                transient = _is_transient(exc)
                if transient and attempt < _MAX_ATTEMPTS:
                    delay = _BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1)
                    logger.warning(
                        "gemini_call_retry",
                        attempt=attempt, delay_s=round(delay, 1),
                        error=str(exc)[:160],
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.warning(
                    "gemini_call_failed",
                    error=str(exc)[:200],
                    transient=transient,
                    attempts=attempt,
                )
                reason = "llm_rate_limited" if transient else f"llm_error:{type(exc).__name__}"
                return ClassificationResult(
                    category_path=None, confidence=0.0, reason=reason,
                    used_search=False, input_tokens=0, output_tokens=0, cost_usd=0.0,
                )
        if response is None:  # defensive — loop should always return or break
            return ClassificationResult(
                category_path=None, confidence=0.0, reason="llm_no_response",
                used_search=False, input_tokens=0, output_tokens=0, cost_usd=0.0,
            )

        text = getattr(response, "text", "") or ""
        path, confidence, reason, used_search = _parse_classification(text)

        usage = getattr(response, "usage_metadata", None)
        in_tokens = getattr(usage, "prompt_token_count", 0) or 0
        out_tokens = getattr(usage, "candidates_token_count", 0) or 0
        cost = _estimate_cost(self.model, in_tokens, out_tokens)

        # When the parser couldn't extract a category, log a snippet of the
        # raw Gemini output so we can diagnose abstain vs. parse-error vs.
        # tool-call-only responses (grounding sometimes returns search
        # function-call rounds with no final JSON).
        if path is None:
            logger.info(
                "gemini_classification_abstained",
                model=self.model,
                grounding=use_grounding,
                raw_preview=text[:400] if text else "(empty)",
                in_tokens=in_tokens,
                out_tokens=out_tokens,
            )

        return ClassificationResult(
            category_path=path,
            confidence=confidence,
            reason=reason or ("grounded" if used_search else ""),
            used_search=used_search,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cost_usd=cost,
        )
