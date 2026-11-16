# Design

## Context

参见 proposal.md - Why。现状约束：`models.py` 已提供不可变、已校验的 `HealthEvent`（含两模式 `eventId` 自算，`correctionOf` 不参与身份）；`docs/technology-stack.md` §5 已冻结 SQLite 决策；第 2 期尚无 HA 实体与 Config Entry，存储必须能在纯 pytest 环境下以显式路径打开。`tests/storage/` 已存在但为空。

## Goals / Non-Goals

**Goals:**

- 存储对外只接受已校验的 `HealthEvent`，内部不再重复校验契约。
- 正确性优先于吞吐：宁可串行写，也不允许"报告成功但未落盘"。
- 迁移与恢复的失败路径可被测试完整演练。
- 时间范围查询对日期级、瞬时、区间三类观察时间语义一致且无静默漏检。

**Non-Goals:**

- 不建投递表、审计表、游标表（第 4 期按导出需求追加迁移）。
- 不做保留期清理、条数上限、归档（`docs/privacy.md` §6 明确不静默淘汰）。
- 不做性能遥测、连接池、后台写入线程、第二套队列。
- 不实现 SQLite 之外的存储，不做多进程/多机共享。
- 不承诺物理擦除；不重写既有备份与已导出副本。
- 不做组件级（`components[].metric`）指标过滤、全文检索、跨库查询。

## Decisions

### D1: 单表 `events`，只给过滤与排序需要的字段建列

```text
events(
  event_id           TEXT PRIMARY KEY,
  subject_ref        TEXT NOT NULL,
  metric             TEXT NOT NULL,          -- 顶层 metric
  observed_start_key TEXT NOT NULL,          -- UTC 瞬时键（见 D5）
  observed_end_key   TEXT,                   -- UTC 瞬时上界；瞬时事件为 NULL
  precision          TEXT NOT NULL,
  correction_of      TEXT REFERENCES events(event_id) ON DELETE SET NULL,
  superseded         INTEGER NOT NULL DEFAULT 0,
  payload_json       TEXT NOT NULL,          -- camelCase 规范事件全文
  inserted_at_utc    TEXT NOT NULL           -- 写入时刻，分页 tie-break
)
```

索引：`(subject_ref, metric, observed_start_key)`、`(metric, observed_start_key)`、`(correction_of)`。其余信息一律留在 `payload_json`。

- 备选：把 `schema_version`、`recorded_at_key`、`quality_status`、`device_ref`、来源索引也建成列 → 拒绝：本期没有任何查询或排序使用它们，属于"为以后加"；第 4/5 期出现真实查询时随迁移追加。

### D2: 同步层 `threading.Lock` 串行写，异步层只做排队；PRAGMA 必须回读验证

每个操作在独立连接上完成完整同步函数，连接不跨线程。写路径在同步层持 `threading.Lock`（跨线程唯一的写者保证）；异步门面另持 `asyncio.Lock` 仅用于协程侧排队，两者不可互相替代。

连接建立后依次设置并在**回读**时校验：`journal_mode=WAL`、`foreign_keys=ON`、`busy_timeout=5000`、`synchronous=FULL`。`foreign_keys` 与 `journal_mode` 在事务内设置是**静默无效**的（实测：事务内 `PRAGMA foreign_keys=ON` 返回 0、`journal_mode=WAL` 返回 `delete`），因此任一回读不符合预期即抛错，不假定设置成功。

首次创建数据库文件后立即 `chmod 0600`，父目录 `0700`（`docs/technology-stack.md` §5.3）。

- 备选：`synchronous=NORMAL` → 拒绝：掉电可能丢最近提交。
- 备选：长连接池 → 拒绝：`sqlite3` 连接不可跨线程共享，本负载下无收益。

### D3: 幂等裁决先于一切副作用；目标存在性只靠外键

写入顺序固定为：显式 `BEGIN IMMEDIATE` → 逐条 `INSERT ... ON CONFLICT(event_id) DO NOTHING`（逐条 `execute`，不用 `executemany`，因为批量执行下 `rowcount` 退化为汇总计数、无法逐事件裁决）→ 若该条 `rowcount == 0` 立即记为"已存在"并跳过它的取代副作用 → 否则在同一事务内执行 `UPDATE events SET superseded = 1 WHERE event_id = :correction_of` → 全部处理完 `COMMIT`。

修正目标存在性只由外键保证，不额外 `SELECT` 预查（避免与幂等裁决打架：目标被删除后重复提交同一事件仍应报告"已存在"）。批次按输入顺序插入，被修正事件 MUST 在批次中先于修正事件出现（外键即时校验）；不自动排序、不依赖延迟约束。

- 备选：先查后插 → 拒绝：TOCTOU，且会被幂等裁决与目标校验的顺序差异撕裂。
- 备选：延迟外键（`DEFERRABLE INITIALLY DEFERRED`）→ 拒绝：允许任意批次顺序，但把"引用完整性错误"从写入点推迟到提交点，错误定位变差。

### D4: 修正链 = 自引用外键 + 显式 `superseded` 标记

`correction_of` 自引用 `events(event_id)` 并 `ON DELETE SET NULL`；`superseded` 由 D3 的写入路径在同一事务内置位。默认查询 `WHERE superseded = 0`，可通过参数包含已取代事件。

- 备选：`NOT EXISTS (SELECT 1 FROM events c WHERE c.correction_of = e.event_id)` 推导链末 → **实测拒绝**：删除链中 B（A←B←C）后 A 失去被引用关系而复活、C 的引用被置空，默认查询返回 `[A, C]` 两条"当前"；显式标记实测返回 `[C]`。
- 已知取舍：删除链末事件 C 后，该测量**没有当前版本**（默认查询为空集），A/B 保持已被取代；不自动回退到旧版本。用户需要时可包含已取代事件查看。这一行为写入 spec 场景而不是留给测试偶然。
- 一致性：标记只在写入事务内变更，无外部修改入口；删除 MUST NOT 清除其他事件的标记。

### D5: 时间键 = UTC 瞬时闭开区间，查询用区间相交

`observed_start_key` / `observed_end_key` 均为 UTC 瞬时，格式钉死为单一 normalizer：`dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")`。写入键与查询边界 MUST 走同一个 normalizer（实测：同一瞬时以 `+08:00` 与 `America/New_York` 两种偏移输入，生成逐字节相同的键；不同写法如带/不带小数秒或 `Z`/`+00:00` 混用会让 TEXT 比较静默出错）。

- 瞬时事件：`start_key = observedAt.start` 转 UTC，`end_key = NULL`；
- 区间事件：`end_key = observedAt.end` 转 UTC；
- 日期级事件（`precision = day`）：`start_key = local_midnight(observedAt.start) → UTC`；`end_key` 取事件自带 `end` 的次日当地午夜（`local_midnight(observedAt.end + 1 day)`，`end` 含当天），`end` 缺省时取 `local_midnight(observedAt.start + 1 day)`。日边界 MUST 按事件 `timezone` 逐日期解析当地午夜（`ZoneInfo`，遵循 DST），MUST NOT 用固定偏移或固定 24 小时推算（实测：`America/New_York` 2026-03-08 当地日为 23 小时，`05:00Z → 04:00Z`；硬编码 `-05:00` 再 +24h 会使终点偏差 1 小时）。`precision = day` 的行 `observed_end_key` MUST NOT 为 NULL，且 MUST 反映 `end`（忽略 `end` 会把跨日日期级区间错当成单日，造成静默漏检）。

查询区间为 `[q_lo, q_hi)`，谓词对瞬时与区间分离判断（半开开区间，两端均为开）：

```sql
observed_start_key < :q_hi
AND (
     (observed_end_key IS NULL     AND observed_start_key >= :q_lo)
  OR (observed_end_key IS NOT NULL AND observed_end_key  >  :q_lo)
)
```

边界矩阵（实测锁定，写入测试）：

| 情形 | 结果 |
| --- | --- |
| 瞬时事件 `start == q_lo` | 包含 |
| 瞬时事件 `start == q_hi` | 不包含 |
| 区间事件 `end == q_lo`（刚好在区间起点结束） | 不包含 |
| 区间事件起点在区间前、终点在内 | 包含 |
| 区间事件起点在内、终点在区间后 | 包含 |
| 区间事件完全覆盖查询区间 | 包含 |
| 日期级事件在查询前/后一天 | 不包含 |

“前一天/后一天”指以事件 `observedAt.timezone` 为基准的相邻本地日；若查询区间按 UTC 日界对齐，与某日期级事件部分相交属于正确行为（区间相交语义），不是缺陷。

- 备选：`start < q_hi AND COALESCE(end_key, start_key) >= q_lo` → **实测拒绝**：日期级事件的下界等于后一天的 `q_lo`，`>=` 会让它被"后一天"的查询误纳（实测：查 11-13 返回了 11-12 的日期级事件）。

- 备选：把纯日期保留为字面量 `2026-11-12` 参与字典序比较 → **实测拒绝**：日期串是当日时刻串的**前缀**，字典序上排在当日所有时刻**之前**，导致当天范围内漏检、前一范围误纳（实测：查 11-12 得 `[]` 漏掉日期级事件；查 11-11 误纳 11-12 的日期级事件）。同时 `ORDER BY ... DESC` 会把日期级排到同日最后。
- 与契约的关系：`payload_json` 仍保存原始日期与 `precision`，"不伪造时刻"约束作用于事件内容，不作用于内部比较键；导出与查询返回的内容仍是日期级。
- 备选：`start_key >= q_lo AND COALESCE(end, start) < q_hi`（"完全包含"语义）→ 拒绝：会系统性漏掉跨入/跨出/覆盖查询区间的区间事件。

### D6: 迁移 = 可注入的有序列表 + 显式 `BEGIN IMMEDIATE` 单事务

`apply_migrations(conn, migrations=MIGRATIONS) -> int`：读取 `PRAGMA user_version`，按序应用未应用项；每项显式 `BEGIN IMMEDIATE` → DDL/DML → `PRAGMA user_version = N` → `COMMIT`。

显式开事务是硬要求：Python legacy 隔离模式下 `CREATE TABLE` 后 `in_transaction` 仍为 `False`（实测），DDL 走 autocommit，会出现"版本未推进但表已建"的脏状态，下次启动永久失败。`PRAGMA user_version` 参与事务（实测回滚后版本不变）。任一迁移异常：回滚并把存储标记为不可写。

`migrations` 参数存在是为了让测试用合成 v2 迁移演练"历史版本 + 已有数据 → 升级"。

- 备选：Alembic → 拒绝：§5.1 已判定不引入。
- 备选：`CREATE TABLE IF NOT EXISTS` 静默建表 → 拒绝：无法表达列变更且掩盖失败。

### D7: 备份用 `Connection.backup()`；校验必须含真实读与期望值

备份在同步写锁内用 `sqlite3.Connection.backup()` 生成快照，并在同目录写 `<backup>.meta.json`（`chmod 0600`，含 `event_count`、`user_version`、备份时刻）。期望值必须随备份持久化：真实恢复场景中调用方往往已不存在，不能依赖内存里的预期值。

恢复校验 `verify_integrity(path, expected_count=None)` 的执行顺序是硬约束：`PRAGMA integrity_check` → `PRAGMA foreign_key_check` → **期望值解析**（`expected_count` 为 `None` 时读 `<backup>.meta.json`，meta 缺失即判失败，不从被验证文件自取）→ **真实读** `SELECT count(*) FROM events` 并与期望值比较 → 业务不变量（无悬空 `correction_of`、载荷可解析数等于行数、`precision = 'day'` 的行 `observed_end_key` 非 NULL）。期望值解析 MUST 先于真实读：否则无表副本会先抛 `no such table`，使"期望值缺失"与"读取失败"退化为同一个原因，丢失区分能力。

`sqlite3.DatabaseError` MUST 归类为校验失败（实测：截断文件让 `integrity_check` 直接抛错而不是返回非 `ok`）。仅靠 `integrity_check` 不足以发现"丢失 WAL 的完整文件"（实测：朴素 `shutil.copy` 后 `integrity_check` 返回 `ok`，但 `SELECT COUNT(*)` 报 `no such table`），因此"真实读 + 期望值比较"是必需项而非可选。

- 备选：`shutil.copy` → 拒绝：WAL 模式下得不到一致快照，且能通过完整性检查。
- 备选：`VACUUM INTO` → 拒绝：重写页布局，且无法表达"备份后立即用期望值验证"。

### D8: 异步门面 = `asyncio.Lock` 排队 + `asyncio.to_thread` 卸载

同步实现是唯一真相；`AsyncEventStore` 把每个调用 `await asyncio.to_thread(...)`，写路径额外持 `asyncio.Lock`。读路径不持写锁（WAL 允许并发读）。

第 3 期接入时把 `to_thread` 换成 `hass.async_add_executor_job` 即可，签名不变。

- 备选：自建 worker 线程/队列 → 拒绝：§5.2 已判定首版不需要第二套任务系统。

### D9: 删除 = 单事务删行 + 清引用（列与载荷一起）

`delete_events(ids)` 单事务：`DELETE FROM events WHERE event_id IN (...)`（外键把其他行的 `correction_of` 置空）→ 对因此被置空的行，把 `payload_json` 里的 `correctionOf` 同步改为 `null` → 返回 `(deleted, not_found)`。

载荷改写 MUST 走与写入相同的路径：`HealthEvent.model_validate(payload)` → 重算 `eventId`（应不变）→ `model_dump(mode="json", by_alias=True)`，而不是 `json.loads` 后原地改键；否则键序、可选字段省略与数字格式会与首次写入产生两种字节形态。

必须同时改载荷：`correction_of` 列与 `payload_json.correctionOf` 是同一引用的两份表示，只改列会让"按 ID 取回"返回仍指向已删除事件的载荷（实测可复现），而只改载荷会让外键失去意义。`correctionOf` 不参与 `eventId` 计算，因此重写载荷**不改变事件身份**。

这是"事件内容不可变"的唯一例外，且只发生在用户显式删除时；不执行 `VACUUM`，不宣称物理擦除。

- 备选：删除时级联删整条链 → 拒绝：扩大用户选择范围，造成数据丢失。
- 备选：软删除 → 拒绝：与 `docs/privacy.md` §6 的内容移除要求冲突。

### D10: 存储不重复校验、不写健康内容到日志

写入只接受 `HealthEvent` 实例。失败路径仅记录 `event_id`、`metric`、错误类别；断言与异常消息不得拼接 `value` 或 `payload_json`。

## Risks / Trade-offs

- [`synchronous=FULL` + 每操作新连接带来额外 fsync 与连接开销] → 低频健康测量，接受；实测瓶颈再加连接复用。
- [`superseded` 与 `correction_of` 是两处状态] → 只在同一事务同一条写入路径变更，无外部入口；测试覆盖"修正写入后旧的被标记"与"删除不清标记"。
- [日期级事件在内部用 UTC 瞬时键，与 `payload` 的日期字面量不同形] → 内容仍是日期级，键只用于比较；测试锁定"查询返回的精度与日期不变"。
- [WAL 与 `chmod` 在非本地文件系统（网络盘、大小写不敏感卷）行为可能不同] → CI 与目标平台各跑一次存储测试；不宣称跨文件系统一致。
- [多进程同时写入会得到 `busy_timeout` 后的显式失败] → 失败即上报，不静默；多进程不在本期范围。
- [`PRAGMA user_version` 需字符串拼接] → 版本号只来自代码内受控整数序列。
- [删除最新版本后该测量无当前值] → D4 明确为契约行为并写入 spec 场景。

## Migration Plan

无既有数据库：`user_version` 0 → 1（建表与索引）。回滚策略为删除数据库文件重建（本期无生产数据）。第 3 期在 HA 启动流程中先迁移、后订阅实体。

## Open Questions

无。
