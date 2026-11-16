"""数据库 schema 版本与顺序迁移。

设计依据：openspec/changes/local-event-store/design.md D6。
每个迁移在显式 BEGIN IMMEDIATE 事务内执行 DDL/DML 并同事务推进 `PRAGMA user_version`
（实测：PRAGMA user_version 参与事务、会随回滚恢复；CREATE TABLE 在 legacy 隔离模式下
不自动进入事务，因此必须显式开启）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence

Migration = tuple[int, Callable[[sqlite3.Connection], None]]


def _migrate_v1(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE events (
            event_id           TEXT PRIMARY KEY,
            subject_ref        TEXT NOT NULL,
            metric             TEXT NOT NULL,
            observed_start_key TEXT NOT NULL,
            observed_end_key   TEXT,
            precision          TEXT NOT NULL,
            correction_of      TEXT REFERENCES events(event_id) ON DELETE SET NULL,
            superseded         INTEGER NOT NULL DEFAULT 0,
            payload_json       TEXT NOT NULL,
            inserted_at_utc    TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX idx_events_subject_metric_start "
        "ON events (subject_ref, metric, observed_start_key)"
    )
    conn.execute(
        "CREATE INDEX idx_events_metric_start ON events (metric, observed_start_key)"
    )
    conn.execute("CREATE INDEX idx_events_correction ON events (correction_of)")


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """只新增投递审计表与索引，不改 events 表。

    用 IF NOT EXISTS：卸载回滚后 user_version 停在 2，重装时该迁移不会重跑，
    但手工回退过版本号的环境仍可能遇到表已存在。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS deliveries (
            delivery_id     TEXT PRIMARY KEY,
            target_ref      TEXT NOT NULL,
            filter_digest   TEXT NOT NULL,
            since_key       TEXT,
            until_key       TEXT,
            event_count     INTEGER NOT NULL CHECK (event_count >= 0),
            event_ids_json  TEXT NOT NULL,
            status          TEXT NOT NULL
                            CHECK (status IN ('success','failed','skipped_no_events')),
            error_category  TEXT,
            created_at      TEXT NOT NULL,
            CHECK (status <> 'failed' OR error_category IS NOT NULL)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_deliveries_created_at "
        "ON deliveries (created_at)"
    )


MIGRATIONS: list[Migration] = [(1, _migrate_v1), (2, _migrate_v2)]


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def apply_migrations(
    conn: sqlite3.Connection, migrations: Sequence[Migration] = tuple(MIGRATIONS)
) -> int:
    """按版本升序应用未应用的迁移，返回最终版本。

    每个迁移单独一个事务：失败时回滚该迁移的全部变更（含版本号），异常向上抛。
    """
    version = current_version(conn)
    for target, migrate in sorted(migrations, key=lambda item: item[0]):
        if target <= version:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            migrate(conn)
            # PRAGMA 不支持参数绑定，版本号来自代码内受控整数序列
            conn.execute(f"PRAGMA user_version = {int(target)}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        version = target
    return version
