"""LLM provider Protocol + shared result types.

Today only `GeminiProvider` (in `gemini.py`) implements this. Adding
OpenAI / Anthropic later means dropping in another file that satisfies
the Protocol; the rest of the pipeline doesn't change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class LLMUnavailableError(RuntimeError):
    """LLM call could not be made (missing key, disabled, budget exceeded)."""


@dataclass(frozen=True)
class ClassificationResult:
    category_path: str | None  # "住家/房租"; None = abstain
    confidence: float          # 0..1
    reason: str
    used_search: bool
    input_tokens: int
    output_tokens: int
    cost_usd: float


class LLMProvider(Protocol):
    name: str
    model: str

    async def classify(
        self,
        prompt: str,
        *,
        use_grounding: bool,
        timeout_s: float = 15.0,
    ) -> ClassificationResult:
        """Run the prompt and return a structured classification verdict.

        Implementations must:
        - Honour `timeout_s` (raise/treat as abstain on overrun).
        - Never raise on bad model output — return an abstain result with
          `category_path=None` instead, so the caller falls back to inbox.
        - Populate token counts + cost_usd best-effort (zero is fine if
          the API doesn't expose them).
        """
        ...
