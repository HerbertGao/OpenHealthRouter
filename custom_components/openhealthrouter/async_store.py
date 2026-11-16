"""异步门面：同步实现是唯一真相，这一层只做协程排队与线程卸载。

设计依据：openspec/changes/local-event-store/design.md D8。
读路径不持写锁（WAL 允许并发读）；第 3 期把 to_thread 换成 HA executor 即可，签名不变。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .models import HealthEvent
from .store import (
    DeleteResult,
    EventStore,
    InsertResult,
    QueryFilters,
    StoredEvent,
)

Executor = Callable[..., Awaitable[Any]]


class AsyncEventStore:
    """串行化写、并行读的异步包装。"""

    def __init__(self, store: EventStore, executor: Executor | None = None) -> None:
        self._store = store
        self._executor = executor
        self._write_lock = asyncio.Lock()

    @classmethod
    async def open(
        cls, path: str | Path, *, executor: Executor | None = None
    ) -> AsyncEventStore:
        if executor is None:
            store = await asyncio.to_thread(EventStore.open, path)
        else:
            store = await executor(EventStore.open, path)
        return cls(store, executor)

    async def _run(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """统一出口：默认 to_thread，注入了执行器则走执行器。"""
        if self._executor is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._executor(func, *args, **kwargs)

    @property
    def is_available(self) -> bool:
        return self._store.is_available

    @property
    def store(self) -> EventStore:
        return self._store

    async def insert_events(self, events: list[HealthEvent]) -> InsertResult:
        async with self._write_lock:
            return await self._run(self._store.insert_events, events)

    async def delete_events(self, event_ids: list[str]) -> DeleteResult:
        async with self._write_lock:
            return await self._run(self._store.delete_events, event_ids)

    async def backup_to(self, target: str | Path) -> Path:
        async with self._write_lock:
            return await self._run(self._store.backup_to, target)

    async def query(
        self, filters: QueryFilters | None = None
    ) -> tuple[list[HealthEvent], int]:
        return await self._run(self._store.query, filters)

    async def get_event(self, event_id: str) -> StoredEvent | None:
        return await self._run(self._store.get_event, event_id)

    async def close(self) -> None:
        async with self._write_lock:
            await self._run(self._store.close)
