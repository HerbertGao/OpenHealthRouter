"""任务 7.1–7.2：接入状态投影与隐私约束。"""

from __future__ import annotations

import json
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.openhealthrouter.const import (
    DOMAIN,
)

_TOTAL = "sensor.openhealthrouter_ingested_events"
_LAST = "sensor.openhealthrouter_last_ingested"
_SKIP = "sensor.openhealthrouter_last_skip_reason"


async def _change(hass: HomeAssistant, set_state: Any, **kwargs: Any) -> None:
    set_state(**kwargs)
    await hass.async_block_till_done()


def _state(hass: HomeAssistant, entity_id: str) -> Any:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state


def _serialised(hass: HomeAssistant) -> str:
    """把三个投影实体的状态、属性、entity_id 与 unique_id 全量序列化。"""
    payload: list[dict[str, Any]] = []
    for entity_id in (_TOTAL, _LAST, _SKIP):
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        payload.append(
            {
                "entity_id": entity_id,
                "state": state.state,
                "attributes": dict(state.attributes),
            }
        )
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for entity_id in (_TOTAL, _LAST, _SKIP):
        registry_entry = registry.async_get(entity_id)
        assert registry_entry is not None, entity_id
        payload.append(
            {
                "entity_id": entity_id,
                "unique_id": registry_entry.unique_id,
                "original_name": registry_entry.original_name,
            }
        )
    return json.dumps(payload, ensure_ascii=False)


async def test_initial_state_is_neutral(
    hass: HomeAssistant, setup_entry: Any, mapping_factory: Any
) -> None:
    await setup_entry([mapping_factory()])
    assert _state(hass, _TOTAL).state == "0"
    assert _state(hass, _LAST).state == "unknown"
    assert _state(hass, _SKIP).state == "unknown"
    # HA 自身会附加 friendly_name，其余键必须是白名单之外的空集
    assert set(_state(hass, _SKIP).attributes) - {"friendly_name"} == set()


async def test_ingest_updates_count_and_time(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
) -> None:
    await setup_entry([mapping_factory()])
    await _change(hass, set_state)

    assert _state(hass, _TOTAL).state == "1"
    assert _state(hass, _LAST).state != "unknown"
    assert _state(hass, _SKIP).state == "unknown"


async def test_skip_updates_reason_without_count(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
) -> None:
    await setup_entry([mapping_factory()])
    await _change(hass, set_state, value="heavy")

    assert _state(hass, _TOTAL).state == "0"
    assert _state(hass, _SKIP).state == "value_not_numeric"
    attributes = _state(hass, _SKIP).attributes
    assert attributes["reason"] == "value_not_numeric"
    assert set(attributes) - {"friendly_name", "reason"} == set()


async def test_projection_never_exposes_health_content(
    hass: HomeAssistant,
    setup_entry: Any,
    mapping_factory: Any,
    set_state: Any,
) -> None:
    await setup_entry([mapping_factory()])
    await _change(hass, set_state)

    text = _serialised(hass)
    for forbidden in (
        "72.4",
        "kg",
        "body.weight",
        "measured_at",
        "unit_of_measurement",
    ):
        assert forbidden not in text, f"投影泄露了 {forbidden!r}"
    assert DOMAIN in text  # 只暴露集成标识本身
