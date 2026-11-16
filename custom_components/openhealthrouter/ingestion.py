"""运行时：订阅、摄入与接入状态聚合。

设计依据：openspec/changes/ha-entity-ingestion/design.md D3/D4/D7/D8。
刻意不叫 coordinator：接入是事件驱动，不是轮询协调器。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event

from .async_store import AsyncEventStore
from .const import DOMAIN
from .mapping import (
    REASON_STORE_ERROR,
    EntityMapping,
    Skipped,
    StateView,
    map_state_to_event,
)
from .store import StoreError

_LOGGER = logging.getLogger(__name__)

# 这两个状态直接判定，交给纯函数前先短路，避免无意义的属性解析
_IDLE_STATES = {STATE_UNKNOWN, STATE_UNAVAILABLE}


def update_signal(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_update"


@dataclass(frozen=True)
class IngestionState:
    """接入状态快照：只含计数、时间与原因码。"""

    ingested_total: int = 0
    duplicates_total: int = 0
    last_ingested_at: datetime | None = None
    last_skip_reason: str | None = None


@dataclass
class RuntimeData:
    """条目运行时对象（放进 ConfigEntry.runtime_data）。"""

    store: AsyncEventStore
    state: IngestionState = field(default_factory=IngestionState)
    tasks: set[Any] = field(default_factory=set)
    unsubs: list[Callable[[], None]] = field(default_factory=list)
    closed: bool = False
    unload_ok: bool = False


def load_mappings(entry_options: dict[str, Any]) -> list[EntityMapping]:
    """从 options 读出映射；非法条目跳过并记录，不阻塞启动。"""
    mappings: list[EntityMapping] = []
    for raw in entry_options.get("mappings", []):
        try:
            mappings.append(EntityMapping.model_validate(raw))
        except ValueError:
            _LOGGER.error("忽略无法解析的映射配置")
    return mappings


def _integration_for(hass: HomeAssistant, entity_id: str) -> str:
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is not None and entry.platform:
        return entry.platform
    return entity_id.split(".", 1)[0]


def _state_view(hass: HomeAssistant, state: State) -> StateView:
    return StateView(
        entity_id=state.entity_id,
        state=state.state,
        attributes=dict(state.attributes),
        last_changed=state.last_changed,
        timezone_name=hass.config.time_zone,
        integration=_integration_for(hass, state.entity_id),
    )


async def async_ingest_state(
    hass: HomeAssistant,
    entry_id: str,
    runtime: RuntimeData,
    mapping: EntityMapping,
    state: State,
) -> None:
    """处理一次状态变化：映射 → 写库 → 更新接入状态。"""
    if runtime.closed:
        return
    result = map_state_to_event(mapping, _state_view(hass, state))
    if isinstance(result, Skipped):
        runtime.state = replace(runtime.state, last_skip_reason=result.reason)
        _LOGGER.debug(
            "跳过摄入 entity_id=%s mapping_id=%s reason=%s",
            state.entity_id,
            mapping.mapping_id,
            result.reason,
        )
        async_dispatcher_send(hass, update_signal(entry_id))
        return

    try:
        insert = await runtime.store.insert_events([result])
    except StoreError as exc:
        runtime.state = replace(runtime.state, last_skip_reason=REASON_STORE_ERROR)
        _LOGGER.error(
            "摄入写入失败 entity_id=%s mapping_id=%s reason=%s",
            state.entity_id,
            mapping.mapping_id,
            type(exc).__name__,
        )
        async_dispatcher_send(hass, update_signal(entry_id))
        return

    if insert.created:
        runtime.state = replace(
            runtime.state,
            ingested_total=runtime.state.ingested_total + insert.created,
            last_ingested_at=result.recorded_at,
            last_skip_reason=None,
        )
    elif insert.duplicates:
        runtime.state = replace(
            runtime.state,
            duplicates_total=runtime.state.duplicates_total + insert.duplicates,
        )
    async_dispatcher_send(hass, update_signal(entry_id))


def async_subscribe(
    hass: HomeAssistant, entry_id: str, runtime: RuntimeData
) -> list[Callable[[], None]]:
    """为每个启用的映射订阅其源实体；返回取消函数列表。"""
    unsubs: list[Callable[[], None]] = []
    for mapping in load_mappings_of(hass, entry_id):
        if not mapping.enabled:
            continue
        unsubs.append(_async_subscribe_one(hass, entry_id, runtime, mapping))
    return unsubs


def load_mappings_of(hass: HomeAssistant, entry_id: str) -> list[EntityMapping]:
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return []
    return load_mappings(dict(entry.options))


def _async_subscribe_one(
    hass: HomeAssistant,
    entry_id: str,
    runtime: RuntimeData,
    mapping: EntityMapping,
) -> Callable[[], None]:
    @callback
    def _handle(event: Event[EventStateChangedData]) -> None:
        if runtime.closed:
            return
        new_state: State | None = event.data.get("new_state")
        if new_state is None:
            return
        if _integration_for(hass, new_state.entity_id) == DOMAIN:
            return
        task = hass.async_create_task(
            _ingest(hass, entry_id, runtime, mapping, new_state),
            f"{DOMAIN} ingest {new_state.entity_id}",
        )
        runtime.tasks.add(task)
        task.add_done_callback(runtime.tasks.discard)

    async def _ingest(
        hass: HomeAssistant,
        entry_id: str,
        runtime: RuntimeData,
        mapping: EntityMapping,
        state: State,
    ) -> None:
        try:
            await async_ingest_state(hass, entry_id, runtime, mapping, state)
        except Exception as exc:  # noqa: BLE001 - 任何异常都不得逃逸到事件循环
            _LOGGER.error(
                "摄入异常 entity_id=%s mapping_id=%s reason=%s",
                state.entity_id,
                mapping.mapping_id,
                type(exc).__name__,
            )

    return async_track_state_change_event(hass, [mapping.source_entity], _handle)
