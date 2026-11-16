"""任务 7.1–7.4：异步门面等价性、不阻塞事件循环、读不被写阻塞、日志纪律。"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import cast

import pytest

from custom_components.openhealthrouter.async_store import AsyncEventStore
from custom_components.openhealthrouter.store import (
    EventStore,
    QueryFilters,
    StoreError,
)


class GatedStore(EventStore):
    """写路径可被人为挂住的存储，用于构造确定性的并发窗口。"""

    _gate: threading.Event | None = None
    _entered: threading.Event | None = None

    def set_gate(self, gate: threading.Event, entered: threading.Event) -> None:
        self._gate = gate
        self._entered = entered

    def insert_events(self, events):  # noqa: ANN001, ANN201 - 测试替身
        if self._gate is not None and self._entered is not None:
            self._entered.set()
            self._gate.wait(5)
        return super().insert_events(events)


class LockingQueryStore(AsyncEventStore):
    """变异实现：读也去抢同一把写锁（用于证明用例有判别力）。"""

    async def query(self, filters=None):  # noqa: ANN001, ANN201
        async with self._write_lock:
            return await super().query(filters)


def test_async_facade_matches_sync(tmp_path: Path, make_event) -> None:
    async def main() -> None:
        store = await AsyncEventStore.open(tmp_path / "events.db")
        try:
            event = make_event()
            result = await store.insert_events([event])
            assert result.created == 1

            events, total = await store.query(QueryFilters(metric="body.weight"))
            assert total == 1
            assert events[0].event_id == event.event_id

            stored = await store.get_event(event.event_id)
            assert stored is not None
            assert stored.superseded is False

            backup = await store.backup_to(tmp_path / "backup.db")
            EventStore.verify_integrity(backup)

            deleted = await store.delete_events([event.event_id])
            assert deleted.deleted == 1
            assert await store.get_event(event.event_id) is None
        finally:
            await store.close()

    asyncio.run(main())


def test_async_write_keeps_loop_schedulable(tmp_path: Path, make_event) -> None:
    gate = threading.Event()
    entered = threading.Event()

    async def main() -> float:
        sync_store = cast(GatedStore, GatedStore.open(tmp_path / "events.db"))
        sync_store.set_gate(gate, entered)
        store = AsyncEventStore(sync_store)
        loop = asyncio.get_running_loop()
        marker_ran: list[float] = []

        async def marker() -> None:
            marker_ran.append(loop.time())

        start = loop.time()
        task = asyncio.create_task(store.insert_events([make_event()]))
        while not entered.is_set():
            await asyncio.sleep(0)
        marker_task = asyncio.create_task(marker())
        await marker_task
        marker_delay = marker_ran[0] - start
        gate.set()
        await task
        await store.close()
        return marker_delay

    delay = asyncio.run(main())
    assert delay < 1.0


def test_direct_sync_call_blocks_loop(tmp_path: Path, make_event) -> None:
    """同一用例在同步直调下的表现：证明上一条断言有判别力。"""
    gate = threading.Event()
    entered = threading.Event()

    async def main() -> tuple[float, float]:
        sync_store = cast(GatedStore, GatedStore.open(tmp_path / "events.db"))
        sync_store.set_gate(gate, entered)
        loop = asyncio.get_running_loop()
        marker_ran: list[float] = []

        async def marker() -> None:
            marker_ran.append(loop.time())

        start = loop.time()
        task = asyncio.create_task(marker())
        timer = threading.Timer(0.3, gate.set)
        timer.start()
        sync_store.insert_events([make_event()])  # 阻塞事件循环
        blocked_for = loop.time() - start
        await task
        timer.cancel()
        sync_store.close()
        return marker_ran[0] - start, blocked_for

    marker_delay, blocked_for = asyncio.run(main())
    assert blocked_for >= 0.25
    assert marker_delay >= 0.25


def test_read_not_blocked_by_write(tmp_path: Path, make_event) -> None:
    gate = threading.Event()
    entered = threading.Event()

    async def main() -> None:
        sync_store = cast(GatedStore, GatedStore.open(tmp_path / "events.db"))
        seeded = make_event()
        sync_store.insert_events([seeded])
        sync_store.set_gate(gate, entered)
        store = AsyncEventStore(sync_store)

        writer = asyncio.create_task(store.insert_events([make_event(value=73.1)]))
        while not entered.is_set():
            await asyncio.sleep(0)
        events, total = await asyncio.wait_for(store.query(), timeout=2)
        assert total == 1
        assert events[0].event_id == seeded.event_id
        gate.set()
        await writer
        await store.close()

    asyncio.run(main())


def test_mutation_that_locks_reads_is_detected(tmp_path: Path, make_event) -> None:
    """变异自检：读路径也抢写锁时，外层 wait_for 必须超时。"""
    gate = threading.Event()
    entered = threading.Event()

    async def main() -> None:
        sync_store = cast(GatedStore, GatedStore.open(tmp_path / "events.db"))
        sync_store.insert_events([make_event()])
        sync_store.set_gate(gate, entered)
        store = LockingQueryStore(sync_store)

        writer = asyncio.create_task(store.insert_events([make_event(value=73.1)]))
        while not entered.is_set():
            await asyncio.sleep(0)
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(store.query(), timeout=0.5)
        finally:
            gate.set()
            await writer
            await store.close()

    asyncio.run(main())


def test_failure_log_has_diagnosis_without_health_values(
    tmp_path: Path, make_event, caplog: pytest.LogCaptureFixture
) -> None:
    store = EventStore.open(tmp_path / "events.db")
    try:
        ok = make_event()
        store.insert_events([ok])
        bad = make_event(
            value=91.7,
            provenance={"type": "derived.user"},
            correctionOf="evt_" + "9" * 64,
        )
        logger_name = "custom_components.openhealthrouter"
        with caplog.at_level(logging.ERROR, logger=logger_name):
            with pytest.raises(StoreError):
                store.insert_events([bad])
        events, total = store.query(QueryFilters(include_superseded=True))
        assert total == 1
        assert [event.event_id for event in events] == [ok.event_id]
        text = caplog.text
        assert bad.event_id in text
        assert "IntegrityError" in text or "StoreError" in text
        assert "91.7" not in text
        assert "payload_json" not in text
    finally:
        store.close()


def test_time_key_format_is_single_shape() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from custom_components.openhealthrouter.store import time_key

    moment = datetime(2026, 11, 12, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert time_key(moment) == "2026-11-11T16:00:00.000000Z"
    assert len(time_key(moment)) == len("2026-11-11T16:00:00.000000Z")
    assert time.perf_counter() > 0


async def test_executor_is_used_for_every_operation(tmp_path: Path, make_event) -> None:
    """注释：注入的执行器必须承担 open/写/读/删/备份/关库。"""
    import asyncio

    from custom_components.openhealthrouter.async_store import AsyncEventStore

    seen: list[str] = []

    async def executor(func, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        seen.append(getattr(func, "__name__", type(func).__name__))
        return await asyncio.to_thread(func, *args, **kwargs)

    store = await AsyncEventStore.open(tmp_path / "events.db", executor=executor)
    try:
        event = make_event()
        await store.insert_events([event])
        await store.query()
        await store.get_event(event.event_id)
        await store.backup_to(tmp_path / "backup.db")
        await store.delete_events([event.event_id])
    finally:
        await store.close()

    for expected in (
        "open",
        "insert_events",
        "query",
        "get_event",
        "backup_to",
        "delete_events",
        "close",
    ):
        assert expected in seen, (expected, seen)
