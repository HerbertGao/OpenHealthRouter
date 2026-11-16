"""任务 6.1–6.3：一致性备份、恢复校验顺序与损坏副本拒绝。

注意：本实现每次操作用独立连接，最后一个连接关闭时会 checkpoint。要复现"朴素复制丢
未合并日志"的危险场景，测试必须自己持有一个外部连接让 WAL 保持存活（真实场景同理：
进程开着库时用户手工复制文件）。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from custom_components.openhealthrouter.store import EventStore, StoreError


def _seed(store: EventStore, make_event) -> list[str]:
    events = [
        make_event(),
        make_event(
            value=74.2,
            observedAt={
                "start": "2026-11-13T08:00:00+08:00",
                "precision": "second",
                "timezone": "Asia/Shanghai",
            },
        ),
    ]
    store.insert_events(events)
    return [event.event_id for event in events]


def test_backup_contains_committed_events_with_uncheckpointed_wal(
    tmp_path: Path, make_event
) -> None:
    db = tmp_path / "events.db"
    store = EventStore.open(db)
    holder = sqlite3.connect(db, isolation_level=None)
    try:
        holder.execute("PRAGMA wal_autocheckpoint=0")
        holder.execute("SELECT count(*) FROM sqlite_master").fetchone()
        ids = _seed(store, make_event)
        assert (tmp_path / "events.db-wal").exists()

        backup = store.backup_to(tmp_path / "backup.db")

        meta_path = tmp_path / "backup.db.meta.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["event_count"] == len(ids)
        assert meta["user_version"] == store.schema_version
        assert meta_path.stat().st_mode & 0o777 == 0o600
        assert backup.stat().st_mode & 0o777 == 0o600

        restored = EventStore.open(backup)
        try:
            for event_id in ids:
                assert restored.get_event(event_id) is not None
        finally:
            restored.close()
    finally:
        holder.close()
        store.close()


def test_verify_passes_on_good_backup(tmp_path: Path, make_event) -> None:
    store = EventStore.open(tmp_path / "events.db")
    try:
        _seed(store, make_event)
        backup = store.backup_to(tmp_path / "backup.db")
    finally:
        store.close()
    EventStore.verify_integrity(backup)


def test_verify_rejects_corrupt_file(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is not a sqlite database")
    with pytest.raises(StoreError):
        EventStore.verify_integrity(corrupt, expected_count=1)


def test_plain_copy_without_meta_reports_missing_expectation(
    tmp_path: Path, make_event
) -> None:
    db = tmp_path / "events.db"
    store = EventStore.open(db)
    holder = sqlite3.connect(db, isolation_level=None)
    try:
        holder.execute("PRAGMA wal_autocheckpoint=0")
        _seed(store, make_event)
        copy = tmp_path / "copy.db"
        shutil.copy(db, copy)
        with pytest.raises(StoreError, match="期望值缺失"):
            EventStore.verify_integrity(copy)
    finally:
        holder.close()
        store.close()


def test_plain_copy_with_matching_meta_still_fails_on_real_read(
    tmp_path: Path, make_event
) -> None:
    """真实读是必需项：meta 与源库一致也不能通过，因为副本里没有那些行。"""
    db = tmp_path / "events.db"
    store = EventStore.open(db)
    holder = sqlite3.connect(db, isolation_level=None)
    try:
        holder.execute("PRAGMA wal_autocheckpoint=0")
        holder.execute("SELECT count(*) FROM sqlite_master").fetchone()
        ids = _seed(store, make_event)
        copy = tmp_path / "copy.db"
        shutil.copy(db, copy)

        probe = sqlite3.connect(copy)
        try:
            copied_rows = int(
                probe.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            )
        finally:
            probe.close()
        assert copied_rows < len(ids)  # 副本确实缺少未合并日志里的行

        meta_path = tmp_path / "copy.db.meta.json"
        meta_path.write_text(
            json.dumps({"event_count": len(ids), "user_version": 1}),
            encoding="utf-8",
        )
        with pytest.raises(StoreError, match="计数与期望值不符"):
            EventStore.verify_integrity(copy)
    finally:
        holder.close()
        store.close()


def test_verify_rejects_count_mismatch(tmp_path: Path, make_event) -> None:
    store = EventStore.open(tmp_path / "events.db")
    try:
        _seed(store, make_event)
        backup = store.backup_to(tmp_path / "backup.db")
    finally:
        store.close()
    with pytest.raises(StoreError, match="计数与期望值不符"):
        EventStore.verify_integrity(backup, expected_count=99)


def test_verify_rejects_day_row_without_end_key(tmp_path: Path, make_event) -> None:
    store = EventStore.open(tmp_path / "events.db")
    try:
        _seed(store, make_event)
        backup = store.backup_to(tmp_path / "backup.db")
    finally:
        store.close()

    conn = sqlite3.connect(backup)
    try:
        conn.execute(
            "UPDATE events SET precision = 'day', observed_end_key = NULL "
            "WHERE rowid = (SELECT MIN(rowid) FROM events)"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(StoreError, match="日期级事件缺少区间上界"):
        EventStore.verify_integrity(backup, expected_count=2)


def test_restore_requires_verification_before_use(tmp_path: Path, make_event) -> None:
    store = EventStore.open(tmp_path / "events.db")
    try:
        ids = _seed(store, make_event)
        backup = store.backup_to(tmp_path / "backup.db")
    finally:
        store.close()

    EventStore.verify_integrity(backup)
    restored = EventStore.open(backup)
    try:
        assert restored.is_available
        for event_id in ids:
            assert restored.get_event(event_id) is not None
    finally:
        restored.close()

    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database")
    with pytest.raises(StoreError):
        EventStore.verify_integrity(broken, expected_count=len(ids))


def test_meta_count_matches_snapshot_even_under_concurrent_writes(
    tmp_path: Path, make_event
) -> None:
    """计数取自快照本身：并发写入不会让合法备份被误判损坏。"""
    import threading

    db = tmp_path / "events.db"
    store = EventStore.open(db)
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer(index: int) -> None:
        own = EventStore.open(db)
        try:
            while not stop.is_set():
                own.insert_events(
                    [
                        make_event(
                            value=70.0 + index,
                            observedAt={
                                "start": f"2026-11-12T08:{index:02d}:00+08:00",
                                "precision": "second",
                                "timezone": "Asia/Shanghai",
                            },
                        )
                    ]
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            own.close()

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(3)]
    for thread in threads:
        thread.start()
    try:
        backup = store.backup_to(tmp_path / "backup.db")
    finally:
        stop.set()
        for thread in threads:
            thread.join()
        store.close()

    assert errors == []
    EventStore.verify_integrity(backup)


def test_permissions_tightened_for_preexisting_loose_db(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    EventStore.open(db).close()
    db.chmod(0o644)
    tmp_path.chmod(0o755)

    EventStore.open(db).close()

    assert db.stat().st_mode & 0o777 == 0o600
    # 父目录不是本模块创建的（共享目录），不得被静默降权
    assert tmp_path.stat().st_mode & 0o777 == 0o755


def test_owned_directory_is_restricted(tmp_path: Path) -> None:
    db = tmp_path / "openhealthrouter" / "events.db"
    EventStore.open(db).close()
    assert db.parent.stat().st_mode & 0o777 == 0o700
    assert db.stat().st_mode & 0o777 == 0o600


def test_backup_target_cannot_be_source(tmp_path: Path, make_event) -> None:
    db = tmp_path / "events.db"
    store = EventStore.open(db)
    try:
        _seed(store, make_event)
        with pytest.raises(StoreError, match="不能是源库"):
            store.backup_to(db)
    finally:
        store.close()


def test_backup_failure_is_reported_as_store_error(tmp_path: Path, make_event) -> None:
    store = EventStore.open(tmp_path / "events.db")
    readonly_dir = tmp_path / "readonly"
    readonly_dir.mkdir()
    readonly_dir.chmod(0o500)
    try:
        _seed(store, make_event)
        with pytest.raises(StoreError, match="备份失败"):
            store.backup_to(readonly_dir / "backup.db")
    finally:
        readonly_dir.chmod(0o700)
        store.close()


def test_wal_sidecars_are_private(tmp_path: Path, make_event) -> None:
    db = tmp_path / "events.db"
    store = EventStore.open(db)
    holder = sqlite3.connect(db, isolation_level=None)
    try:
        holder.execute("PRAGMA wal_autocheckpoint=0")
        holder.execute("SELECT count(*) FROM sqlite_master").fetchone()
        _seed(store, make_event)
        EventStore.open(db).close()  # 重新打开会再收紧一次
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(db) + suffix)
            if path.exists():
                assert path.stat().st_mode & 0o777 == 0o600, suffix
    finally:
        holder.close()
        store.close()


def test_failed_backup_keeps_previous_snapshot_intact(
    tmp_path: Path, make_event
) -> None:
    """备份失败时上一份快照必须完好：写入走 .part + 原子替换。"""
    store = EventStore.open(tmp_path / "events.db")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    target = out_dir / "backup.db"
    try:
        _seed(store, make_event)
        store.backup_to(target)
        good_snapshot = target.read_bytes()
        good_meta = (out_dir / "backup.db.meta.json").read_bytes()

        out_dir.chmod(0o500)
        try:
            with pytest.raises(StoreError, match="备份失败"):
                store.backup_to(target)
        finally:
            out_dir.chmod(0o700)

        assert target.read_bytes() == good_snapshot
        assert (out_dir / "backup.db.meta.json").read_bytes() == good_meta
        assert not (out_dir / "backup.db.part").exists()
    finally:
        out_dir.chmod(0o700)
        store.close()

    EventStore.verify_integrity(target)


def test_verify_rejects_missing_file_without_creating_it(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    with pytest.raises(StoreError, match="不存在"):
        EventStore.verify_integrity(missing, expected_count=1)
    assert not missing.exists()


def test_meta_write_failure_happens_before_replace(tmp_path: Path, make_event) -> None:
    """meta 段失败必须发生在替换之前：上一份快照与 meta 都不能被动过。"""
    store = EventStore.open(tmp_path / "events.db")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    target = out_dir / "backup.db"
    meta_path = out_dir / "backup.db.meta.json"
    try:
        _seed(store, make_event)
        store.backup_to(target)
        good_snapshot = target.read_bytes()
        good_meta = meta_path.read_bytes()

        # 让 meta 的 .part 无法创建（同名目录占位）→ 失败点在替换之前
        (out_dir / "backup.db.meta.json.part").mkdir()

        with pytest.raises(StoreError, match="备份失败"):
            store.backup_to(target)

        assert target.read_bytes() == good_snapshot
        assert meta_path.read_bytes() == good_meta
        assert not (out_dir / "backup.db.part").exists()
    finally:
        store.close()

    EventStore.verify_integrity(target)
