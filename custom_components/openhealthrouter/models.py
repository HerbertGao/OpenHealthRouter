"""HealthEvent 契约模型（Pydantic v2，schemaVersion 1.0）。

after-validator 执行顺序（design D5）：结构校验 → ①派生标记检查（输入判定）
→ ②单位转换（自动追加仅限 unit_converted）→ ③eventId 自算（基于规范化值）。
"""

from datetime import date, datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from . import identity
from .metrics import ISSUE_CODES, convert_unit, get_metric

SCHEMA_VERSION = "1.0"

Precision = Literal["second", "minute", "day"]
ProvenanceType = Literal[
    "observation", "derived.vendor", "derived.openhealthrouter", "derived.user"
]
QualityStatus = Literal["valid", "suspect", "invalid"]
Confirmation = Literal["not_required", "unconfirmed", "confirmed"]


def _snake_to_camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


def _parse_iso(value: Any) -> Any:
    """JSON 契约中日期/日期时间以 ISO 字符串表达；对象原样通过。"""
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return datetime.fromisoformat(value)
    return value


def _parse_datetime(value: Any) -> Any:
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


DateOrDatetime = Annotated[date | datetime, BeforeValidator(_parse_iso)]
TzDatetime = Annotated[datetime, BeforeValidator(_parse_datetime)]


class _ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        strict=True,
        allow_inf_nan=False,
        alias_generator=_snake_to_camel,
    )


class ObservedAt(_ContractModel):
    start: DateOrDatetime
    precision: Precision
    timezone: str
    end: DateOrDatetime | None = None

    @field_validator("timezone")
    @classmethod
    def _timezone_iana(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"非法时区: {value}") from exc
        return value

    @model_validator(mode="after")
    def _check_temporal_semantics(self) -> ObservedAt:
        start_is_date = isinstance(self.start, date) and not isinstance(
            self.start, datetime
        )
        if self.start_is_naive_datetime():
            raise ValueError("时间必须带时区（naive 拒绝）")
        if self.precision == "day":
            if not start_is_date:
                raise ValueError("day 精度的 start 必须为纯日期，不得携带时刻")
        elif start_is_date:
            raise ValueError("纯日期仅允许 precision 为 day")
        if self.end is not None:
            end_is_date = isinstance(self.end, date) and not isinstance(
                self.end, datetime
            )
            if start_is_date != end_is_date:
                raise ValueError("start 与 end 粒度必须一致（同纯日期或同日期时间）")
            if isinstance(self.end, datetime) and self.end.tzinfo is None:
                raise ValueError("时间必须带时区（naive 拒绝）")
            if self.end <= self.start:  # type: ignore[operator]
                raise ValueError("end 必须晚于 start")
        return self

    def start_is_naive_datetime(self) -> bool:
        return isinstance(self.start, datetime) and self.start.tzinfo is None


class Source(_ContractModel):
    kind: str
    integration: str | None = None
    entity_id: str | None = None
    native_id: str | None = None


class Provenance(_ContractModel):
    type: ProvenanceType
    captured_by: str | None = None
    converter: str | None = None


class Quality(_ContractModel):
    status: QualityStatus = "valid"
    confirmation: Confirmation = "unconfirmed"
    issues: list[str] = Field(default_factory=list)


class Evidence(_ContractModel):
    raw_attributes: dict[str, Any] = Field(default_factory=dict)


class Component(_ContractModel):
    metric: str
    value: float
    unit: str


class HealthEvent(_ContractModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    event_id: str | None = None
    subject_ref: str
    metric: str
    value: float | None = None
    unit: str | None = None
    components: list[Component] | None = None
    observed_at: ObservedAt
    recorded_at: TzDatetime
    source: Source
    device_ref: str | None = None
    provenance: Provenance
    quality: Quality = Field(default_factory=Quality)
    context: dict[str, Any] = Field(default_factory=dict)
    evidence: Evidence = Field(default_factory=Evidence)
    correction_of: str | None = None
    derived_from: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_structure(self) -> HealthEvent:
        if (self.value is None) == (self.components is None):
            raise ValueError("value 与 components 必须恰好出现一个")
        if self.components is not None and not self.components:
            raise ValueError("components 不得为空")
        if self.unit is not None and self.components is not None:
            raise ValueError("顶层 unit 与 components 互斥（组件单位在组件内）")
        return self

    @model_validator(mode="after")
    def _check_markers(self) -> HealthEvent:
        if self.provenance.type == "derived.vendor" and "estimated" not in (
            self.quality.issues
        ):
            raise ValueError("derived.vendor 必须携带 estimated 质量问题")
        unknown = [c for c in self.quality.issues if c not in ISSUE_CODES]
        if unknown:
            raise ValueError(f"未注册质量代码: {unknown}；允许: {sorted(ISSUE_CODES)}")
        return self

    @model_validator(mode="after")
    def _normalize_units(self) -> HealthEvent:
        if isinstance(self.recorded_at, datetime) and self.recorded_at.tzinfo is None:
            raise ValueError("recordedAt 必须带时区（naive 拒绝）")
        if self.components is not None:
            self._normalize_component_units()
        else:
            self._normalize_single_unit()
        return self

    def _normalize_single_unit(self) -> None:
        spec = get_metric(self.metric)
        if spec is None:
            raise ValueError(f"指标未注册: {self.metric}")
        if self.unit is None:
            raise ValueError(f"{self.metric} 为数值测量，必须携带 unit")
        self._convert_to_canonical(spec, component=None)

    def _normalize_component_units(self) -> None:
        for component in self.components or []:
            spec = get_metric(component.metric)
            if spec is None:
                raise ValueError(f"指标未注册: {component.metric}")
            self._convert_to_canonical(spec, component=component)

    def _convert_to_canonical(self, spec: Any, component: Any) -> None:
        unit = component.unit if component is not None else self.unit
        value = component.value if component is not None else self.value
        if unit is None or value is None:  # 结构校验已保证，防御性收窄
            raise ValueError(f"{spec.metric} 缺少 unit 或 value")
        if unit not in spec.allowed_units:
            raise ValueError(
                f"单位 {unit} 不在 {spec.metric} 允许集合 {sorted(spec.allowed_units)}"
            )
        if unit == spec.canonical_unit:
            return
        converted = convert_unit(value, unit, spec.canonical_unit, spec.metric)
        if converted is None:
            raise ValueError(
                f"{spec.metric} 不支持单位转换 {unit} -> {spec.canonical_unit}"
            )
        if component is not None:
            component.value = converted
            component.unit = spec.canonical_unit
        else:
            self.value = converted
            self.unit = spec.canonical_unit
        if "unit_converted" not in self.quality.issues:
            self.quality.issues.append("unit_converted")

    @model_validator(mode="after")
    def _assign_event_id(self) -> HealthEvent:
        computed = identity.event_id(self)
        if self.event_id is not None and self.event_id != computed:
            raise ValueError(
                f"输入 eventId 与计算值不符: {self.event_id} != {computed}"
            )
        self.event_id = computed
        return self
