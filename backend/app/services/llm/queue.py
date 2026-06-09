"""Single-worker, rate-paced queue for L2 LLM classification.

Why a queue instead of fire-and-forget tasks
---------------------------------------------
The previous dispatch spawned one ``asyncio.create_task`` per transaction
(later bounded by a semaphore of 2). A bulk refresh-matching of 40 rows
still burst far above Gemini's free-tier ~15 RPM, tripping a cascade of
429 RESOURCE_EXHAUSTED (ERR-20260607-002 / ERR-20260609-001).

This module replaces that with ONE long-lived worker draining a FIFO
queue, sleeping ``llm_min_interval_sec`` (default 5s ≈ 12 RPM) between
calls so we stay just under the rate limit and never burst.

State is process-local (single-user, single-process app). Enqueue dedups
against ids already queued or in-flight so repeated refresh-matching
clicks don't double-process. Queue depth is exposed via ``status()`` so
the UI can show "AI 处理中 · 剩 N 笔".
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class _QueueState:
    queue: asyncio.Queue[int]
    pending_ids: set[int]
    in_flight: int = 0
    processed: int = 0
    worker: asyncio.Task | None = None


_state: _QueueState | None = None
# Injected at startup so the worker can open its own DB sessions and read
# the live pacing interval. Kept module-level to avoid threading it through
# every enqueue call site.
_session_factory = None  # type: ignore[var-annotated]


def _ensure_state() -> _QueueState:
    global _state
    if _state is None:
        _state = _QueueState(queue=asyncio.Queue(), pending_ids=set())
    return _state


def configure(session_factory) -> None:
    """Wire the async session factory (called once from lifespan)."""
    global _session_factory
    _session_factory = session_factory


async def _interval_seconds() -> float:
    """Live pacing interval from app_settings (so the user can tune it)."""
    if _session_factory is None:
        return 5.0
    try:
        from app.services.app_settings import get_llm_settings
        async with _session_factory() as db:
            s = await get_llm_settings(db)
        return max(0.0, s.min_interval_sec)
    except Exception:
        return 5.0


async def _process_one(tx_id: int) -> None:
    """Classify a single tx. Imported lazily to dodge a circular import
    (ingestion → queue → ingestion)."""
    from app.services.ingestion import _classify_one_with_llm
    await _classify_one_with_llm(tx_id)


async def _worker_loop() -> None:
    st = _ensure_state()
    logger.info("llm_queue_worker_started")
    while True:
        tx_id = await st.queue.get()
        st.pending_ids.discard(tx_id)
        st.in_flight += 1
        try:
            await _process_one(tx_id)
            st.processed += 1
        except Exception as exc:  # noqa: BLE001 — worker must never die
            logger.warning("llm_queue_item_failed", tx_id=tx_id, error=str(exc)[:160])
        finally:
            st.in_flight -= 1
            st.queue.task_done()
        # Pace AFTER each call so consecutive calls are spaced regardless of
        # how fast Gemini responds. Skips the sleep when the queue is empty
        # so a lone item isn't artificially delayed on the next enqueue.
        if not st.queue.empty():
            await asyncio.sleep(await _interval_seconds())


def start_worker() -> None:
    """Idempotently start the single worker on the running loop."""
    st = _ensure_state()
    if st.worker is not None and not st.worker.done():
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (sync/CLI/test enqueue). Items stay queued; the
        # worker starts on the next enqueue that happens inside the loop,
        # or at lifespan startup.
        logger.debug("llm_queue_worker_no_loop")
        return
    st.worker = asyncio.create_task(_worker_loop())


def enqueue(tx_ids: list[int]) -> int:
    """Add tx ids to the queue (deduped). Returns how many were newly added."""
    st = _ensure_state()
    added = 0
    for tx_id in tx_ids:
        if tx_id in st.pending_ids:
            continue
        st.pending_ids.add(tx_id)
        st.queue.put_nowait(tx_id)
        added += 1
    if added:
        start_worker()  # ensure a drainer exists
    return added


def status() -> dict[str, int]:
    """Queue snapshot for the UI: depth (waiting) + in_flight + processed."""
    st = _ensure_state()
    return {
        "depth": st.queue.qsize(),
        "in_flight": st.in_flight,
        "processed": st.processed,
        # Convenience: total outstanding work the UI should wait on.
        "outstanding": st.queue.qsize() + st.in_flight,
    }
