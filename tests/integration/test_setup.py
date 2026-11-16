"""任务 5.1：setup 生命周期与存储不可用时的重试语义。"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.openhealthrouter as ohr
from custom_components.openhealthrouter.async_store import AsyncEventStore
from custom_components.openhealthrouter.const import DOMAIN


async def test_setup_exposes_runtime_data(
    hass: HomeAssistant, setup_entry: Any, mapping_factory: Any
) -> None:
    entry = await setup_entry([mapping_factory()])
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.store.is_available
    assert entry.runtime_data.listeners == 1
    assert entry.runtime_data.closed is False


async def test_unavailable_store_defers_setup(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """存储不可用必须走 ConfigEntryNotReady（可重试），而不是永久失败。"""

    class _DeadStore:
        is_available = False

        class store:  # noqa: N801 - 仅伪装同步层
            @staticmethod
            def close() -> None:
                return None

    async def dead_open(*args: Any, **kwargs: Any) -> Any:
        return _DeadStore()

    monkeypatch.setattr(AsyncEventStore, "open", dead_open)

    entry = MockConfigEntry(
        domain=DOMAIN, data={}, options={"mappings": []}, unique_id=DOMAIN
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id) is False
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert ohr.DRAIN_TIMEOUT_S > 0  # 模块已加载，避免未使用告警


async def test_setup_failure_rolls_back_subscription(
    hass: HomeAssistant,
    mapping_factory: Any,
    set_state: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """setup 抛错必须回滚订阅：否则 HA 重试后会处理同一变化两次。"""
    from custom_components.openhealthrouter.store import EventStore

    set_state()
    calls: list[list[Any]] = []
    original_insert = EventStore.insert_events

    def spy(self: EventStore, events: list[Any]) -> Any:
        calls.append(list(events))
        return original_insert(self, events)

    monkeypatch.setattr(EventStore, "insert_events", spy)

    import custom_components.openhealthrouter.ingestion as ing

    from .conftest import WEIGHT_ENTITY

    cancelled: list[str] = []

    def fake_track(hass_arg: Any, entity_ids: Any, callback: Any) -> Any:
        def unsub() -> None:
            cancelled.append(entity_ids[0])

        return unsub

    # 只打一次补丁：同一属性双补丁时撤销顺序会把假实现留在模块上，污染后续用例
    monkeypatch.setattr(ing, "async_track_state_change_event", fake_track)

    forward_patch = pytest.MonkeyPatch()

    async def boom(entry_arg: Any, platforms: Any) -> None:
        raise RuntimeError("forward failed")

    forward_patch.setattr(hass.config_entries, "async_forward_entry_setups", boom)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"mappings": [mapping_factory()]},
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)
    # HA 自行捕获 setup 异常并置错误状态，不会向调用方抛出
    assert await hass.config_entries.async_setup(entry.entry_id) is False
    assert entry.state in (
        ConfigEntryState.SETUP_ERROR,
        ConfigEntryState.SETUP_RETRY,
    )

    forward_patch.undo()
    assert entry.runtime_data is None, "回滚后不得残留运行时对象"
    # 直接见证退订：残余监听会挡住后续写入，光看 calls 是空转的
    assert cancelled == [WEIGHT_ENTITY], f"订阅未被撤销: {cancelled}"
    assert calls == []


async def test_partial_subscribe_failure_rolls_back_earlier_subscriptions(
    hass: HomeAssistant,
    mapping_factory: Any,
    set_state: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第 2 条映射订阅失败时，第 1 条必须已被撤掉（否则重试会重复订阅）。"""
    import custom_components.openhealthrouter.ingestion as ing

    from .conftest import FAT_ENTITY, WEIGHT_ENTITY

    set_state()
    weight = mapping_factory()
    fat = mapping_factory(
        mapping_id=f"{FAT_ENTITY}::body.fat_percentage",
        source_entity=FAT_ENTITY,
        metric="body.fat_percentage",
        unit="%",
        provenance_type="derived.vendor",
        quality_issues=["estimated"],
    )

    cancelled: list[str] = []
    attempts: list[int] = []

    def fake_track(hass_arg: Any, entity_ids: Any, callback: Any) -> Any:
        attempts.append(len(attempts) + 1)
        if len(attempts) == 2:
            raise RuntimeError("subscribe failed")
        entity = entity_ids[0]

        def unsub() -> None:
            cancelled.append(entity)

        return unsub

    monkeypatch.setattr(ing, "async_track_state_change_event", fake_track)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={"mappings": [weight, fat]},
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id) is False
    assert cancelled == [WEIGHT_ENTITY], f"第一条订阅未被撤销: {cancelled}"
    assert entry.runtime_data is None
