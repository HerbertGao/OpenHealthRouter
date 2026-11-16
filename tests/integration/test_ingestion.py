"""任务 5.2–5.3、6.1–6.5：订阅、字段保真、去重、跳过分类、失败语义与日志。"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.openhealthrouter.mapping import (
    REASON_EVENT_INVALID,
    REASON_STATE_UNAVAILABLE,
    REASON_STATE_UNKNOWN,
    REASON_STORE_ERROR,
    REASON_TIME_NOT_PARSEABLE,
    REASON_TIME_UNAVAILABLE,
    REASON_UNIT_NOT_ALLOWED,
    REASON_VALUE_NOT_NUMERIC,
)
from custom_components.openhealthrouter.store import EventStore, StoreError

from .conftest import FAT_ENTITY, WEIGHT_ENTITY


async def _change(hass: HomeAssistant, set_state: Any, **kwargs: Any) -> None:
    set_state(**kwargs)
    await hass.async_block_till_done()


async def test_valid_change_writes_one_faithful_event(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
    register_platform: Any,
    read_events: Any,
) -> None:
    register_platform(WEIGHT_ENTITY, "synthetic_scale")
    entry = await setup_entry([mapping_factory()])
    await _change(hass, set_state)

    events = read_events(entry)
    assert len(events) == 1
    event = events[0]
    assert event.metric == "body.weight"
    assert event.value == pytest.approx(72.4)
    assert event.unit == "kg"
    assert event.subject_ref == "subject_local_1"
    assert event.device_ref == "device_local_1"
    assert event.source.kind == "ha_entity"
    assert event.source.entity_id == WEIGHT_ENTITY
    assert event.source.integration == "synthetic_scale"
    assert entry.runtime_data.state.ingested_total == 1


async def test_two_entities_do_not_replace_each_other(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
    read_events: Any,
) -> None:
    fat_mapping = mapping_factory(
        mapping_id=f"{FAT_ENTITY}::body.fat_percentage",
        source_entity=FAT_ENTITY,
        metric="body.fat_percentage",
        unit="%",
        provenance_type="derived.vendor",
        quality_issues=["estimated"],
    )
    entry = await setup_entry([mapping_factory(), fat_mapping])

    await _change(hass, set_state, force_update=True)
    await _change(
        hass, set_state, entity_id=FAT_ENTITY, value="18.5", unit_of_measurement="%"
    )

    assert len(read_events(entry)) == 2


async def test_unconfigured_entity_is_ignored(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
    read_events: Any,
) -> None:
    entry = await setup_entry([mapping_factory()])
    await _change(hass, set_state, entity_id="sensor.unrelated_weight")
    assert read_events(entry) == []
    assert entry.runtime_data.state.ingested_total == 0


@pytest.mark.parametrize(
    ("value", "attributes", "expected"),
    [
        ("unknown", {}, REASON_STATE_UNKNOWN),
        ("unavailable", {}, REASON_STATE_UNAVAILABLE),
        ("heavy", {}, REASON_VALUE_NOT_NUMERIC),
        ("72.4", {"unit_of_measurement": "lb"}, REASON_UNIT_NOT_ALLOWED),
        ("72.4", {"measured_at": ""}, REASON_TIME_UNAVAILABLE),
        ("72.4", {"measured_at": "not-a-time"}, REASON_TIME_NOT_PARSEABLE),
    ],
)
async def test_skip_reasons_are_classified(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
    read_events: Any,
    value: str,
    attributes: dict[str, Any],
    expected: str,
) -> None:
    entry = await setup_entry([mapping_factory()])
    await _change(hass, set_state, value=value, **attributes)

    assert read_events(entry) == []
    assert entry.runtime_data.state.last_skip_reason == expected
    assert entry.runtime_data.state.ingested_total == 0


async def test_contract_rejection_records_event_invalid(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
    read_events: Any,
) -> None:
    # 映射声明 derived.vendor 但没带 estimated：保存能过（直接写 options），契约会拒绝
    entry = await setup_entry([mapping_factory(provenance_type="derived.vendor")])
    await _change(hass, set_state)

    assert read_events(entry) == []
    assert entry.runtime_data.state.last_skip_reason == REASON_EVENT_INVALID


async def test_duplicate_change_does_not_add_rows(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
    read_events: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[Any]] = []
    original = EventStore.insert_events

    def spy(self: EventStore, events: list[Any]) -> Any:
        calls.append(list(events))
        return original(self, events)

    monkeypatch.setattr(EventStore, "insert_events", spy)

    entry = await setup_entry([mapping_factory()])
    await _change(hass, set_state, force_update=True)
    await _change(hass, set_state, force_update=True)

    assert len(calls) == 2, "两次回调都应被处理"
    assert len(read_events(entry)) == 1
    assert entry.runtime_data.state.ingested_total == 1
    assert entry.runtime_data.state.duplicates_total == 1


async def test_fallback_time_makes_forced_update_distinct(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
    read_events: Any,
) -> None:
    mapping = mapping_factory(
        observed_at_attribute=None, fallback_time="state.last_changed"
    )
    entry = await setup_entry([mapping])

    await _change(hass, set_state, force_update=True)
    await _change(hass, set_state, force_update=True)

    events = read_events(entry)
    assert len(events) == 2, "回退时间会随 last_changed 变化，身份因此不同"
    assert len({event.event_id for event in events}) == 2


async def test_own_entities_are_never_ingested(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
    read_events: Any,
    register_platform: Any,
) -> None:
    own_entity = "sensor.openhealthrouter_ingested_events"
    # 守卫生效靠实体注册表里的平台名，不靠实体 ID 命名巧合
    register_platform(own_entity, "openhealthrouter")
    mapping = mapping_factory(
        mapping_id=f"{own_entity}::body.weight",
        source_entity=own_entity,
    )
    entry = await setup_entry([mapping])

    await _change(hass, set_state, entity_id=own_entity, value="5")
    assert read_events(entry) == []


async def test_startup_does_not_backfill_history(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
    read_events: Any,
) -> None:
    set_state()  # 启动前就已存在状态（相当于历史）
    entry = await setup_entry([mapping_factory()])
    assert read_events(entry) == []

    await _change(hass, set_state, value="73.1")
    assert len(read_events(entry)) == 1, "启动后一次真实变化必须能入库"


async def test_store_failure_does_not_mark_success(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
    read_events: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = await setup_entry([mapping_factory()])

    def boom(self: EventStore, events: list[Any]) -> Any:
        raise StoreError("injected failure")

    monkeypatch.setattr(EventStore, "insert_events", boom)
    await _change(hass, set_state, value="72.4", force_update=True)

    assert entry.runtime_data.state.ingested_total == 0
    assert entry.runtime_data.state.last_ingested_at is None
    assert entry.runtime_data.state.last_skip_reason == REASON_STORE_ERROR
    assert read_events(entry) == []

    monkeypatch.undo()
    await _change(hass, set_state, value="73.2", force_update=True)
    assert entry.runtime_data.state.ingested_total == 1
    assert len(read_events(entry)) == 1


async def test_logs_carry_ids_and_reasons_but_no_health_values(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = await setup_entry([mapping_factory()])
    with caplog.at_level(logging.DEBUG, logger="custom_components.openhealthrouter"):
        await _change(hass, set_state, value="heavy")

    text = caplog.text
    assert WEIGHT_ENTITY in text
    assert REASON_VALUE_NOT_NUMERIC in text
    assert "72.4" not in text
    assert "heavy" not in text
    assert entry.runtime_data.state.ingested_total == 0


@pytest.mark.parametrize(
    ("mapping_overrides", "attributes", "expected"),
    [
        ({"value_attribute": "weight_kg"}, {"weight_kg": ""}, "value_missing"),
        ({"unit_attribute": "reported_unit"}, {}, "unit_missing"),
        (
            {"unit_attribute": "reported_unit"},
            {"reported_unit": "lb"},
            "unit_not_allowed",
        ),
    ],
)
async def test_attribute_driven_skip_reasons(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
    read_events: Any,
    mapping_overrides: dict[str, Any],
    attributes: dict[str, Any],
    expected: str,
) -> None:
    entry = await setup_entry([mapping_factory(**mapping_overrides)])
    await _change(hass, set_state, **attributes)

    assert read_events(entry) == []
    assert entry.runtime_data.state.last_skip_reason == expected


async def test_ingestion_makes_no_outbound_connection(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
    read_events: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认配置不得有任何出站连接：任何 connect 都会让本用例失败。"""
    import socket

    def deny(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("摄入不应发起外部网络连接")

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny, raising=False)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)

    entry = await setup_entry([mapping_factory()])
    await _change(hass, set_state)

    assert len(read_events(entry)) == 1


async def test_same_value_with_different_measured_at_writes_two_events(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
    read_events: Any,
) -> None:
    """观察时间参与身份：同值不同时刻必须各自入库（去重不能过度）。"""
    entry = await setup_entry([mapping_factory()])

    await _change(hass, set_state, force_update=True)
    await _change(
        hass,
        set_state,
        force_update=True,
        measured_at="2026-11-12T09:00:00+08:00",
    )

    events = read_events(entry)
    assert len(events) == 2
    assert len({event.event_id for event in events}) == 2
