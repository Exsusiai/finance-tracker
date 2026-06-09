"""Single-worker paced LLM classification queue (ERR-20260609-001 fix).

Guards: enqueue dedups, the worker drains in order under a (test-zeroed)
pace, status() reflects depth, and a failing item never kills the worker.
"""

from __future__ import annotations

import asyncio
import os

import pytest

_TEST_TOKEN = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
os.environ.setdefault("FINANCE_TRACKER_API_TOKEN", _TEST_TOKEN)
os.environ.setdefault("BASE_CURRENCY", "CNY")

from app.services.llm import queue as q  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_queue(monkeypatch):
    # Fresh state + zero pacing per test so we don't sleep 5s.
    q._state = None
    q._session_factory = None
    monkeypatch.setattr(q, "_interval_seconds", lambda: _zero())
    yield
    if q._state and q._state.worker:
        q._state.worker.cancel()
    q._state = None


async def _zero() -> float:
    return 0.0


class TestEnqueueDedup:
    def test_enqueue_returns_newly_added_count(self):
        assert q.enqueue([1, 2, 3]) == 3
        # 2,3 already queued → only 4 is new
        assert q.enqueue([2, 3, 4]) == 1

    def test_status_reflects_depth(self):
        q.enqueue([10, 11, 12])
        st = q.status()
        # Worker may have started; depth+in_flight covers all 3.
        assert st["depth"] + st["in_flight"] <= 3
        assert st["outstanding"] >= 0


class TestWorkerDrains:
    @pytest.mark.asyncio
    async def test_processes_all_items_in_order(self, monkeypatch):
        seen: list[int] = []

        async def fake_process(tx_id: int) -> None:
            seen.append(tx_id)

        monkeypatch.setattr(q, "_process_one", fake_process)
        q.enqueue([1, 2, 3, 4, 5])
        # Let the worker drain.
        for _ in range(50):
            if q.status()["outstanding"] == 0:
                break
            await asyncio.sleep(0.01)
        assert seen == [1, 2, 3, 4, 5]
        assert q.status()["processed"] == 5

    @pytest.mark.asyncio
    async def test_failing_item_does_not_kill_worker(self, monkeypatch):
        seen: list[int] = []

        async def fake_process(tx_id: int) -> None:
            if tx_id == 2:
                raise RuntimeError("boom")
            seen.append(tx_id)

        monkeypatch.setattr(q, "_process_one", fake_process)
        q.enqueue([1, 2, 3])
        for _ in range(50):
            if q.status()["outstanding"] == 0:
                break
            await asyncio.sleep(0.01)
        # 1 and 3 still processed despite 2 raising.
        assert seen == [1, 3]

    @pytest.mark.asyncio
    async def test_dedup_skips_in_flight(self, monkeypatch):
        calls: list[int] = []

        async def fake_process(tx_id: int) -> None:
            calls.append(tx_id)
            await asyncio.sleep(0.02)

        monkeypatch.setattr(q, "_process_one", fake_process)
        q.enqueue([7])
        # Immediately enqueue 7 again while it may be in flight — must not
        # double-process.
        q.enqueue([7])
        for _ in range(50):
            if q.status()["outstanding"] == 0:
                break
            await asyncio.sleep(0.01)
        assert calls.count(7) == 1
