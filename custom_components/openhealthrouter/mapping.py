"""实体映射模型与纯函数摄入。

设计依据：openspec/changes/ha-entity-ingestion/design.md D2/D3/D6/D7。
本模块不 import homeassistant：HA 侧只负责组 `StateView` 并调用 `map_state_to_event`。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .metrics import ISSUE_CODES, get_metric
from .models import SCHEMA_VERSION, HealthEvent

MAPPING_VERSION = "1"
SELF_OBJECT_PREFIX = "openhealthrouter"

# 原因码闭集（与契约质量代码 ISSUE_CODES 互不相交）
REASON_STATE_UNAVAILABLE = "state_unavailable"
REASON_STATE_UNKNOWN = "state_unknown"
REASON_VALUE_MISSING = "value_missing"
REASON_VALUE_NOT_NUMERIC = "value_not_numeric"
REASON_UNIT_MISSING = "unit_missing"
REASON_UNIT_NOT_ALLOWED = "unit_not_allowed"
REASON_TIME_UNAVAILABLE = "time_unavailable"
REASON_TIME_NOT_PARSEABLE = "time_not_parseable"
REASON_EVENT_INVALID = "event_invalid"
REASON_STORE_ERROR = "store_error"

REASON_CODES: frozenset[str] = frozenset(
    {
        REASON_STATE_UNAVAILABLE,
        REASON_STATE_UNKNOWN,
        REASON_VALUE_MISSING,
        REASON_VALUE_NOT_NUMERIC,
        REASON_UNIT_MISSING,
        REASON_UNIT_NOT_ALLOWED,
        REASON_TIME_UNAVAILABLE,
        REASON_TIME_NOT_PARSEABLE,
        REASON_EVENT_INVALID,
        REASON_STORE_ERROR,
    }
)

_PROVENANCE_TYPES = (
    "observation",
    "derived.vendor",
    "derived.openhealthrouter",
    "derived.user",
)

_UNIT_FOLD = str.maketrans({"²": "2", "³": "3", "µ": "u", "μ": "u"})
_UNIT_ALIASES = {"lbs": "lb"}


def normalize_unit(unit: str) -> str:
    """单位归一：空白、大小写、上标数字、微符号，外加单条显式别名 lbs→lb。

    刻意只做这些：别名映射越多，\"声明 kg 却上报磅\"这类错配就越难被发现。
    """
    folded = unit.strip().casefold().translate(_UNIT_FOLD)
    return _UNIT_ALIASES.get(folded, folded)


class EntityMapping(BaseModel):
    """一条映射：如何把一个实体状态变成一个健康事件。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    mapping_id: str
    source_entity: str
    subject_ref: str
    metric: str
    unit: str
    unit_attribute: str | None = None
    value_attribute: str | None = None
    device_ref: str | None = None
    native_id_attribute: str | None = None
    observed_at_attribute: str | None = None
    fallback_time: Literal["state.last_changed"] | None = None
    provenance_type: str = "observation"
    quality_issues: list[str] = Field(default_factory=list)
    enabled: bool = True


@dataclass(frozen=True)
class StateView:
    """与 HA 解耦的状态快照。"""

    entity_id: str
    state: str
    attributes: Mapping[str, Any]
    last_changed: datetime | None
    timezone_name: str
    integration: str | None = None


@dataclass(frozen=True)
class Skipped:
    """跳过结果：原因码 + 仅供诊断的来源信息（不含健康值）。"""

    reason: str
    mapping_id: str | None = None
    entity_id: str | None = None


@dataclass(frozen=True)
class _ResolvedTime:
    value: date | datetime
    precision: Literal["second", "minute", "day"]
    from_state: bool


def _is_self_entity(entity_id: str) -> bool:
    """自身实体：object_id 以集成域名开头（如 sensor.openhealthrouter_ingested）。"""
    _, _, object_id = entity_id.partition(".")
    return object_id.startswith(SELF_OBJECT_PREFIX)


def validate_mapping(
    mapping: EntityMapping, entity_check: Callable[[str], bool]
) -> list[str]:
    """保存前校验，返回错误键列表（空列表表示通过）。"""
    errors: list[str] = []
    if _is_self_entity(mapping.source_entity):
        errors.append("entity_is_self")
    elif not entity_check(mapping.source_entity):
        errors.append("entity_not_found")

    spec = get_metric(mapping.metric)
    if spec is None:
        errors.append("metric_not_registered")
    elif mapping.unit not in spec.allowed_units:
        # 保存校验走字面量：不接受"仅归一后相同"的写法
        errors.append("unit_not_allowed")

    if mapping.provenance_type not in _PROVENANCE_TYPES:
        errors.append("provenance_not_registered")
    if any(code not in ISSUE_CODES for code in mapping.quality_issues):
        errors.append("issue_not_registered")
    if mapping.provenance_type == "derived.vendor" and "estimated" not in (
        mapping.quality_issues
    ):
        errors.append("estimated_required")
    if mapping.observed_at_attribute is None and mapping.fallback_time is None:
        errors.append("time_source_missing")
    return errors


def _as_float(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError:
            return None
    else:
        return None
    return value if math.isfinite(value) else None


def _read_attribute(attributes: Mapping[str, Any], name: str | None) -> Any:
    if name is None:
        return None
    return attributes.get(name)


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _parse_observed_time(
    raw: Any, zone: ZoneInfo
) -> tuple[date | datetime, Literal["second", "minute", "day"]] | None:
    """把来源值解析为 (时间, 精度)；无法解析返回 None。"""
    if isinstance(raw, datetime):
        moment = raw if raw.tzinfo is not None else raw.replace(tzinfo=zone)
        return moment.astimezone(zone), "second"
    if isinstance(raw, date):
        return raw, "day"
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text), "day"
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone), "second"


def _resolve_time(
    mapping: EntityMapping, view: StateView, zone: ZoneInfo
) -> _ResolvedTime | Skipped:
    skip = Skipped(REASON_TIME_UNAVAILABLE, mapping.mapping_id, view.entity_id)
    if mapping.observed_at_attribute is not None:
        raw = _read_attribute(view.attributes, mapping.observed_at_attribute)
        if not _blank(raw):
            parsed = _parse_observed_time(raw, zone)
            if parsed is None:
                return Skipped(
                    REASON_TIME_NOT_PARSEABLE, mapping.mapping_id, view.entity_id
                )
            return _ResolvedTime(parsed[0], parsed[1], from_state=False)
        if mapping.fallback_time is None:
            return skip
    elif mapping.fallback_time is None:
        return skip

    if view.last_changed is None:
        return skip
    return _ResolvedTime(view.last_changed.astimezone(zone), "second", from_state=True)


def _resolve_unit(
    mapping: EntityMapping, view: StateView, value_from_state: bool
) -> str | Skipped:
    spec = get_metric(mapping.metric)
    if spec is None:
        return Skipped(REASON_EVENT_INVALID, mapping.mapping_id, view.entity_id)

    if mapping.unit_attribute is not None:
        raw = _read_attribute(view.attributes, mapping.unit_attribute)
        if _blank(raw) or not isinstance(raw, str):
            return Skipped(REASON_UNIT_MISSING, mapping.mapping_id, view.entity_id)
        if normalize_unit(raw) != normalize_unit(mapping.unit):
            return Skipped(REASON_UNIT_NOT_ALLOWED, mapping.mapping_id, view.entity_id)
        if normalize_unit(raw) not in {normalize_unit(u) for u in spec.allowed_units}:
            return Skipped(REASON_UNIT_NOT_ALLOWED, mapping.mapping_id, view.entity_id)
        return mapping.unit

    if value_from_state:
        entity_unit = view.attributes.get("unit_of_measurement")
        if not _blank(entity_unit) and isinstance(entity_unit, str):
            if normalize_unit(entity_unit) != normalize_unit(mapping.unit):
                return Skipped(
                    REASON_UNIT_NOT_ALLOWED, mapping.mapping_id, view.entity_id
                )
    return mapping.unit


def map_state_to_event(
    mapping: EntityMapping, view: StateView
) -> HealthEvent | Skipped:
    """把一个状态快照映射为健康事件；任何失败都返回原因码，不抛异常。"""
    skip_base = {"mapping_id": mapping.mapping_id, "entity_id": view.entity_id}

    if view.state == "unavailable":
        return Skipped(REASON_STATE_UNAVAILABLE, **skip_base)
    if view.state == "unknown":
        return Skipped(REASON_STATE_UNKNOWN, **skip_base)

    value_from_state = mapping.value_attribute is None
    raw_value = (
        view.state
        if value_from_state
        else _read_attribute(view.attributes, mapping.value_attribute)
    )
    if _blank(raw_value):
        return Skipped(REASON_VALUE_MISSING, **skip_base)
    value = _as_float(raw_value)
    if value is None:
        return Skipped(REASON_VALUE_NOT_NUMERIC, **skip_base)

    unit = _resolve_unit(mapping, view, value_from_state)
    if isinstance(unit, Skipped):
        return unit

    zone = ZoneInfo(view.timezone_name)
    resolved = _resolve_time(mapping, view, zone)
    if isinstance(resolved, Skipped):
        return resolved

    observed: dict[str, Any] = {
        "start": resolved.value.isoformat()
        if isinstance(resolved.value, date) and not isinstance(resolved.value, datetime)
        else resolved.value.isoformat(),
        "precision": resolved.precision,
        "timezone": view.timezone_name,
    }

    issues = list(dict.fromkeys(mapping.quality_issues))
    if resolved.from_state:
        issues = list(dict.fromkeys([*issues, "observed_time_from_ha_state"]))

    native_id = _read_attribute(view.attributes, mapping.native_id_attribute)
    source: dict[str, Any] = {
        "kind": "ha_entity",
        "entityId": view.entity_id,
        "integration": view.integration,
        "nativeId": None if _blank(native_id) else str(native_id),
    }

    payload: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "subjectRef": mapping.subject_ref,
        "metric": mapping.metric,
        "value": value,
        "unit": unit,
        "observedAt": observed,
        "recordedAt": datetime.now(UTC).isoformat(),
        "source": source,
        "deviceRef": mapping.device_ref,
        "provenance": {
            "type": mapping.provenance_type,
            "capturedBy": "device",
            "converter": f"entity-mapper@{MAPPING_VERSION}",
        },
        "quality": {"issues": issues},
    }
    try:
        return HealthEvent.model_validate(payload)
    except ValidationError:
        return Skipped(REASON_EVENT_INVALID, **skip_base)
