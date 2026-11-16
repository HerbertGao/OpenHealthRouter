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
    assert len(entry.runtime_data.unsubs) == 1
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
