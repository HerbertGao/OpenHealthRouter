"""任务 5.1–5.4、4.2：删除语义、引用双清、作用域与取代标记。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from custom_components.openhealthrouter.store import (
    EventStore,
    QueryFilters,
    dump_payload,
    load_payload,
)


def _raw_payload(db: Path, event_id: str) -> str:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT payload_json FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        assert row is not None
        return str(row[0])
    finally:
        conn.close()


def _superseded_flag(db: Path, event_id: str) -> int:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT superseded FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        assert row is not None
        return int(row[0])
    finally:
        conn.close()


def test_deleted_event_is_not_queryable(tmp_path: Path, make_event) -> None:
    db = tmp_path / "events.db"
    store = EventStore.open(db)
    try:
        event = make_event()
        store.insert_events([event])
        result = store.delete_events([event.event_id])
        assert result.deleted == 1
        assert result.not_found == []

        assert store.get_event(event.event_id) is None
        events, total = store.query(QueryFilters(include_superseded=True))
        assert events == []
        assert total == 0
    finally:
        store.close()


def test_delete_keeps_other_events_and_clears_reference(
    tmp_path: Path, make_event
) -> None:
    db = tmp_path / "events.db"
    store = EventStore.open(db)
    try:
        original = make_event()
        correction = make_event(
            value=73.1,
            provenance={"type": "derived.user"},
            correctionOf=original.event_id,
        )
        store.insert_events([original, correction])

        store.delete_events([original.event_id])

        stored = store.get_event(correction.event_id)
        assert stored is not None
        assert stored.event.correction_of is None
        assert stored.event.value == pytest.approx(73.1)
        assert stored.event.event_id == correction.event_id

        raw = _raw_payload(db, correction.event_id)
        parsed = json.loads(raw)
        assert parsed["correctionOf"] is None
        assert parsed["eventId"] == correction.event_id
        assert raw == dump_payload(load_payload(raw))
    finally:
        store.close()


def test_delete_missing_event_reports_not_found(tmp_path: Path, make_event) -> None:
    store = EventStore.open(tmp_path / "events.db")
    try:
        result = store.delete_events(["evt_missing"])
        assert result.deleted == 0
        assert result.not_found == ["evt_missing"]
    finally:
        store.close()


def test_mixed_batch_reports_both_counts(tmp_path: Path, make_event) -> None:
    store = EventStore.open(tmp_path / "events.db")
    try:
        first = make_event()
        second = make_event(
            value=74.2,
            observedAt={
                "start": "2026-11-13T08:00:00+08:00",
                "precision": "second",
                "timezone": "Asia/Shanghai",
            },
        )
        store.insert_events([first, second])
        result = store.delete_events([first.event_id, "evt_missing"])
        assert result.deleted == 1
        assert result.not_found == ["evt_missing"]
        assert store.get_event(second.event_id) is not None
    finally:
        store.close()


def test_delete_middle_event_does_not_resurrect_old_version(
    tmp_path: Path, make_event
) -> None:
    db = tmp_path / "events.db"
    store = EventStore.open(db)
    try:
        a = make_event()
        b = make_event(
            value=73.1, provenance={"type": "derived.user"}, correctionOf=a.event_id
        )
        c = make_event(
            value=73.9, provenance={"type": "derived.user"}, correctionOf=b.event_id
        )
        store.insert_events([a, b, c])

        store.delete_events([b.event_id])

        events, _ = store.query()
        assert [event.event_id for event in events] == [c.event_id]
        assert _superseded_flag(db, a.event_id) == 1

        with_superseded, total = store.query(QueryFilters(include_superseded=True))
        assert total == 2
        assert {event.event_id for event in with_superseded} == {a.event_id, c.event_id}
    finally:
        store.close()


def test_delete_newest_event_leaves_no_current_version(
    tmp_path: Path, make_event
) -> None:
    store = EventStore.open(tmp_path / "events.db")
    try:
        a = make_event()
        b = make_event(
            value=73.1, provenance={"type": "derived.user"}, correctionOf=a.event_id
        )
        store.insert_events([a, b])
        store.delete_events([b.event_id])

        events, _ = store.query()
        assert events == []

        with_superseded, _ = store.query(QueryFilters(include_superseded=True))
        assert [event.event_id for event in with_superseded] == [a.event_id]
    finally:
        store.close()


def test_delete_empty_list_is_noop(tmp_path: Path, make_event) -> None:
    store = EventStore.open(tmp_path / "events.db")
    try:
        event = make_event()
        store.insert_events([event])
        result = store.delete_events([])
        assert result.deleted == 0
        assert result.not_found == []
        assert store.get_event(event.event_id) is not None
    finally:
        store.close()
