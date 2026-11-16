"""指标注册表、质量代码注册表与确定性单位转换（首期闭集）。

新增指标 = 注册表加一行（兼容变更，不升 schemaVersion 主版本）。
新增质量代码同理。复合指标（如血压）未来以 MetricSpec 增加声明字段扩展。
"""

from dataclasses import dataclass

LB_TO_KG = 0.45359237

# 首期质量代码闭集（spec: 来源、派生与质量标记）
ISSUE_CODES: frozenset[str] = frozenset(
    {
        "estimated",
        "unit_converted",
        "subject_unconfirmed",
        "observed_time_from_ha_state",
        "missing_context",
    }
)


@dataclass(frozen=True)
class MetricSpec:
    """单个指标的注册表记录。

    allowed_units 含 canonical_unit；convert_unit 仅支持显式列出的转换对。
    """

    metric: str
    allowed_units: frozenset[str]
    canonical_unit: str


_METRICS: dict[str, MetricSpec] = {
    "body.weight": MetricSpec("body.weight", frozenset({"kg", "lb"}), "kg"),
    "body.fat_percentage": MetricSpec("body.fat_percentage", frozenset({"%"}), "%"),
    "body.bmi": MetricSpec("body.bmi", frozenset({"kg/m²"}), "kg/m²"),
}

# 显式转换表：(metric, from_unit, to_unit) -> 系数，to = from * factor
_CONVERSIONS: dict[tuple[str, str, str], float] = {
    ("body.weight", "lb", "kg"): LB_TO_KG,
    ("body.weight", "kg", "lb"): 1.0 / LB_TO_KG,
}


def get_metric(name: str) -> MetricSpec | None:
    """查询指标；未注册返回 None。"""
    return _METRICS.get(name)


def convert_unit(
    value: float, from_unit: str, to_unit: str, metric: str
) -> float | None:
    """按注册表显式转换对换算；恒等转换原样返回；不支持返回 None。"""
    if from_unit == to_unit:
        return value
    factor = _CONVERSIONS.get((metric, from_unit, to_unit))
    if factor is None:
        return None
    return value * factor
