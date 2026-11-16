"""任务 8.1–8.3：卸载协议、在途写入与重载语义。"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

import custom_components.openhealthrouter as ohr
from custom_components.openhealthrouter.store import EventStore, QueryFilters


async def _change(hass: HomeAssistant, set_state: Any, **kwargs: Any) -> None:
    set_state(**kwargs)
    await hass.async_block_till_done()


@pytest.fixture
def insert_spy(monkeypatch: pytest.MonkeyPatch) -> list[list[Any]]:
    calls: list[list[Any]] = []
    original = EventStore.insert_events

    def spy(self: EventStore, events: list[Any]) -> Any:
        calls.append(list(events))
        return original(self, events)

    monkeypatch.setattr(EventStore, "insert_events", spy)
    return calls


@pytest.fixture
def close_spy(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    original = EventStore.close

    def spy(self: EventStore) -> None:
        calls.append(1)
        original(self)

    monkeypatch.setattr(EventStore, "close", spy)
    return calls


async def test_unload_stops_ingestion_with_positive_control(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
    insert_spy: list[list[Any]],
) -> None:
    entry = await setup_entry([mapping_factory()])
    runtime = entry.runtime_data

    await _change(hass, set_state, force_update=True)
    assert len(insert_spy) >= 1, "卸载前的正向对照：订阅确实生效"

    before = len(insert_spy)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    await _change(hass, set_state, value="75.0", force_update=True)
    assert len(insert_spy) == before, "卸载后不得再触碰存储"
    assert runtime.closed is True
    assert runtime.unsubs == []


async def test_unload_is_idempotent_and_closes_once(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    close_spy: list[int],
) -> None:
    entry = await setup_entry([mapping_factory()])

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert len(close_spy) == 1

    assert await ohr.async_unload_entry(hass, entry) is True
    assert len(close_spy) == 1, "重复卸载不得再次关库"


async def test_platform_unload_failure_leaves_entry_intact(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
    insert_spy: list[list[Any]],
    close_spy: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await setup_entry([mapping_factory()])

    async def fail(entry_arg: Any, platforms: Any) -> bool:
        return False

    # 独立 patch 对象：撤销它不会连带撤销 insert_spy 的 patch
    platform_patch = pytest.MonkeyPatch()
    platform_patch.setattr(hass.config_entries, "async_unload_platforms", fail)

    assert await ohr.async_unload_entry(hass, entry) is False
    assert close_spy == [], "平台卸载失败时不得关库"
    assert entry.runtime_data.closed is False
    assert entry.runtime_data.unsubs != [], "平台卸载失败时不得退订"

    platform_patch.undo()
    await _change(hass, set_state, force_update=True)
    assert len(insert_spy) >= 1, "条目仍应可用"


async def test_reload_keeps_history_and_keeps_ingesting(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
    read_events: Any,
) -> None:
    entry = await setup_entry([mapping_factory()])
    await _change(hass, set_state, force_update=True)
    rows_before = len(read_events(entry))
    assert rows_before == 1

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert len(read_events(entry)) == rows_before, "重载本身不写入事件"

    await _change(hass, set_state, value="74.5", force_update=True)
    assert len(read_events(entry)) == rows_before + 1


async def test_in_flight_write_finishes_before_close(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    original = EventStore.insert_events

    def gated(self: EventStore, events: list[Any]) -> Any:
        entered.set()
        release.wait(5)
        return original(self, events)

    entry = await setup_entry([mapping_factory()])
    db_path = entry.runtime_data.store.store.path
    monkeypatch.setattr(EventStore, "insert_events", gated)

    set_state(value="76.0", force_update=True)
    for _ in range(100):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set(), "写入钩子必须被进入过"

    unload_task = asyncio.create_task(ohr.async_unload_entry(hass, entry))
    await asyncio.sleep(0.05)
    release.set()

    assert await unload_task is True
    await hass.async_block_till_done()

    monkeypatch.undo()
    # 卸载后 runtime_data 已被 HA 清空：用同一路径重开库读回数据
    reopened = EventStore.open(db_path)
    try:
        events, _ = reopened.query(QueryFilters(limit=100, include_superseded=True))
    finally:
        reopened.close()
    assert [event.value for event in events] == [76.0]


async def test_cancelled_unload_still_closes_store(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
    close_spy: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    original = EventStore.insert_events

    def gated(self: EventStore, events: list[Any]) -> Any:
        entered.set()
        release.wait(5)
        return original(self, events)

    entry = await setup_entry([mapping_factory()])
    monkeypatch.setattr(EventStore, "insert_events", gated)

    set_state(value="77.0", force_update=True)
    for _ in range(100):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set()

    unload_task = asyncio.create_task(ohr.async_unload_entry(hass, entry))
    # 等平台卸载完成、进入排空等待后再取消，消除固定 sleep 的时序抖动
    for _ in range(200):
        if entry.runtime_data.closed:
            break
        await asyncio.sleep(0.005)
    assert entry.runtime_data.closed, "应已进入排空阶段"
    unload_task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await unload_task

    assert close_spy, "取消路径也必须关库"
    assert entry.runtime_data.unsubs == []
    await hass.async_block_till_done()
