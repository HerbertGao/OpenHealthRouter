"""任务 1.2–1.5：schema 版本、PRAGMA 回读、迁移失败与历史版本升级。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from custom_components.openhealthrouter.migrations import (
    MIGRATIONS,
    apply_migrations,
    current_version,
)
from custom_components.openhealthrouter.store import EventStore, StoreUnavailable

_NEXT_VERSION = max(version for version, _ in MIGRATIONS) + 1


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def _indexes(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    return {row[0] for row in rows}


def _broken_migration(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE broken_probe (id INTEGER)")
    raise RuntimeError("迁移故意失败")


def test_empty_db_initialises_to_current_version(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    store = EventStore.open(db)
    try:
        assert store.schema_version == len(MIGRATIONS)
        conn = sqlite3.connect(db)
        try:
            assert "events" in _tables(conn)
            assert {
                "idx_events_subject_metric_start",
                "idx_events_metric_start",
                "idx_events_correction",
            } <= _indexes(conn)
        finally:
            conn.close()
    finally:
        store.close()


def test_second_open_does_not_remigrate(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    EventStore.open(db).close()
    store = EventStore.open(db)
    try:
        assert store.schema_version == len(MIGRATIONS)
    finally:
        store.close()


def test_pragmas_are_applied_and_read_back(tmp_path: Path) -> None:
    store = EventStore.open(tmp_path / "events.db")
    try:
        conn = store._new_connection()  # noqa: SLF001 - 测试需要直接观察 PRAGMA
        try:
            assert (
                str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
            )
            assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
            assert int(conn.execute("PRAGMA busy_timeout").fetchone()[0]) == 5000
            assert int(conn.execute("PRAGMA synchronous").fetchone()[0]) == 2
        finally:
            conn.close()
    finally:
        store.close()


def test_file_permissions_restricted(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "events.db"
    store = EventStore.open(db)
    try:
        assert db.stat().st_mode & 0o777 == 0o600
        assert db.parent.stat().st_mode & 0o777 == 0o700
    finally:
        store.close()


def test_foreign_key_pragma_is_noop_inside_transaction(tmp_path: Path) -> None:
    """记录 design D2 的事实：事务内设置 foreign_keys 静默无效。"""
    conn = sqlite3.connect(tmp_path / "probe.db", isolation_level=None)
    try:
        conn.execute("BEGIN")
        conn.execute("PRAGMA foreign_keys=ON")
        assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 0
        conn.execute("COMMIT")
    finally:
        conn.close()


def test_migration_failure_rolls_back_and_blocks_writes(
    tmp_path: Path, make_event
) -> None:
    db = tmp_path / "events.db"
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        with pytest.raises(RuntimeError):
            apply_migrations(conn, [*MIGRATIONS, (_NEXT_VERSION, _broken_migration)])
        assert current_version(conn) == len(MIGRATIONS)
        assert "broken_probe" not in _tables(conn)
    finally:
        conn.close()

    store = EventStore.open(db)
    store._unavailable = "注入的不可用状态"  # noqa: SLF001 - 模拟迁移失败后的拒绝服务
    try:
        assert not store.is_available
        with pytest.raises(StoreUnavailable):
            store.insert_events([make_event()])
    finally:
        store.close()


def test_migration_failure_marks_store_unavailable(tmp_path: Path, monkeypatch) -> None:
    import custom_components.openhealthrouter.store as store_module

    def failing_apply(conn, migrations=tuple(MIGRATIONS)) -> int:
        raise RuntimeError("迁移故意失败")

    monkeypatch.setattr(store_module, "apply_migrations", failing_apply)
    store = EventStore.open(tmp_path / "events.db")
    try:
        assert not store.is_available
        with pytest.raises(StoreUnavailable):
            store.query()
    finally:
        store.close()


def test_fixed_migration_reruns_after_failure(tmp_path: Path, make_event) -> None:
    db = tmp_path / "events.db"
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        with pytest.raises(RuntimeError):
            apply_migrations(conn, [*MIGRATIONS, (_NEXT_VERSION, _broken_migration)])
        assert current_version(conn) == len(MIGRATIONS)
    finally:
        conn.close()

    def good_v2(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE INDEX idx_probe_v2 ON events (inserted_at_utc)")

    conn = sqlite3.connect(db, isolation_level=None)
    try:
        assert (
            apply_migrations(conn, [*MIGRATIONS, (_NEXT_VERSION, good_v2)])
            == _NEXT_VERSION
        )
        assert "idx_probe_v2" in _indexes(conn)
    finally:
        conn.close()

    store = EventStore.open(db)
    try:
        assert store.is_available
        store.insert_events([make_event()])
    finally:
        store.close()


def test_historical_version_upgrade_preserves_events(
    tmp_path: Path, make_event
) -> None:
    db = tmp_path / "events.db"
    store = EventStore.open(db)
    original = make_event()
    correction = make_event(
        value=73.1,
        provenance={"type": "derived.user"},
        correctionOf=original.event_id,
    )
    day_event = make_event(
        observedAt={
            "start": "2026-11-12",
            "precision": "day",
            "timezone": "Asia/Shanghai",
        },
        metric="body.fat_percentage",
        unit="%",
        value=18.5,
    )
    store.insert_events([original, correction, day_event])
    before_by_id = {
        e.event_id: e.model_dump(mode="json", by_alias=True)
        for e in (original, correction, day_event)
    }
    before_query, before_total = store.query()
    store.close()

    def add_v2(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE INDEX idx_probe_v2 ON events (inserted_at_utc)")

    conn = sqlite3.connect(db, isolation_level=None)
    try:
        assert (
            apply_migrations(conn, [*MIGRATIONS, (_NEXT_VERSION, add_v2)])
            == _NEXT_VERSION
        )
        assert int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]) == 3
    finally:
        conn.close()

    store = EventStore.open(db)
    try:
        # 版本已由测试注入的 v2 推进，重新打开不得回退
        assert store.schema_version >= len(MIGRATIONS)
        for event_id, payload in before_by_id.items():
            stored = store.get_event(event_id)
            assert stored is not None
            assert stored.event.model_dump(mode="json", by_alias=True) == payload
        after_query, after_total = store.query()
        assert after_total == before_total
        assert [e.event_id for e in after_query] == [e.event_id for e in before_query]
    finally:
        store.close()


def test_v1_upgrades_to_v2_and_keeps_events(tmp_path: Path, make_event) -> None:
    import sqlite3

    from custom_components.openhealthrouter.store import EventStore, dump_payload

    db = tmp_path / "events.db"
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        apply_migrations(conn, MIGRATIONS[:1])  # 只到 v1
        assert current_version(conn) == 1
        event = make_event()
        conn.execute(
            "INSERT INTO events (event_id, subject_ref, metric, observed_start_key, "
            "observed_end_key, precision, superseded, payload_json, inserted_at_utc) "
            "VALUES (?,?,?,?,?,?,0,?,?)",
            (
                event.event_id,
                event.subject_ref,
                event.metric,
                "2026-11-12T00:00:00.000000Z",
                None,
                "second",
                dump_payload(event),
                "2026-11-12T00:00:01.000000Z",
            ),
        )
    finally:
        conn.close()

    store = EventStore.open(db)
    try:
        assert store.schema_version == max(v for v, _ in MIGRATIONS)
        stored = store.get_event(event.event_id)
        assert stored is not None
        assert stored.event.value == event.value
        assert store.list_deliveries() == []
    finally:
        store.close()


def test_orphan_deliveries_table_does_not_break_reinstall(tmp_path: Path) -> None:
    """回滚后 user_version 停在 2；若版本被手工回退，IF NOT EXISTS 必须兜住。"""
    import sqlite3

    from custom_components.openhealthrouter.store import EventStore

    db = tmp_path / "events.db"
    EventStore.open(db).close()

    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute("PRAGMA user_version = 1")  # 伪造"表还在但版本回退"
    finally:
        conn.close()

    store = EventStore.open(db)
    try:
        assert store.is_available
        assert store.schema_version == max(v for v, _ in MIGRATIONS)
    finally:
        store.close()
