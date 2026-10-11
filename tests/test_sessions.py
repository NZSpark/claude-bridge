"""会话分桶、桶锁与状态持久化（doc/design.md 5、11.1）。"""

from __future__ import annotations

import asyncio
import json

import pytest

from claude_web.errors import SessionBusyError
from claude_web.session.lock import BucketLockManager
from claude_web.session.schema import SessionState, empty_state_document
from claude_web.session.store import SessionStore


def test_same_bucket_runs_serially():
    async def scenario():
        mgr = BucketLockManager(timeout_s=2)
        order = []

        async def worker(tag, delay):
            async with mgr.acquire("bucket-a"):
                order.append(f"start:{tag}")
                await asyncio.sleep(delay)
                order.append(f"end:{tag}")

        await asyncio.gather(worker("first", 0.05), worker("second", 0.0))
        return order

    order = asyncio.run(scenario())
    assert order == ["start:first", "end:first", "start:second", "end:second"]


def test_different_buckets_run_parallel():
    async def scenario():
        mgr = BucketLockManager(timeout_s=2)
        events = []

        async def worker(tag, delay):
            async with mgr.acquire(tag):
                events.append(f"start:{tag}")
                await asyncio.sleep(delay)
                events.append(f"end:{tag}")

        await asyncio.gather(worker("a", 0.05), worker("b", 0.05))
        return events

    events = asyncio.run(scenario())
    # 两个桶都先 start，说明确实并行而非串行。
    starts = [e for e in events if e.startswith("start")]
    assert len(starts) == 2
    assert events.index("start:b") < events.index("end:a")


def test_bucket_lock_times_out_with_busy_error():
    async def scenario():
        mgr = BucketLockManager(timeout_s=0.05)

        async def holder():
            async with mgr.acquire("busy"):
                await asyncio.sleep(0.2)

        task = asyncio.create_task(holder())
        await asyncio.sleep(0.01)
        with pytest.raises(SessionBusyError) as exc:
            async with mgr.acquire("busy"):
                pass
        await task
        return exc.value

    err = asyncio.run(scenario())
    assert err.code.value == "session_busy"
    assert err.http_status == 503


def test_bucket_lru_does_not_evict_active_bucket():
    async def scenario():
        mgr = BucketLockManager(max_buckets=2, timeout_s=1)
        async with mgr.acquire("active"):
            # 在持有 active 的情况下多开两个桶，active 不能被回收。
            async with mgr.acquire("other"):
                pass
            async with mgr.acquire("third"):
                pass
            return "active" in mgr.active_buckets()

    assert asyncio.run(scenario()) is True


def test_store_roundtrip_and_reset(tmp_path):
    store = SessionStore(tmp_path / "state.json")
    state = store.state("b1")
    assert state.turns == 0
    state.turns = 3
    state.has_history = True
    state.linked_url = "https://claude.ai/chat/abc"
    store.save_state("b1", state)

    reloaded = SessionStore(tmp_path / "state.json").state("b1")
    assert reloaded.turns == 3
    assert reloaded.has_history is True
    assert reloaded.linked_url == "https://claude.ai/chat/abc"

    store.reset("b1")
    assert SessionStore(tmp_path / "state.json").state("b1").turns == 0


def test_store_ignores_corrupted_file(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    store = SessionStore(path)
    assert store.state("b").turns == 0


def test_store_migrates_legacy_document(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"version": 0, "sessions": {"old": {"turns": 5}}}),
        encoding="utf-8",
    )
    store = SessionStore(path)
    assert store.state("old").turns == 5
    # 迁移后落盘为当前 schema。
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema_version"] == 1


def test_empty_document_shape():
    assert empty_state_document() == {"schema_version": 1, "sessions": {}}


def test_session_state_from_payload_is_defensive():
    state = SessionState.from_payload({"turns": "5", "has_history": 1, "linked_url": "  "})
    assert state.turns == 5
    assert state.has_history is True
    assert state.linked_url is None
