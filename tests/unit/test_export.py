"""任务 1.2–1.4：导出策略、裁剪与序列化。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.openhealthrouter.export import (
    REDACTED_SUBJECT,
    ExportSelection,
    event_row,
    filter_digest,
    selection_summary,
    to_ndjson,
)
from custom_components.openhealthrouter.models import HealthEvent

SH = ZoneInfo("Asia/Shanghai")


def _event(**overrides) -> HealthEvent:
    payload: dict = {
        "schemaVersion": "1.0",
        "subjectRef": "subject_local_1",
        "metric": "body.weight",
        "value": 72.4,
        "unit": "kg",
        "observedAt": {
            "start": "2026-11-12T08:00:00+08:00",
            "precision": "second",
            "timezone": "Asia/Shanghai",
        },
        "recordedAt": "2026-11-12T08:00:01+08:00",
        "source": {
            "kind": "ha_entity",
            "integration": "synthetic_scale",
            "entityId": "sensor.synthetic_scale_weight",
            "nativeId": "native-1",
        },
        "deviceRef": "device_local_1",
        "provenance": {"type": "observation"},
        "quality": {"issues": []},
        "context": {"note": "synthetic"},
        "evidence": {"rawAttributes": {"unit_of_measurement": "kg"}},
    }
    payload.update(overrides)
    return HealthEvent.model_validate(payload)


def test_default_redaction_removes_layers() -> None:
    row = event_row(_event(), ExportSelection())
    assert row["subjectRef"] == REDACTED_SUBJECT
    assert row["deviceRef"] is None
    assert row["source"]["nativeId"] is None
    assert row["context"] == {}
    assert row["evidence"]["rawAttributes"] == {}
    # 事件内容本身仍保留
    assert row["value"] == pytest.approx(72.4)
    assert row["metric"] == "body.weight"


def test_switches_restore_fields() -> None:
    selection = ExportSelection(
        include_raw_attributes=True,
        include_device_refs=True,
        include_subject=True,
        include_context=True,
    )
    row = event_row(_event(), selection)
    assert row["subjectRef"] == "subject_local_1"
    assert row["deviceRef"] == "device_local_1"
    assert row["source"]["nativeId"] == "native-1"
    assert row["context"] == {"note": "synthetic"}
    assert row["evidence"]["rawAttributes"] == {"unit_of_measurement": "kg"}


def test_ndjson_is_byte_stable() -> None:
    events = [_event(), _event(value=73.1)]
    first = to_ndjson(events, ExportSelection())
    second = to_ndjson(events, ExportSelection())
    assert first == second
    assert first.endswith(b"\n")
    assert len(first.decode("utf-8").splitlines()) == 2


def test_ndjson_empty_is_empty_bytes() -> None:
    assert to_ndjson([], ExportSelection()) == b""


def test_redacted_row_is_a_projection_not_a_valid_event() -> None:
    """裁剪后身份字段改变：带原 eventId 再校验必须失败（不得静默换 ID）。"""
    row = event_row(_event(), ExportSelection())
    assert row["eventId"] is not None
    with pytest.raises(ValueError):
        HealthEvent.model_validate(row)
    # 去掉 eventId 会被重算成另一个 ID：这正是"投影不是可再校验事件"的含义
    recomputed = HealthEvent.model_validate({**row, "eventId": None})
    assert recomputed.event_id != row["eventId"]


def test_filter_digest_is_stable_and_discriminating() -> None:
    base = ExportSelection(metrics=frozenset({"body.weight"}))
    assert filter_digest(base) == filter_digest(
        ExportSelection(metrics=frozenset({"body.weight"}))
    )
    assert filter_digest(base) != filter_digest(
        ExportSelection(metrics=frozenset({"body.bmi"}))
    )
    assert filter_digest(base) != filter_digest(
        ExportSelection(metrics=frozenset({"body.weight"}), include_raw_attributes=True)
    )


def test_selection_summary_lists_effective_values() -> None:
    selection = ExportSelection(
        metrics=frozenset({"body.weight"}),
        since=datetime(2026, 11, 12, tzinfo=SH),
        until=datetime(2026, 11, 15, tzinfo=SH),
    )
    summary = selection_summary(selection)
    assert summary["metrics"] == ["body.weight"]
    assert summary["quality_status"] == ["valid"]
    assert summary["include_raw_attributes"] is False
    assert summary["since"].startswith("2026-11-12T00:00:00")


def test_selection_defaults_are_conservative() -> None:
    selection = ExportSelection()
    assert selection.quality_status == frozenset({"valid"})
    assert selection.include_raw_attributes is False
    assert selection.include_device_refs is False
    assert selection.include_subject is False
    assert selection.include_context is False


def test_ndjson_rows_parse_as_json() -> None:
    data = to_ndjson([_event()], ExportSelection())
    for line in data.decode("utf-8").splitlines():
        parsed = json.loads(line)
        assert parsed["metric"] == "body.weight"
        assert "eventId" in parsed


def test_digest_ignores_input_ordering_of_sets() -> None:
    a = ExportSelection(metrics=frozenset({"body.weight", "body.bmi"}))
    b = ExportSelection(metrics=frozenset({"body.bmi", "body.weight"}))
    assert filter_digest(a) == filter_digest(b)


def test_time_key_normalisation_in_digest() -> None:
    same_instant_sh = ExportSelection(since=datetime(2026, 11, 12, tzinfo=SH))
    same_instant_utc = ExportSelection(since=datetime(2026, 11, 11, 16, tzinfo=UTC))
    assert filter_digest(same_instant_sh) == filter_digest(same_instant_utc)
