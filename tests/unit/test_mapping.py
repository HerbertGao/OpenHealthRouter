"""任务 2.1–2.3：映射模型、纯函数摄入与保存校验。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.openhealthrouter.mapping import (
    REASON_CODES,
    REASON_EVENT_INVALID,
    REASON_STATE_UNAVAILABLE,
    REASON_STATE_UNKNOWN,
    REASON_TIME_NOT_PARSEABLE,
    REASON_TIME_UNAVAILABLE,
    REASON_UNIT_MISSING,
    REASON_UNIT_NOT_ALLOWED,
    REASON_VALUE_MISSING,
    REASON_VALUE_NOT_NUMERIC,
    EntityMapping,
    Skipped,
    StateView,
    map_state_to_event,
    normalize_unit,
    validate_mapping,
)
from custom_components.openhealthrouter.metrics import ISSUE_CODES
from custom_components.openhealthrouter.models import HealthEvent

SH = ZoneInfo("Asia/Shanghai")
TZ_NAME = "Asia/Shanghai"
LOGICAL_TIME = "2026-11-12T08:00:00+08:00"


def _mapping(**overrides: Any) -> EntityMapping:
    payload: dict[str, Any] = {
        "mapping_id": "sensor.synthetic_scale_weight::body.weight",
        "source_entity": "sensor.synthetic_scale_weight",
        "subject_ref": "subject_local_1",
        "metric": "body.weight",
        "unit": "kg",
        "device_ref": "device_local_1",
        "observed_at_attribute": "measured_at",
    }
    payload.update(overrides)
    return EntityMapping.model_validate(payload)


def _attrs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"unit_of_measurement": "kg", "measured_at": LOGICAL_TIME}
    base.update(overrides)
    return base


def _view(**overrides: Any) -> StateView:
    payload: dict[str, Any] = {
        "entity_id": "sensor.synthetic_scale_weight",
        "state": "72.4",
        "attributes": _attrs(),
        "last_changed": datetime(2026, 11, 12, 8, 0, 1, tzinfo=SH),
        "timezone_name": TZ_NAME,
        "integration": "synthetic_scale",
    }
    payload.update(overrides)
    return StateView(**payload)


def _event(mapping: EntityMapping, view: StateView) -> HealthEvent:
    result = map_state_to_event(mapping, view)
    assert isinstance(result, HealthEvent), result
    return result


def _reason(mapping: EntityMapping, view: StateView) -> str:
    result = map_state_to_event(mapping, view)
    assert isinstance(result, Skipped), result
    return result.reason


# ---------- 原因码闭集 ----------


def test_reason_codes_closed_set_and_disjoint() -> None:
    assert len(REASON_CODES) == 10
    assert REASON_CODES & ISSUE_CODES == frozenset()


# ---------- 值来源 ----------


def test_value_from_state_and_source_fields_are_faithful() -> None:
    event = _event(_mapping(), _view())
    assert event.value == pytest.approx(72.4)
    assert event.unit == "kg"
    assert event.metric == "body.weight"
    assert event.subject_ref == "subject_local_1"
    assert event.device_ref == "device_local_1"
    assert event.source.kind == "ha_entity"
    assert event.source.entity_id == "sensor.synthetic_scale_weight"
    assert event.source.integration == "synthetic_scale"


def test_value_from_attribute() -> None:
    mapping = _mapping(value_attribute="weight_kg")
    view = _view(attributes={"weight_kg": "70.5", "measured_at": LOGICAL_TIME})
    assert _event(mapping, view).value == pytest.approx(70.5)


def test_value_missing_is_skipped() -> None:
    mapping = _mapping(value_attribute="weight_kg")
    view = _view(attributes={"measured_at": LOGICAL_TIME})
    assert _reason(mapping, view) == REASON_VALUE_MISSING


def test_value_not_numeric_is_skipped() -> None:
    view = _view(state="heavy", attributes=_attrs())
    assert _reason(_mapping(), view) == REASON_VALUE_NOT_NUMERIC


def test_idle_states_short_circuit() -> None:
    assert _reason(_mapping(), _view(state="unknown")) == REASON_STATE_UNKNOWN
    assert _reason(_mapping(), _view(state="unavailable")) == REASON_STATE_UNAVAILABLE


# ---------- 单位 ----------


def test_unit_normalisation() -> None:
    assert normalize_unit("LBS") == "lb"
    assert normalize_unit("lbs") == "lb"
    assert normalize_unit("kg/m²") == normalize_unit("kg/m2")
    assert normalize_unit(" µg ") == "ug"
    assert normalize_unit("lb") != normalize_unit("kg")


def test_pound_value_is_converted_and_flagged() -> None:
    mapping = _mapping(unit="lb")
    view = _view(state="160", attributes=_attrs(unit_of_measurement="lb"))
    event = _event(mapping, view)
    assert event.unit == "kg"
    assert event.value == pytest.approx(72.5748, abs=1e-4)
    assert "unit_converted" in event.quality.issues


def test_entity_unit_conflicting_with_mapping_is_skipped() -> None:
    view = _view(attributes=_attrs(unit_of_measurement="lb"))
    assert _reason(_mapping(unit="kg"), view) == REASON_UNIT_NOT_ALLOWED


def test_unit_attribute_missing_is_skipped() -> None:
    mapping = _mapping(unit_attribute="reported_unit")
    view = _view(attributes={"measured_at": LOGICAL_TIME})
    assert _reason(mapping, view) == REASON_UNIT_MISSING


def test_unit_attribute_mismatch_is_skipped() -> None:
    mapping = _mapping(unit_attribute="reported_unit")
    view = _view(attributes={"reported_unit": "lb", "measured_at": LOGICAL_TIME})
    assert _reason(mapping, view) == REASON_UNIT_NOT_ALLOWED


def test_unit_attribute_matching_is_accepted() -> None:
    mapping = _mapping(unit_attribute="reported_unit")
    view = _view(attributes={"reported_unit": "KG", "measured_at": LOGICAL_TIME})
    assert _event(mapping, view).unit == "kg"


# ---------- 时间 ----------


def test_offset_attribute_equals_same_instant() -> None:
    mapping = _mapping()
    utc_view = _view(attributes=_attrs(measured_at="2026-11-12T00:00:00+00:00"))
    local_view = _view(attributes=_attrs(measured_at=LOGICAL_TIME))
    utc_event = _event(mapping, utc_view)
    assert utc_event.observed_at.start == _event(mapping, local_view).observed_at.start
    assert utc_event.observed_at.timezone == TZ_NAME


def test_naive_attribute_interpreted_in_config_timezone() -> None:
    view = _view(attributes=_attrs(measured_at="2026-11-12T08:00:00"))
    start = _event(_mapping(), view).observed_at.start
    assert isinstance(start, datetime)
    assert start.utcoffset() == timedelta(hours=8)


def test_day_attribute_keeps_date_precision() -> None:
    view = _view(attributes=_attrs(measured_at="2026-11-12"))
    event = _event(_mapping(), view)
    assert event.observed_at.start == date(2026, 11, 12)
    assert event.observed_at.precision == "day"


def test_missing_time_without_fallback_is_skipped() -> None:
    assert _reason(_mapping(), _view(attributes={})) == REASON_TIME_UNAVAILABLE
    blanks = _attrs(measured_at="   ")
    assert _reason(_mapping(), _view(attributes=blanks)) == REASON_TIME_UNAVAILABLE


def test_unparsable_time_is_skipped() -> None:
    view = _view(attributes=_attrs(measured_at="yesterday-ish"))
    assert _reason(_mapping(), view) == REASON_TIME_NOT_PARSEABLE


def test_fallback_marks_quality_issue() -> None:
    mapping = _mapping(observed_at_attribute=None, fallback_time="state.last_changed")
    event = _event(mapping, _view(attributes={}))
    assert "observed_time_from_ha_state" in event.quality.issues
    last_changed = _view().last_changed
    assert last_changed is not None
    assert event.observed_at.start == last_changed.astimezone(SH)


# ---------- 质量标记与身份稳定性 ----------


def test_mapping_quality_issues_are_preserved() -> None:
    mapping = _mapping(
        metric="body.fat_percentage",
        unit="%",
        provenance_type="derived.vendor",
        quality_issues=["estimated"],
    )
    view = _view(state="18.5", attributes=_attrs(unit_of_measurement="%"))
    assert "estimated" in _event(mapping, view).quality.issues


def test_same_state_view_yields_same_event_id() -> None:
    mapping = _mapping()
    view = _view()
    assert _event(mapping, view).event_id == _event(mapping, view).event_id


def test_contract_rejection_is_reported_as_event_invalid(monkeypatch) -> None:
    import custom_components.openhealthrouter.mapping as module

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise module.ValidationError.from_exception_data("HealthEvent", [])

    monkeypatch.setattr(module.HealthEvent, "model_validate", boom)
    assert _reason(_mapping(), _view()) == REASON_EVENT_INVALID


# ---------- 保存校验 ----------


def _errors(mapping: EntityMapping, known: set[str] | None = None) -> list[str]:
    known_entities = known if known is not None else {"sensor.synthetic_scale_weight"}
    return validate_mapping(mapping, lambda entity: entity in known_entities)


def test_valid_mapping_passes() -> None:
    assert _errors(_mapping()) == []


def test_unknown_entity_and_self_entity_rejected() -> None:
    assert "entity_not_found" in _errors(_mapping(source_entity="sensor.missing"))
    self_entity = _mapping(source_entity="sensor.openhealthrouter_ingested")
    assert "entity_is_self" in _errors(self_entity)


def test_metric_and_unit_rejected() -> None:
    assert "metric_not_registered" in _errors(_mapping(metric="body.muscle_mass"))
    assert "unit_not_allowed" in _errors(_mapping(unit="jin"))


def test_literal_unit_required_at_save_time() -> None:
    # 保存走字面量：lbs 不能通过（即使运行时归一后等于 lb）
    assert "unit_not_allowed" in _errors(_mapping(unit="lbs"))


def test_quality_marker_rules() -> None:
    bad_marker = _mapping(quality_issues=["looks_weird"])
    assert "issue_not_registered" in _errors(bad_marker)

    vendor_without_estimated = _mapping(provenance_type="derived.vendor")
    assert "estimated_required" in _errors(vendor_without_estimated)

    vendor_ok = _mapping(provenance_type="derived.vendor", quality_issues=["estimated"])
    assert _errors(vendor_ok) == []


def test_time_source_required() -> None:
    assert "time_source_missing" in _errors(_mapping(observed_at_attribute=None))
    fallback_ok = _mapping(
        observed_at_attribute=None, fallback_time="state.last_changed"
    )
    assert _errors(fallback_ok) == []


def test_unknown_provenance_rejected() -> None:
    assert "provenance_not_registered" in _errors(_mapping(provenance_type="guess"))


def test_mapping_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError):
        EntityMapping.model_validate(
            {
                "mapping_id": "m",
                "source_entity": "sensor.x",
                "subject_ref": "s",
                "metric": "body.weight",
                "unit": "kg",
                "surprise": 1,
            }
        )
