"""统一导出策略与 NDJSON 序列化。

设计依据：openspec/changes/health-export/design.md D1/D2/D4。
两个出口（NDJSON 下载、Webhook 投递）只能调本模块，保证同一策略得到同一份字节。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant

from .models import HealthEvent
from .store import EventStore, Page, QueryFilters, time_key

EXPORT_BATCH = 200
"""每批读取条数。必须小于测试样本量，才能证明跨分页可用。"""

REDACTED_SUBJECT = "redacted"


@dataclass(frozen=True)
class ExportSelection:
    """导出策略。四个层级开关默认全关（对齐 docs/privacy.md §2）。"""

    metrics: frozenset[str] | None = None
    since: datetime | None = None
    until: datetime | None = None
    quality_status: frozenset[str] = frozenset({"valid"})
    include_raw_attributes: bool = False
    include_device_refs: bool = False
    include_subject: bool = False
    include_context: bool = False


def _selection_filters(selection: ExportSelection, batch: int) -> QueryFilters:
    return QueryFilters(
        metrics=selection.metrics,
        since=selection.since,
        until=selection.until,
        limit=batch,
    )


def filter_digest(selection: ExportSelection) -> str:
    """策略摘要：规范化 JSON 的 SHA-256（不落可读的指标与时间范围）。"""
    payload: dict[str, Any] = {
        "metrics": None if selection.metrics is None else sorted(selection.metrics),
        "since": None if selection.since is None else time_key(selection.since),
        "until": None if selection.until is None else time_key(selection.until),
        "quality_status": sorted(selection.quality_status),
        "include_raw_attributes": selection.include_raw_attributes,
        "include_device_refs": selection.include_device_refs,
        "include_subject": selection.include_subject,
        "include_context": selection.include_context,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _fetch(hass: HomeAssistant, store: EventStore, filters: QueryFilters, after):  # noqa: ANN001, ANN202
    return await hass.async_add_executor_job(
        lambda: store.fetch_page(filters, after=after, limit=filters.limit)
    )


async def iter_export_batches(
    hass: HomeAssistant,
    store: EventStore,
    selection: ExportSelection,
    batch: int = EXPORT_BATCH,
) -> AsyncIterator[list[HealthEvent]]:
    """按快照语义逐批产出事件。

    终止条件只有"游标耗尽"：某批经质量过滤后可能为空，空批不得 break，
    否则导出会被静默截断（两个出口会一致地少数据，一致性测试也发现不了）。
    """
    filters = _selection_filters(selection, batch)
    after: tuple[str, str, str] | None = None
    while True:
        page: Page = await _fetch(hass, store, filters, after)
        selected = [
            event
            for event in page.events
            if event.quality.status in selection.quality_status
        ]
        if selected:
            yield selected
        if not page.has_more:
            return
        after = page.cursor


def _redact(payload: dict[str, Any], selection: ExportSelection) -> dict[str, Any]:
    if not selection.include_subject:
        payload["subjectRef"] = REDACTED_SUBJECT
    if not selection.include_device_refs:
        payload["deviceRef"] = None
        source = payload.get("source")
        if isinstance(source, dict):
            source["nativeId"] = None
    if not selection.include_context:
        payload["context"] = {}
    if not selection.include_raw_attributes:
        evidence = payload.get("evidence")
        if isinstance(evidence, dict):
            evidence["rawAttributes"] = {}
    return payload


def event_row(event: HealthEvent, selection: ExportSelection) -> dict[str, Any]:
    """单行导出内容：裁剪投影，不是可再校验的 HealthEvent。"""
    return _redact(event.model_dump(mode="json", by_alias=True), selection)


def to_ndjson(events: list[HealthEvent], selection: ExportSelection) -> bytes:
    """逐行紧凑 JSON；同一输入两次调用必须逐字节相同。"""
    lines = [
        json.dumps(event_row(event, selection), sort_keys=True, separators=(",", ":"))
        for event in events
    ]
    return ("\n".join(lines) + "\n").encode("utf-8") if lines else b""


async def select_events(
    hass: HomeAssistant,
    store: EventStore,
    selection: ExportSelection,
    batch: int = EXPORT_BATCH,
) -> list[HealthEvent]:
    """便利包装：仅在需要全量列表时使用（禁止成为流式路径的实现）。"""
    collected: list[HealthEvent] = []
    async for chunk in iter_export_batches(hass, store, selection, batch):
        collected.extend(chunk)
    return collected


def selection_summary(selection: ExportSelection) -> dict[str, Any]:
    """预览用摘要：只含策略字段，不含任何事件内容。"""
    summary = asdict(selection)
    summary["metrics"] = (
        None if selection.metrics is None else sorted(selection.metrics)
    )
    summary["quality_status"] = sorted(selection.quality_status)
    summary["since"] = None if selection.since is None else selection.since.isoformat()
    summary["until"] = None if selection.until is None else selection.until.isoformat()
    return summary
