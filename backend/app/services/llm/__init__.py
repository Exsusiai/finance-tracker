"""LLM-fallback classification.

The L2 layer kicks in when L1 keyword matching either misses or hits a
rule marked `requires_llm=True` (e.g. PayPal where the same keyword needs
amount-conditional resolution).

Public entry points:
    classify_with_llm(db, tx)  — orchestrates: load knowledge base,
        build prompt, call provider, parse, write tx fields.
    get_provider(settings)     — factory; only Gemini today.
"""
from __future__ import annotations

from app.services.llm.provider import ClassificationResult, LLMProvider, LLMUnavailableError

__all__ = ["ClassificationResult", "LLMProvider", "LLMUnavailableError"]
