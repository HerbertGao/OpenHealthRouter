"""本地健康事件存储（SQLite）。

设计依据：openspec/changes/local-event-store/design.md（D1–D10）。
同步实现是唯一真相；异步门面见 async_store.py。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .migrations import apply_migrations, current_version
from .models import HealthEvent

_LOGGER = logging.getLogger(__name__)

KEY_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
BUSY_TIMEOUT_MS = 5000
_SYNCHRONOUS_FULL = 2
_CONNECT_TIMEOUT_S = BUSY_TIMEOUT_MS / 1000


class StoreError(Exception):
    """存储层可报告的失败（含不可写、校验失败、约束冲突）。"""


class StoreUnavailable(StoreError):
    """存储处于不可用状态（例如迁移失败后拒绝服务）。"""


@dataclass(frozen=True)
class InsertResult:
    created: int
    duplicates: int


@dataclass(frozen=True)
class DeleteResult:
    deleted: int
    not_found: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StoredEvent:
    event: HealthEvent
    superseded: bool


@dataclass(frozen=True)
class QueryFilters:
    subject_ref: str | None = None
    metric: str | None = None
    metrics: frozenset[str] | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 100
    offset: int = 0
    include_superseded: bool = False


@dataclass(frozen=True)
class Page:
    """键集分页结果：cursor 是最后一行原始行的排序键（即使该行被上层过滤）。"""

    events: list[HealthEvent]
    cursor: tuple[str, str, str] | None
    has_more: bool


@dataclass(frozen=True)
class DeliveryRecord:
    delivery_id: str
    target_ref: str
    filter_digest: str
    since_key: str | None
    until_key: str | None
    event_count: int
    event_ids: list[str]
    status: str
    error_category: str | None
    created_at: str


def _where_parts(filters: QueryFilters) -> tuple[list[str], list[Any]]:
    """唯一 where 构件的来源：query() 与 fetch_page() 必须共用它，避免语义漂移。"""
    where: list[str] = []
    params: list[Any] = []
    if not filters.include_superseded:
        where.append("superseded = 0")
    if filters.subject_ref is not None:
        where.append("subject_ref = ?")
        params.append(filters.subject_ref)
    if filters.metric is not None:
        where.append("metric = ?")
        params.append(filters.metric)
    if filters.metrics is not None:
        if not filters.metrics:
            where.append("0")
        else:
            placeholders = ",".join("?" for _ in filters.metrics)
            where.append(f"metric IN ({placeholders})")
            params.extend(sorted(filters.metrics))
    if filters.since is not None:
        where.append(
            "((observed_end_key IS NULL AND observed_start_key >= ?) "
            "OR (observed_end_key IS NOT NULL AND observed_end_key > ?))"
        )
        since_key = time_key(filters.since)
        params.extend([since_key, since_key])
    if filters.until is not None:
        where.append("observed_start_key < ?")
        params.append(time_key(filters.until))
    return where, params


def time_key(moment: datetime) -> str:
    """单一 normalizer：任何时刻都转成同构的 UTC 键字符串。"""
    if moment.tzinfo is None:
        raise StoreError("时间键必须是带时区的时间")
    return moment.astimezone(UTC).strftime(KEY_FORMAT)


def _local_midnight(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=zone)


def observation_keys(event: HealthEvent) -> tuple[str, str | None]:
    """返回 (start_key, end_key)；瞬时事件 end_key 为 None。

    日期级观察按事件时区逐日期解析当地午夜（遵循 DST），不做固定偏移推算。
    """
    observed = event.observed_at
    if observed.precision == "day":
        zone = ZoneInfo(observed.timezone)
        start_day = observed.start
        if isinstance(start_day, datetime) or not isinstance(start_day, date):
            raise StoreError("日期级观察时间必须是纯日期")
        last_day = observed.end if observed.end is not None else start_day
        if isinstance(last_day, datetime) or not isinstance(last_day, date):
            raise StoreError("日期级观察区间两端必须是纯日期")
        start_key = time_key(_local_midnight(start_day, zone))
        exclusive = _local_midnight(last_day + timedelta(days=1), zone)
        return start_key, time_key(exclusive)
    start_at = observed.start
    if not isinstance(start_at, datetime):
        raise StoreError("非日期级观察时间必须是带时区的日期时间")
    end_key: str | None = None
    if observed.end is not None:
        if not isinstance(observed.end, datetime):
            raise StoreError("非日期级观察区间的终点必须是带时区的日期时间")
        end_key = time_key(observed.end)
    return time_key(start_at), end_key


def dump_payload(event: HealthEvent) -> str:
    """规范载荷字节：写入与删除改写共用，保证两条路径产物逐字节一致。"""
    return json.dumps(
        event.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def load_payload(payload: str) -> HealthEvent:
    """载荷反序列化；损坏数据归类为存储错误而不是让契约异常外泄。"""
    try:
        return HealthEvent.model_validate(json.loads(payload))
    except (ValueError, TypeError) as exc:
        raise StoreError(f"载荷无法解析: {type(exc).__name__}") from exc


class EventStore:
    """单文件 SQLite 事件库。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._unavailable: str | None = None
        self._closed = False
        self._own_dir = False
        # D2：同步层写锁，跨线程唯一写者；与异步门面的 asyncio.Lock 不可互相替代
        self._write_lock = threading.Lock()

    # ---------- 生命周期 ----------

    @classmethod
    def open(cls, path: str | Path) -> EventStore:
        store = cls(Path(path))
        try:
            store._prepare_files()
            with closing(store._new_connection()) as conn:
                apply_migrations(conn)
            store._restrict_permissions()
        except Exception as exc:  # noqa: BLE001 - 任何初始化失败都必须进入拒绝服务状态
            store._unavailable = f"{type(exc).__name__}: {exc}"
            _LOGGER.error("存储初始化失败，后续调用将被拒绝: %s", type(exc).__name__)
        return store

    @property
    def is_available(self) -> bool:
        return self._unavailable is None and not self._closed

    @property
    def schema_version(self) -> int:
        with closing(self._new_connection()) as conn:
            return current_version(conn)

    def close(self) -> None:
        self._closed = True

    def _prepare_files(self) -> None:
        parent = self.path.parent
        self._own_dir = not parent.exists()
        parent.mkdir(parents=True, exist_ok=True)
        # 仅在缺失时预创建 0600：否则 -wal/-shm 会继承 umask 下的宽松模式
        _create_private_if_missing(self.path)

    def _restrict_permissions(self) -> None:
        """库文件与 WAL 附属文件每次打开都收紧；父目录仅在本模块创建时才收紧。"""
        if self._own_dir:
            self.path.parent.chmod(0o700)
        for path in (self.path, *_wal_sidecars(self.path)):
            _tighten_to_0600(path)

    def _new_connection(self) -> sqlite3.Connection:
        """每个操作一个连接：连接不跨线程（sqlite3 的线程约束）。

        设计依据 D2：连接不跨线程复用，异步门面的 to_thread 因此是安全的。
        """
        if self._unavailable is not None:
            raise StoreUnavailable(self._unavailable)
        if self._closed:
            raise StoreUnavailable("存储已关闭")
        created = not self.path.exists()
        conn = sqlite3.connect(
            self.path, isolation_level=None, timeout=_CONNECT_TIMEOUT_S
        )
        try:
            self._apply_pragmas(conn)
        except BaseException:
            conn.close()
            raise
        if created:
            self.path.chmod(0o600)
        return conn

    @staticmethod
    def _apply_pragmas(conn: sqlite3.Connection) -> None:
        """设置并回读校验：事务内设置 foreign_keys/journal_mode 是静默无效的。"""
        journal = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(journal).lower() != "wal":
            raise StoreError(f"journal_mode 未能设为 WAL（实际 {journal}）")
        conn.execute("PRAGMA foreign_keys=ON")
        if int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise StoreError("foreign_keys 未能启用")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        if int(conn.execute("PRAGMA busy_timeout").fetchone()[0]) != BUSY_TIMEOUT_MS:
            raise StoreError("busy_timeout 未能生效")
        conn.execute("PRAGMA synchronous=FULL")
        if int(conn.execute("PRAGMA synchronous").fetchone()[0]) != _SYNCHRONOUS_FULL:
            raise StoreError("synchronous 未能设为 FULL")

    # ---------- 写入 ----------

    def insert_events(self, events: list[HealthEvent]) -> InsertResult:
        return self._write(self._insert_many, events)

    def _insert_many(
        self, conn: sqlite3.Connection, events: list[HealthEvent]
    ) -> InsertResult:
        created = 0
        duplicates = 0
        for event in events:
            if self._insert_one(conn, event):
                created += 1
            else:
                duplicates += 1
        return InsertResult(created=created, duplicates=duplicates)

    def _insert_one(self, conn: sqlite3.Connection, event: HealthEvent) -> bool:
        start_key, end_key = observation_keys(event)
        try:
            cursor = conn.execute(
                """
                INSERT INTO events (
                    event_id, subject_ref, metric, observed_start_key, observed_end_key,
                    precision, correction_of, superseded, payload_json, inserted_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(event_id) DO NOTHING
                """,
                (
                    event.event_id,
                    event.subject_ref,
                    event.metric,
                    start_key,
                    end_key,
                    event.observed_at.precision,
                    event.correction_of,
                    dump_payload(event),
                    time_key(datetime.now(UTC)),
                ),
            )
        except sqlite3.IntegrityError as exc:
            _LOGGER.error(
                "写入被拒绝 event_id=%s metric=%s reason=%s",
                event.event_id,
                event.metric,
                type(exc).__name__,
            )
            raise StoreError(
                f"写入被拒绝（修正目标不存在或约束冲突）: {event.correction_of}"
            ) from exc
        if cursor.rowcount == 0:
            return False
        if event.correction_of:
            conn.execute(
                "UPDATE events SET superseded = 1 WHERE event_id = ?",
                (event.correction_of,),
            )
        return True

    # ---------- 读取 ----------

    def query(
        self, filters: QueryFilters | None = None
    ) -> tuple[list[HealthEvent], int]:
        filters = filters or QueryFilters()
        where, params = _where_parts(filters)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        count_sql = f"SELECT COUNT(*) FROM events {clause}"
        page_sql = (
            f"SELECT payload_json FROM events {clause} "
            "ORDER BY observed_start_key DESC, inserted_at_utc DESC, event_id "
            "LIMIT ? OFFSET ?"
        )
        with closing(self._new_connection()) as conn:
            total = int(conn.execute(count_sql, params).fetchone()[0])
            rows = conn.execute(
                page_sql, [*params, filters.limit, filters.offset]
            ).fetchall()
        return [load_payload(row[0]) for row in rows], total

    def fetch_page(
        self,
        filters: QueryFilters,
        *,
        after: tuple[str, str, str] | None = None,
        limit: int = 200,
    ) -> Page:
        """键集分页：游标用显式混合比较，禁止行值比较（排序末位是升序）。"""
        where, params = _where_parts(filters)
        if after is not None:
            start_key, inserted, event_id = after
            where.append(
                "(observed_start_key < ? "
                "OR (observed_start_key = ? "
                "AND (inserted_at_utc < ? "
                "OR (inserted_at_utc = ? AND event_id > ?))))"
            )
            params.extend([start_key, start_key, inserted, inserted, event_id])
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        sql = (
            "SELECT payload_json, observed_start_key, inserted_at_utc, event_id "
            f"FROM events {clause} "
            "ORDER BY observed_start_key DESC, inserted_at_utc DESC, event_id "
            "LIMIT ?"
        )
        with closing(self._new_connection()) as conn:
            rows = conn.execute(sql, [*params, limit + 1]).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        events = [load_payload(row[0]) for row in rows]
        cursor = (rows[-1][1], rows[-1][2], rows[-1][3]) if rows else after
        return Page(events=events, cursor=cursor, has_more=has_more)

    def get_event(self, event_id: str) -> StoredEvent | None:
        with closing(self._new_connection()) as conn:
            sql = "SELECT payload_json, superseded FROM events WHERE event_id = ?"
            row = conn.execute(sql, (event_id,)).fetchone()
        if row is None:
            return None
        return StoredEvent(event=load_payload(row[0]), superseded=bool(row[1]))

    # ---------- 删除 ----------

    def delete_events(self, event_ids: list[str]) -> DeleteResult:
        if not event_ids:
            return DeleteResult(deleted=0, not_found=[])
        return self._write(self._delete_many, event_ids)

    def _delete_many(
        self, conn: sqlite3.Connection, event_ids: list[str]
    ) -> DeleteResult:
        placeholders = ",".join("?" for _ in event_ids)
        existing = {
            row[0]
            for row in conn.execute(
                f"SELECT event_id FROM events WHERE event_id IN ({placeholders})",
                event_ids,
            )
        }
        dependents = [
            row[0]
            for row in conn.execute(
                f"SELECT event_id FROM events WHERE correction_of IN ({placeholders})",
                event_ids,
            )
        ]
        cursor = conn.execute(
            f"DELETE FROM events WHERE event_id IN ({placeholders})", event_ids
        )
        deleted = int(cursor.rowcount)
        for dependent_id in dependents:
            self._clear_correction_reference(conn, dependent_id)
        not_found = [event_id for event_id in event_ids if event_id not in existing]
        return DeleteResult(deleted=deleted, not_found=not_found)

    @staticmethod
    def _clear_correction_reference(conn: sqlite3.Connection, event_id: str) -> None:
        """列与载荷必须一起清；载荷走契约模型往返，保持规范字节与 eventId 不变。"""
        row = conn.execute(
            "SELECT payload_json FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            return
        cleared = load_payload(row[0]).model_copy(update={"correction_of": None})
        conn.execute(
            "UPDATE events SET payload_json = ? WHERE event_id = ?",
            (dump_payload(cleared), event_id),
        )

    # ---------- 备份与恢复 ----------

    def backup_to(self, target: str | Path) -> Path:
        with self._write_lock:
            with closing(self._new_connection()) as conn:
                return self._backup(conn, Path(target))

    def _backup(self, conn: sqlite3.Connection, target_path: Path) -> Path:
        if target_path.resolve() == self.path.resolve():
            raise StoreError("备份目标不能是源库本身")
        try:
            return self._backup_unchecked(conn, target_path)
        except StoreError:
            raise
        except (sqlite3.Error, OSError) as exc:
            _LOGGER.error("备份失败 reason=%s", type(exc).__name__)
            raise StoreError(f"备份失败: {type(exc).__name__}") from exc

    def _backup_unchecked(self, conn: sqlite3.Connection, target_path: Path) -> Path:
        """先写两份 .part，全部成功后才原子替换；任一步失败都不动上一份备份。"""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        part_path = target_path.with_name(target_path.name + ".part")
        meta_path = _meta_path(target_path)
        meta_part = meta_path.with_name(meta_path.name + ".part")
        try:
            _create_private_if_missing(part_path)
            _tighten_to_0600(part_path)
            dest = sqlite3.connect(part_path)
            try:
                conn.backup(dest)
                # 计数取自快照本身：与备份同源，不会因中间提交而与 meta 不一致
                snapshot_count = int(
                    dest.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                )
            finally:
                dest.close()

            meta = {
                "event_count": snapshot_count,
                "user_version": self.schema_version,
                "backed_up_at": time_key(datetime.now(UTC)),
            }
            _create_private_if_missing(meta_part)
            _tighten_to_0600(meta_part)
            meta_part.write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")

            # 两个 rename 都在所有可能失败的操作之后
            os.replace(part_path, target_path)
            os.replace(meta_part, meta_path)
        except BaseException:
            for leftover in (part_path, meta_part):
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        return target_path

    def record_delivery(self, record: DeliveryRecord) -> None:
        """写入投递审计：走与事件相同的单写者事务路径。"""
        self._write(_insert_delivery_row, record)

    def list_deliveries(self, limit: int = 50) -> list[dict[str, Any]]:
        """按创建时间倒序读取投递审计（不含任何载荷字段）。"""
        sql = (
            "SELECT delivery_id, target_ref, filter_digest, since_key, until_key, "
            "event_count, event_ids_json, status, error_category, created_at "
            "FROM deliveries ORDER BY created_at DESC, delivery_id DESC LIMIT ?"
        )
        with closing(self._new_connection()) as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [
            {
                "delivery_id": row[0],
                "target_ref": row[1],
                "filter_digest": row[2],
                "since_key": row[3],
                "until_key": row[4],
                "event_count": row[5],
                "event_ids": _load_event_ids(row[6]),
                "status": row[7],
                "error_category": row[8],
                "created_at": row[9],
            }
            for row in rows
        ]

    def _write(self, operation, *args):  # noqa: ANN001, ANN201 - 内部事务包装
        """写锁 + 独立连接 + 显式事务；失败统一映射为 StoreError。"""
        with self._write_lock:
            with closing(self._new_connection()) as conn:
                try:
                    _begin_write(conn)
                    result = operation(conn, *args)
                    conn.execute("COMMIT")
                    return result
                except sqlite3.OperationalError as exc:
                    _safe_rollback(conn)
                    _LOGGER.error("写入失败 reason=%s", type(exc).__name__)
                    raise StoreError(f"写入失败: {type(exc).__name__}: {exc}") from exc
                except BaseException:
                    _safe_rollback(conn)
                    raise

    @staticmethod
    def verify_integrity(path: str | Path, expected_count: int | None = None) -> None:
        """校验顺序固定：完整性 → 外键 → 期望值解析 → 真实读计数 → 业务不变量。"""
        db_path = Path(path)
        if not db_path.exists():
            raise StoreError(f"备份文件不存在: {db_path.name}")
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(db_path, isolation_level=None)
            conn.execute("PRAGMA foreign_keys=ON")
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if result is None or str(result[0]).lower() != "ok":
                raise StoreError(f"完整性检查失败: {result}")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise StoreError("外键检查失败")
            if expected_count is None:
                expected = _read_meta_count(db_path)
            else:
                expected = int(expected_count)
            actual = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            if actual != expected:
                raise StoreError(f"事件计数与期望值不符: {actual} != {expected}")
            _check_business_invariants(conn)
        except sqlite3.DatabaseError as exc:
            raise StoreError(f"恢复校验失败: {type(exc).__name__}: {exc}") from exc
        finally:
            if conn is not None:
                conn.close()


def _meta_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".meta.json")


def _wal_sidecars(db_path: Path) -> tuple[Path, Path]:
    return (
        Path(str(db_path) + "-wal"),
        Path(str(db_path) + "-shm"),
    )


def _create_private_if_missing(path: Path) -> None:
    """缺失时以 0600 创建；已存在则不动（避免把用户设的 0400 改回可写）。"""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def _tighten_to_0600(path: Path) -> None:
    """只收紧、不放宽：存在组/其他位权限时降到 0600；已是更严模式则不动。"""
    if not path.exists():
        return
    if path.stat().st_mode & 0o077:
        path.chmod(0o600)


def _begin_write(conn: sqlite3.Connection) -> None:
    """显式开写事务：legacy 隔离模式下 DDL/DML 不自动进事务。"""
    conn.execute("BEGIN IMMEDIATE")


def _safe_rollback(conn: sqlite3.Connection) -> None:
    """仅当处于事务中才回滚；BEGIN 本身失败时不应再抛二次异常。"""
    if conn.in_transaction:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            _LOGGER.error("回滚失败，事务状态未知")


def _read_meta_count(db_path: Path) -> int:
    meta_path = _meta_path(db_path)
    if not meta_path.exists():
        raise StoreError(f"期望值缺失: 找不到 {meta_path.name}")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return int(meta["event_count"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise StoreError(f"期望值不可读: {meta_path.name}") from exc


def _check_business_invariants(conn: sqlite3.Connection) -> None:
    dangling = conn.execute(
        "SELECT COUNT(*) FROM events e WHERE e.correction_of IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM events t WHERE t.event_id = e.correction_of)"
    ).fetchone()[0]
    if int(dangling):
        raise StoreError(f"存在悬空修正引用: {int(dangling)} 条")
    rows = conn.execute("SELECT payload_json FROM events").fetchall()
    parseable = 0
    for row in rows:
        try:
            HealthEvent.model_validate(json.loads(row[0]))
            parseable += 1
        except ValueError, TypeError:
            continue
    if parseable != len(rows):
        raise StoreError(f"载荷不可解析: {len(rows) - parseable} 条")
    day_sql = (
        "SELECT COUNT(*) FROM events "
        "WHERE precision = 'day' AND observed_end_key IS NULL"
    )
    missing_end = int(conn.execute(day_sql).fetchone()[0])
    if missing_end:
        raise StoreError(f"日期级事件缺少区间上界: {missing_end} 条")


def _insert_delivery_row(conn: sqlite3.Connection, record: DeliveryRecord) -> None:
    """插入一条投递审计；约束冲突归类为存储错误而不是漏出驱动异常。"""
    try:
        conn.execute(
            """
            INSERT INTO deliveries (
                delivery_id, target_ref, filter_digest, since_key, until_key,
                event_count, event_ids_json, status, error_category, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.delivery_id,
                record.target_ref,
                record.filter_digest,
                record.since_key,
                record.until_key,
                record.event_count,
                json.dumps(record.event_ids, separators=(",", ":")),
                record.status,
                record.error_category,
                record.created_at,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise StoreError(f"投递记录被拒绝: {type(exc).__name__}") from exc


def _load_event_ids(raw: str) -> list[str]:
    """投递记录里的 ID 列表；损坏数据归类为存储错误而不是让 JSON 异常外泄。"""
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise StoreError(f"投递记录损坏: {type(exc).__name__}") from exc
    if not isinstance(parsed, list):
        raise StoreError("投递记录损坏: event_ids 不是列表")
    return [str(item) for item in parsed]


def log_name(path: Path) -> str:
    """诊断用：仅暴露文件名，不暴露完整路径。"""
    return os.path.basename(str(path))
