"""集成测试夹具：合成实体、临时数据库与条目装配。

数据全为合成值（见 tests/fixtures/synthetic/README.md）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.openhealthrouter.const import DOMAIN
from custom_components.openhealthrouter.store import EventStore, QueryFilters

WEIGHT_ENTITY = "sensor.synthetic_scale_weight"
FAT_ENTITY = "sensor.synthetic_scale_fat"
LOGICAL_TIME = "2026-11-12T08:00:00+08:00"


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations: Any) -> None:
    """让 PHACC 加载本仓的 custom_components。"""


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """每个测试独立的数据库目录。

    PHACC 的 config_dir 在测试之间是共享的，不隔离会让上一个测试的事件残留下来。
    DB_DIR 用绝对路径，`hass.config.path` 会直接采用它。
    """
    import custom_components.openhealthrouter as ohr

    monkeypatch.setattr(ohr, "DB_DIR", str(tmp_path / "ohr_store"))


@pytest.fixture
async def hass_shanghai(hass: HomeAssistant) -> HomeAssistant:
    hass.config.time_zone = "Asia/Shanghai"
    return hass


@pytest.fixture
def mapping_factory() -> Callable[..., dict[str, Any]]:
    def factory(**overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "mapping_id": f"{WEIGHT_ENTITY}::body.weight",
            "source_entity": WEIGHT_ENTITY,
            "subject_ref": "subject_local_1",
            "metric": "body.weight",
            "unit": "kg",
            "device_ref": "device_local_1",
            "observed_at_attribute": "measured_at",
            "provenance_type": "observation",
            "quality_issues": [],
            "enabled": True,
        }
        payload.update(overrides)
        return payload

    return factory


@pytest.fixture
def register_platform(hass: HomeAssistant) -> Callable[[str, str], None]:
    """把实体登记到注册表，使 source.integration 能反映来源平台。"""

    def register(entity_id: str, platform: str) -> None:
        registry = er.async_get(hass)
        domain, _, object_id = entity_id.partition(".")
        registry.async_get_or_create(
            domain,
            platform,
            f"{platform}_{object_id}",
            suggested_object_id=object_id,
        )

    return register


@pytest.fixture
def set_state(hass: HomeAssistant) -> Callable[..., None]:
    def apply(
        entity_id: str = WEIGHT_ENTITY,
        value: str = "72.4",
        *,
        force_update: bool = False,
        **attributes: Any,
    ) -> None:
        attrs: dict[str, Any] = {
            "unit_of_measurement": "kg",
            "measured_at": LOGICAL_TIME,
        }
        attrs.update(attributes)
        hass.states.async_set(entity_id, value, attrs, force_update=force_update)

    return apply


@pytest.fixture
def setup_entry(
    hass: HomeAssistant,
) -> Callable[..., Any]:
    async def _setup(mappings: list[dict[str, Any]], entry_id: str = "ohr_test"):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={},
            options={"mappings": mappings},
            entry_id=entry_id,
            unique_id=DOMAIN,
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return entry

    return _setup


@pytest.fixture
def read_events(hass: HomeAssistant) -> Callable[[Any], list[Any]]:
    """直接从条目运行时里的存储读事件（每操作独立连接，测试线程可直接调用）。"""

    def read(entry: Any, *, include_superseded: bool = True) -> list[Any]:
        store: EventStore = entry.runtime_data.store.store
        filters = QueryFilters(limit=1000, include_superseded=include_superseded)
        events, _ = store.query(filters)
        return events

    return read
