"""集成入口：setup 与 unload。

设计依据：openspec/changes/ha-entity-ingestion/design.md D5/D8/D9/D11。
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .async_store import AsyncEventStore
from .const import DB_DIR, DB_FILENAME, PLATFORMS
from .ingestion import RuntimeData, async_subscribe

_LOGGER = logging.getLogger(__name__)

DRAIN_TIMEOUT_S = 10
CANCEL_GRACE_S = 5


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    store = await AsyncEventStore.open(
        hass.config.path(DB_DIR, DB_FILENAME),
        executor=hass.async_add_executor_job,
    )
    if not store.is_available:
        store.store.close()
        raise ConfigEntryNotReady("健康事件存储不可用，稍后重试")

    runtime = RuntimeData(store=store)
    entry.runtime_data = runtime
    runtime.unsubs.extend(async_subscribe(hass, entry.entry_id, runtime))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """卸载顺序：已成功则短路 → 先卸平台（失败即原样返回）→ 停止接收 → 排空 → 关库。"""
    # HA 在卸载后会清掉 runtime_data：没有运行时对象说明已经卸过，直接幂等返回
    runtime: RuntimeData | None = getattr(entry, "runtime_data", None)
    if runtime is None or runtime.unload_ok:
        return True

    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    runtime.closed = True
    for unsub in runtime.unsubs:
        unsub()
    runtime.unsubs.clear()

    try:
        tasks = list(runtime.tasks)
        if tasks:
            pending = asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.wait_for(asyncio.shield(pending), timeout=DRAIN_TIMEOUT_S)
            except TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.wait(tasks, timeout=CANCEL_GRACE_S)
                pending.cancel()
            except asyncio.CancelledError:
                await asyncio.wait(tasks, timeout=CANCEL_GRACE_S)
                pending.cancel()
                raise
    finally:
        # 幂等关库：只置标志、无 I/O、不取写锁（取消路径也必须执行）
        runtime.store.store.close()

    runtime.unload_ok = True
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
