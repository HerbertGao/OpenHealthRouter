"""事件 ID 两模式身份计算与规范序列化（design D5）。

本模块不 import models（由 models 单向引入），对事件对象做鸭子类型访问。
"""

import hashlib
import json
import math
from datetime import UTC, date, datetime
from typing import Any

EVENT_ID_PREFIX = "evt_"


def canonical_json(obj: Any) -> bytes:
    """规范序列化：紧凑分隔符、排序 key、UTF-8。

    数值有限性检查为纵深防御（模型层 allow_inf_nan=False 已拒绝）。
    """
    _check_finite(obj)
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _check_finite(obj: Any) -> None:
    if isinstance(obj, float) and not math.isfinite(obj):
        raise ValueError(f"非有限数值不能进入规范序列化: {obj!r}")
    if isinstance(obj, dict):
        for item in obj.values():
            _check_finite(item)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _check_finite(item)


def _norm_temporal(value: date | datetime | None) -> str | None:
    """datetime 归一 UTC 后序列化（同一时刻不同时区偏移表示等价）；date 原样。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive 时间不能进入事件身份")
        return value.astimezone(UTC).isoformat()
    return value.isoformat()


def _identity(event: Any) -> dict[str, Any]:
    native = getattr(event.source, "native_id", None)
    if native is not None:
        # 模式 A：原生记录身份（跨通道去重，值/时间差异不影响 ID）
        return {
            "schemaVersion": event.schema_version,
            "subjectRef": event.subject_ref,
            "metric": event.metric,
            "sourceIntegration": getattr(event.source, "integration", None),
            "sourceNativeId": native,
            "deviceRef": event.device_ref,
        }
    # 模式 B：规范化内容散列
    return {
        "schemaVersion": event.schema_version,
        "subjectRef": event.subject_ref,
        "metric": event.metric,
        "sourceEntityId": getattr(event.source, "entity_id", None),
        "value": event.value,
        "components": sorted(
            (c.value, c.metric, c.unit) for c in (event.components or [])
        ),
        "observedAt": {
            "start": _norm_temporal(event.observed_at.start),
            "end": _norm_temporal(event.observed_at.end),
            "precision": event.observed_at.precision,
            "timezone": event.observed_at.timezone,
        },
        "provenance": {"type": event.provenance.type},
    }


def event_id(event: Any) -> str:
    """按两模式身份计算事件 ID（design D5）。

    recordedAt、quality、correctionOf、derivedFrom、context、evidence 不参与身份。
    """
    digest = hashlib.sha256(canonical_json(_identity(event))).hexdigest()
    return EVENT_ID_PREFIX + digest
