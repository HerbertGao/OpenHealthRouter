"""HealthEvent 契约模型校验测试（tasks 2.1–2.6 验证）。"""

from typing import Any

import pytest
from pydantic import ValidationError

from custom_components.openhealthrouter.models import HealthEvent

BASE_EVENT: dict = {
    "schemaVersion": "1.0",
    "subjectRef": "subject_local_1",
    "metric": "body.weight",
    "value": 72.4,
    "unit": "kg",
    "observedAt": {
        "start": "2026-01-01T00:00:00+08:00",
        "precision": "second",
        "timezone": "Asia/Shanghai",
    },
    "recordedAt": "2026-01-01T00:00:01+08:00",
    "source": {
        "kind": "ha_entity",
        "integration": "synthetic_scale",
        "entityId": "sensor.synthetic_scale_weight",
        "nativeId": "synthetic-measurement-0001",
    },
    "deviceRef": "device_local_1",
    "provenance": {
        "type": "observation",
        "capturedBy": "device",
        "converter": "entity-mapper@1",
    },
    "quality": {"status": "valid", "confirmation": "unconfirmed", "issues": []},
    "context": {},
    "evidence": {"rawAttributes": {}},
    "correctionOf": None,
    "derivedFrom": [],
}


def make_event(**overrides: object) -> HealthEvent:
    import copy

    data = copy.deepcopy(BASE_EVENT)
    for key, value in overrides.items():
        if value is None and key not in data:
            continue
        data[key] = value
    return HealthEvent.model_validate(data)


def test_valid_weight_event() -> None:
    event = make_event()
    assert event.event_id is not None
    assert event.event_id.startswith("evt_")
    assert event.unit == "kg"
    assert event.quality.issues == []


def test_camel_case_dump() -> None:
    dumped = make_event().model_dump(mode="json", by_alias=True)
    assert "eventId" in dumped
    assert "observedAt" in dumped
    assert "rawAttributes" in dumped["evidence"]
    assert "correctionOf" in dumped


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError, match="extra_field"):
        make_event(extra_field=1)


def test_value_components_mutex() -> None:
    with pytest.raises(ValidationError, match="恰好出现一个"):
        make_event(components=[{"metric": "body.weight", "value": 72.4, "unit": "kg"}])
    with pytest.raises(ValidationError, match="恰好出现一个"):
        make_event(value=None, unit=None)


def test_unit_components_mutex() -> None:
    data = dict(BASE_EVENT)
    data.pop("value")
    data.pop("unit")
    data["components"] = [{"metric": "body.weight", "value": 72.4, "unit": "kg"}]
    data["unit"] = "kg"
    with pytest.raises(ValidationError, match="互斥"):
        HealthEvent.model_validate(data)


def test_schema_version_must_be_1() -> None:
    with pytest.raises(ValidationError, match="1.0"):
        make_event(schemaVersion="2.0")


def test_day_precision_with_datetime_rejected() -> None:
    with pytest.raises(ValidationError, match="纯日期"):
        make_event(
            observedAt={
                "start": "2026-01-01T12:00:00+08:00",
                "precision": "day",
                "timezone": "Asia/Shanghai",
            }
        )


def test_pure_date_with_second_precision_rejected() -> None:
    with pytest.raises(ValidationError, match="day"):
        make_event(
            observedAt={
                "start": "2026-01-01",
                "precision": "second",
                "timezone": "Asia/Shanghai",
            }
        )


def test_day_interval_keeps_pure_dates() -> None:
    event = make_event(
        observedAt={
            "start": "2026-11-12",
            "end": "2026-11-13",
            "precision": "day",
            "timezone": "Asia/Shanghai",
        }
    )
    dumped = event.model_dump(mode="json", by_alias=True)
    assert dumped["observedAt"]["start"] == "2026-11-12"
    assert dumped["observedAt"]["end"] == "2026-11-13"
    assert "T" not in dumped["observedAt"]["start"]


def test_granularity_mismatch_rejected() -> None:
    with pytest.raises(ValidationError, match="粒度"):
        make_event(
            observedAt={
                "start": "2026-11-12",
                "end": "2026-11-13T00:00:00+08:00",
                "precision": "day",
                "timezone": "Asia/Shanghai",
            }
        )


def test_end_before_start_rejected() -> None:
    with pytest.raises(ValidationError, match="晚于"):
        make_event(
            observedAt={
                "start": "2026-11-13T00:00:00+08:00",
                "end": "2026-11-12T00:00:00+08:00",
                "precision": "second",
                "timezone": "Asia/Shanghai",
            }
        )


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValidationError, match="时区"):
        make_event(
            observedAt={
                "start": "2026-01-01T00:00:00",
                "precision": "second",
                "timezone": "Asia/Shanghai",
            }
        )
    with pytest.raises(ValidationError, match="时区"):
        make_event(recordedAt="2026-01-01T00:00:01")


def test_invalid_timezone_rejected() -> None:
    with pytest.raises(ValidationError, match="非法时区"):
        make_event(
            observedAt={
                "start": "2026-01-01T00:00:00+08:00",
                "precision": "second",
                "timezone": "Mars/Olympus",
            }
        )


def test_nan_rejected() -> None:
    with pytest.raises(ValidationError):
        make_event(value=float("nan"))


def test_infinity_rejected() -> None:
    with pytest.raises(ValidationError):
        make_event(value=float("inf"))


def test_unregistered_metric_rejected() -> None:
    with pytest.raises(ValidationError, match="指标未注册"):
        make_event(metric="body.muscle_mass")


def test_disallowed_unit_rejected() -> None:
    with pytest.raises(ValidationError, match="jin"):
        make_event(unit="jin")


def test_unit_conversion_marks_issue() -> None:
    event = make_event(
        value=160.0,
        unit="lb",
    )
    assert event.unit == "kg"
    assert event.value == pytest.approx(72.5748, abs=1e-4)
    assert "unit_converted" in event.quality.issues


def test_component_level_validation() -> None:
    data: dict[str, Any] = dict(BASE_EVENT)
    data.pop("value")
    data.pop("unit")
    data["metric"] = "body.bmi"
    data["components"] = [{"metric": "body.weight", "value": 72.4, "unit": "jin"}]
    with pytest.raises(ValidationError, match="jin"):
        HealthEvent.model_validate(data)


def test_derived_vendor_requires_estimated() -> None:
    data: dict[str, Any] = dict(BASE_EVENT)
    data["provenance"] = {"type": "derived.vendor"}
    with pytest.raises(ValidationError, match="estimated"):
        HealthEvent.model_validate(data)
    data["provenance"] = {"type": "derived.vendor"}
    data["quality"] = {"issues": ["estimated"]}
    event = HealthEvent.model_validate(data)
    assert event.provenance.type == "derived.vendor"


def test_unregistered_issue_code_rejected() -> None:
    with pytest.raises(ValidationError, match="未注册质量代码"):
        make_event(quality={"issues": ["looks_weird"]})


def test_event_id_deterministic_and_recorded_at_independent() -> None:
    event1 = make_event()
    event2 = make_event(recordedAt="2026-01-02T10:00:00+00:00")
    assert event1.event_id == event2.event_id


def test_event_id_quality_independent() -> None:
    event1 = make_event()
    event2 = make_event(
        quality={
            "status": "valid",
            "confirmation": "confirmed",
            "issues": ["estimated"],
        },
        recordedAt="2026-01-01T00:00:02+08:00",
    )
    assert event2.quality.issues == ["estimated"]
    assert event1.event_id == event2.event_id


def test_native_id_mode_value_differences_same_id() -> None:
    event1 = make_event()
    event2 = make_event(value=99.9)
    assert event1.event_id == event2.event_id


def test_wrong_input_event_id_rejected() -> None:
    with pytest.raises(ValidationError, match="不符"):
        make_event(eventId="evt_deadbeef")


def test_component_order_does_not_change_id() -> None:
    base: dict[str, Any] = dict(BASE_EVENT)
    base.pop("value")
    base.pop("unit")
    base["metric"] = "body.bmi"
    comp_a = [{"metric": "body.weight", "value": 72.4, "unit": "kg"}]
    comp_b = [{"metric": "body.fat_percentage", "value": 18.5, "unit": "%"}]
    data_a = dict(base, components=comp_a + comp_b)
    data_b = dict(base, components=comp_b + comp_a)
    data_a["source"] = dict(base["source"])
    data_a["source"].pop("nativeId")
    data_b["source"] = dict(base["source"])
    data_b["source"].pop("nativeId")
    event_a = HealthEvent.model_validate(data_a)
    event_b = HealthEvent.model_validate(data_b)
    assert event_a.event_id == event_b.event_id


def test_timezone_equivalence_same_id() -> None:
    event1 = make_event()
    data: dict[str, Any] = dict(BASE_EVENT)
    data["source"] = dict(data["source"])
    data["source"].pop("nativeId")
    event2 = HealthEvent.model_validate(data)
    assert event1.event_id != event2.event_id  # 模式 A/B 身份不同集合
    data3 = dict(data)
    data3["observedAt"] = {
        "start": "2025-12-31T16:00:00+00:00",
        "precision": "second",
        "timezone": "Asia/Shanghai",
    }
    event3 = HealthEvent.model_validate(data3)
    assert event2.event_id == event3.event_id
