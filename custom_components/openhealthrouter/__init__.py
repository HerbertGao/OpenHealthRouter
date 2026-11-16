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
    try:
        async_subscribe(hass, entry.entry_id, runtime)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        # 回滚：否则 HA 重试 setup 会再订阅一次，同一变化被处理两遍
        runtime.closed = True
        for unsub in runtime.unsubs:
            unsub()
        runtime.unsubs.clear()
        try:
            # 平台转发可能部分成功，必须一并撤掉，否则重试后实体重复注册
            await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        except Exception:  # noqa: BLE001 - 回滚尽力而为，不掩盖原始异常
            _LOGGER.warning("回滚平台卸载失败")
        runtime.store.store.close()
        entry.runtime_data = None
        raise
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
        # 幂等关库：EventStore 的连接是每操作新建，close() 只置 _closed 标志，
        # 不持有连接、不取写锁，因此同步调用不会阻塞，取消路径也必须执行
        runtime.store.store.close()

    runtime.unload_ok = True
    return True
