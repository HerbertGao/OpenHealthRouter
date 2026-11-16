"""任务 2.1–2.5：幂等写入、事务原子性与失败可见性。"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from custom_components.openhealthrouter.store import EventStore, StoreError


def _row_count(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
    finally:
        conn.close()


def _superseded_flag(db: Path, event_id: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(
            conn.execute(
                "SELECT superseded FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()[0]
        )
    finally:
        conn.close()


def test_first_insert_creates(tmp_path: Path, make_event) -> None:
    store = EventStore.open(tmp_path / "events.db")
    try:
        event = make_event()
        result = store.insert_events([event])
        assert result.created == 1
        assert result.duplicates == 0
        stored = store.get_event(event.event_id)
        assert stored is not None
        assert stored.event.value == event.value
    finally:
        store.close()


def test_duplicate_insert_keeps_original(tmp_path: Path, make_event) -> None:
    db = tmp_path / "events.db"
    store = EventStore.open(db)
    try:
        source = {
            "kind": "ha_entity",
            "integration": "synthetic_scale",
            "nativeId": "synthetic-measurement-1",
        }
        first = make_event(source=source)
        store.insert_events([first])
        # 有原生 ID 时身份走模式 A：值变化不改变 eventId
        second = make_event(source=source, value=73.1)
        assert second.event_id == first.event_id
        result = store.insert_events([second])
        assert result.created == 0
        assert result.duplicates == 1
        assert _row_count(db) == 1
        stored = store.get_event(first.event_id)
        assert stored is not None
        assert stored.event.value == pytest.approx(72.4)
    finally:
        store.close()


def test_resubmission_after_target_deleted_reports_duplicate(
    tmp_path: Path, make_event
) -> None:
    store = EventStore.open(tmp_path / "events.db")
    try:
        original = make_event()
        correction = make_event(
            value=73.1,
            provenance={"type": "derived.user"},
            correctionOf=original.event_id,
        )
        store.insert_events([original, correction])
        store.delete_events([original.event_id])
        result = store.insert_events([correction])
        assert result.duplicates == 1
        assert result.created == 0
    finally:
        store.close()


def test_new_event_with_missing_target_rejected(tmp_path: Path, make_event) -> None:
    db = tmp_path / "events.db"
    store = EventStore.open(db)
    try:
        bogus = make_event(
            value=73.1,
            provenance={"type": "derived.user"},
            correctionOf="evt_" + "0" * 64,
        )
        with pytest.raises(StoreError):
            store.insert_events([bogus])
        assert _row_count(db) == 0
    finally:
        store.close()


def test_batch_correction_before_target_rejected(tmp_path: Path, make_event) -> None:
    db = tmp_path / "events.db"
    store = EventStore.open(db)
    try:
        original = make_event()
        correction = make_event(
            value=73.1,
            provenance={"type": "derived.user"},
            correctionOf=original.event_id,
        )
        with pytest.raises(StoreError):
            store.insert_events([correction, original])
        assert _row_count(db) == 0
    finally:
        store.close()


def test_batch_failure_rolls_back_and_leaves_superseded_untouched(
    tmp_path: Path, make_event
) -> None:
    db = tmp_path / "events.db"
    store = EventStore.open(db)
    try:
        base = make_event()
        store.insert_events([base])
        bad = make_event(
            value=74.2,
            metric="body.bmi",
            unit="kg/m²",
            provenance={"type": "derived.user"},
            correctionOf="evt_" + "1" * 64,
        )
        correction = make_event(
            value=73.1,
            provenance={"type": "derived.user"},
            correctionOf=base.event_id,
        )
        with pytest.raises(StoreError):
            store.insert_events([correction, bad])
        assert _row_count(db) == 1
        assert _superseded_flag(db, base.event_id) == 0
    finally:
        store.close()


def test_write_to_readonly_db_fails(tmp_path: Path, make_event) -> None:
    """让 open 成功，失败必须发生在写路径，并断言库内没有残留。"""
    db = tmp_path / "events.db"
    EventStore.open(db).close()
    db.chmod(0o400)
    try:
        store = EventStore.open(db)
        try:
            assert store.is_available
            with pytest.raises(StoreError):
                store.insert_events([make_event()])
        finally:
            store.close()
        db.chmod(0o600)
        assert _row_count(db) == 0
    finally:
        db.chmod(0o600)


def test_lock_conflict_reports_failure(tmp_path: Path, make_event) -> None:
    db = tmp_path / "events.db"
    store = EventStore.open(db)
    blocker = sqlite3.connect(db, isolation_level=None)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute(
            "INSERT INTO events (event_id, subject_ref, metric, observed_start_key, "
            "precision, superseded, payload_json, inserted_at_utc) "
            "VALUES ('evt_blocker','s','body.weight','2026-01-01T00:00:00.000000Z',"
            "'second',0,'{}','2026-01-01T00:00:00.000000Z')"
        )
        with pytest.raises(StoreError):
            store.insert_events([make_event()])
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
        store.close()


def test_concurrent_same_event_yields_single_row(tmp_path: Path, make_event) -> None:
    db = tmp_path / "events.db"
    EventStore.open(db).close()
    event = make_event()
    errors: list[BaseException] = []
    outcomes: list[tuple[int, int]] = []
    lock = threading.Lock()

    def worker() -> None:
        store = EventStore.open(db)
        try:
            result = store.insert_events([event])
            with lock:
                outcomes.append((result.created, result.duplicates))
        except BaseException as exc:  # noqa: BLE001 - 测试收集任意失败
            with lock:
                errors.append(exc)
        finally:
            store.close()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert _row_count(db) == 1
    assert sum(created for created, _ in outcomes) == 1
    assert sum(dup for _, dup in outcomes) == 5
    assert all(created + dup == 1 for created, dup in outcomes)


def test_concurrent_distinct_events_all_visible(tmp_path: Path, make_event) -> None:
    db = tmp_path / "events.db"
    EventStore.open(db).close()
    events = [make_event(value=70.0 + index) for index in range(6)]
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(event) -> None:  # noqa: ANN001 - 测试内联
        store = EventStore.open(db)
        try:
            store.insert_events([event])
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)
        finally:
            store.close()

    threads = [threading.Thread(target=worker, args=(event,)) for event in events]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    store = EventStore.open(db)
    try:
        assert _row_count(db) == len(events)
        for event in events:
            assert store.get_event(event.event_id) is not None
    finally:
        store.close()
