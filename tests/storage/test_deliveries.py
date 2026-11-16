"""投递审计：列白名单、内容最小化与写入路径。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from custom_components.openhealthrouter.store import (
    DeliveryRecord,
    EventStore,
    StoreError,
    StoreUnavailable,
)

_EXPECTED_COLUMNS = {
    "delivery_id",
    "target_ref",
    "filter_digest",
    "since_key",
    "until_key",
    "event_count",
    "event_ids_json",
    "status",
    "error_category",
    "created_at",
}


def _record(**overrides) -> DeliveryRecord:
    payload = {
        "delivery_id": "dlv_1",
        "target_ref": "local-test",
        "filter_digest": "digest-1",
        "since_key": None,
        "until_key": None,
        "event_count": 2,
        "event_ids": ["evt_a", "evt_b"],
        "status": "success",
        "error_category": None,
        "created_at": "2026-11-12T00:00:00.000000Z",
    }
    payload.update(overrides)
    return DeliveryRecord(**payload)


def test_columns_are_exactly_the_declared_set(tmp_path: Path) -> None:
    store = EventStore.open(tmp_path / "events.db")
    try:
        conn = sqlite3.connect(tmp_path / "events.db")
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(deliveries)")}
        finally:
            conn.close()
        assert columns == _EXPECTED_COLUMNS
    finally:
        store.close()


def test_records_round_trip(tmp_path: Path) -> None:
    store = EventStore.open(tmp_path / "events.db")
    try:
        store.record_delivery(_record())
        rows = store.list_deliveries()
        assert len(rows) == 1
        assert rows[0]["event_ids"] == ["evt_a", "evt_b"]
        assert rows[0]["status"] == "success"
    finally:
        store.close()


def test_audit_holds_no_payload_or_secret(tmp_path: Path) -> None:
    store = EventStore.open(tmp_path / "events.db")
    try:
        store.record_delivery(_record(target_ref="local-test"))
        dump = json.dumps(store.list_deliveries())
        for forbidden in ("72.4", "unit_of_measurement", "secret", "https://"):
            assert forbidden not in dump
    finally:
        store.close()


def test_failed_status_requires_category(tmp_path: Path) -> None:
    store = EventStore.open(tmp_path / "events.db")
    try:
        with pytest.raises(StoreError):
            store.record_delivery(_record(status="failed", error_category=None))
        assert store.list_deliveries() == []
    finally:
        store.close()


def test_skipped_no_events_is_allowed(tmp_path: Path) -> None:
    store = EventStore.open(tmp_path / "events.db")
    try:
        store.record_delivery(
            _record(status="skipped_no_events", event_count=0, event_ids=[])
        )
        rows = store.list_deliveries()
        assert rows[0]["status"] == "skipped_no_events"
        assert rows[0]["event_count"] == 0
    finally:
        store.close()


def test_recording_on_closed_store_is_rejected(tmp_path: Path) -> None:
    store = EventStore.open(tmp_path / "events.db")
    store.close()
    with pytest.raises(StoreUnavailable):
        store.record_delivery(_record())
