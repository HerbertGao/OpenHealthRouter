"""指标注册表与单位转换测试（tasks 1.1/1.2）。"""

import pytest

from custom_components.openhealthrouter.metrics import (
    ISSUE_CODES,
    LB_TO_KG,
    convert_unit,
    get_metric,
)


def test_registered_metrics() -> None:
    weight = get_metric("body.weight")
    assert weight is not None
    assert weight.canonical_unit == "kg"
    assert weight.allowed_units == frozenset({"kg", "lb"})
    assert get_metric("body.fat_percentage") is not None
    assert get_metric("body.bmi") is not None


def test_unregistered_metric_returns_none() -> None:
    assert get_metric("body.muscle_mass") is None


def test_issue_codes_closed_set() -> None:
    assert ISSUE_CODES == frozenset(
        {
            "estimated",
            "unit_converted",
            "subject_unconfirmed",
            "observed_time_from_ha_state",
            "missing_context",
        }
    )


@pytest.mark.parametrize(
    ("value", "from_unit", "to_unit", "expected"),
    [
        (72.4, "kg", "lb", 72.4 / LB_TO_KG),
        (160.0, "lb", "kg", 160.0 * LB_TO_KG),
        (72.4, "kg", "kg", 72.4),
    ],
)
def test_convert_supported_pairs(
    value: float, from_unit: str, to_unit: str, expected: float
) -> None:
    result = convert_unit(value, from_unit, to_unit, "body.weight")
    assert result is not None
    assert result == pytest.approx(expected, abs=1e-9)


def test_convert_round_trip() -> None:
    kg = convert_unit(160.0, "lb", "kg", "body.weight")
    assert kg is not None
    back = convert_unit(kg, "kg", "lb", "body.weight")
    assert back is not None
    assert back == pytest.approx(160.0, abs=1e-9)


@pytest.mark.parametrize(
    ("metric", "from_unit", "to_unit"),
    [
        ("body.weight", "jin", "kg"),
        ("body.weight", "kg", "jin"),
        ("body.fat_percentage", "%", "kg"),
        ("body.muscle_mass", "kg", "lb"),
    ],
)
def test_convert_unsupported_returns_none(
    metric: str, from_unit: str, to_unit: str
) -> None:
    assert convert_unit(72.4, from_unit, to_unit, metric) is None
