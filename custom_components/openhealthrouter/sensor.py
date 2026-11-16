"""接入状态投影：三个固定传感器。

设计依据：openspec/changes/ha-entity-ingestion/design.md D7。
受约束的是"不泄露健康内容"：entity_id、unique_id、名称与属性都不含指标、测量者或数值。
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    SENSOR_INGESTED_TOTAL,
    SENSOR_LAST_INGESTED,
    SENSOR_LAST_SKIP_REASON,
)
from .ingestion import RuntimeData, update_signal


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: RuntimeData = entry.runtime_data
    async_add_entities(
        [
            _IngestedTotalSensor(entry, runtime),
            _LastIngestedSensor(entry, runtime),
            _LastSkipReasonSensor(entry, runtime),
        ]
    )


class _BaseSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, runtime: RuntimeData, key: str) -> None:
        self._entry = entry
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="OpenHealthRouter",
            manufacturer="OpenHealthRouter",
            model="Local health event router",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                update_signal(self._entry.entry_id),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


class _IngestedTotalSensor(_BaseSensor):
    _attr_name = "Ingested events"

    def __init__(self, entry: ConfigEntry, runtime: RuntimeData) -> None:
        super().__init__(entry, runtime, SENSOR_INGESTED_TOTAL)

    @property
    def native_value(self) -> int:
        return self._runtime.state.ingested_total


class _LastIngestedSensor(_BaseSensor):
    _attr_name = "Last ingested"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: ConfigEntry, runtime: RuntimeData) -> None:
        super().__init__(entry, runtime, SENSOR_LAST_INGESTED)

    @property
    def native_value(self) -> Any:
        return self._runtime.state.last_ingested_at


class _LastSkipReasonSensor(_BaseSensor):
    _attr_name = "Last skip reason"

    def __init__(self, entry: ConfigEntry, runtime: RuntimeData) -> None:
        super().__init__(entry, runtime, SENSOR_LAST_SKIP_REASON)

    @property
    def native_value(self) -> str | None:
        return self._runtime.state.last_skip_reason

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # 白名单：只有原因码，没有源属性、数值或单位
        reason = self._runtime.state.last_skip_reason
        return {} if reason is None else {"reason": reason}
