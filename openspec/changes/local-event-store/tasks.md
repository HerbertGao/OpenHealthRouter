# Tasks

## 1. 前置确认与存储骨架

- [x] 1.1 将第 2 期人工审批结论写入 `openspec/changes/local-event-store/approval.md`（三项：删除语义——内容移除、修正引用列与载荷同步清空、不复活、不扩大范围；迁移失败——回滚并拒绝写入、修复后可重跑；备份边界——不承诺物理擦除、不覆盖既有备份与已导出副本），第 1 节、4.2 与第 5 节其余任务以该文件存在且三项结论与 design/spec 一致为前提；并验证 `approval.md` 存在且含上述三项结论
- [x] 1.2 创建 `custom_components/openhealthrouter/migrations.py`：`MIGRATIONS: list[tuple[int, Callable]]` 含 v1（建 `events` 表与三个索引），`apply_migrations(conn, migrations=MIGRATIONS) -> int` 读取 `PRAGMA user_version` 并按序在**显式** `BEGIN IMMEDIATE` 事务内应用、同事务内推进版本；并验证空库初始化到版本 1、索引齐备、重复调用不重复执行
- [x] 1.3 创建 `custom_components/openhealthrouter/store.py`：`EventStore.open(path)`、显式 `close()`；建连后设置 `journal_mode=WAL`、`foreign_keys=ON`、`busy_timeout=5000`、`synchronous=FULL` 并**回读校验**，不符即抛错；新建文件 `chmod 0600`、父目录 `0700`；并验证回读四项符合预期、事务内设置 `foreign_keys` 无效的行为被显式捕获
- [x] 1.4 实现迁移失败语义：任一迁移抛错时回滚、版本不推进、存储标记不可写（后续写入抛 `StoreUnavailable`）；并验证注入必失败迁移后断言版本未变、表未残留、写入被拒绝；随后修正迁移实现并重开，断言可成功迁移且存储恢复可用
- [x] 1.5 实现历史版本升级演练：注入合成 v2 迁移（如新增一个索引），先按 v1 建库并写入多条事件（含带修正链的一条与日期级事件一条），再以 `migrations=MIGRATIONS + [v2]` 重开；并验证 `user_version` 推至 2、行数不变、按 ID 与按范围查询结果与升级前逐字段一致、备份校验通过

## 2. 幂等写入与事务原子性

- [x] 2.1 实现 `insert_events(events) -> InsertResult(created, duplicates)`：`payload_json` 由 `model_dump(mode="json", by_alias=True)` 生成，索引列按 design D1/D5 提取；显式 `BEGIN IMMEDIATE` 后**逐条** `execute` `INSERT ... ON CONFLICT(event_id) DO NOTHING`（不用 `executemany`），以单条 `rowcount` 裁决，`rowcount == 0` 时立即记"已存在"且不执行任何后续副作用；并验证首次新建、重复报已存在、原内容不变
- [x] 2.2 实现修正写入副作用的顺序语义：仅在新插入时于同一事务内置 `superseded = 1`；目标存在性只靠外键，不做预查；批次按输入顺序插入；并验证"目标已删除后重复提交同一事件 → 已存在且不抛错"、"新事件指向不存在目标 → 拒绝"、"批次内修正先于目标出现 → 整批拒绝"三例
- [x] 2.3 实现批次单事务：显式 `BEGIN IMMEDIATE` 开启，一批事件全部成功或全部回滚；并验证含一条违反约束的批次整体回滚、库内容不变、且该批次中已处理行的 `superseded` 标记同样未被改动
- [x] 2.4 实现失败可见性：只读数据库、以及外部持写锁超过 `busy_timeout` 两种场景均抛出明确失败而非返回成功；并验证两例分别断言失败类型与库内容不变
- [x] 2.5 实现并发写不丢事件：同 `eventId` 并发提交后恰好一行；不同 `eventId` 并发提交后全部可按 ID 取回；并验证两类并发用例（多线程）后断言行数与取回结果

## 3. 查询

- [x] 3.1 实现 `query(filters)`：按 `subject_ref`、顶层 `metric`、时间范围过滤，半开区间相交谓词 `observed_start_key < :q_hi AND ((observed_end_key IS NULL AND observed_start_key >= :q_lo) OR (observed_end_key IS NOT NULL AND observed_end_key > :q_lo))`，分页 `limit/offset`，排序 `observed_start_key DESC, inserted_at_utc DESC, event_id`，返回事件与 `total`；键与边界统一走单一 normalizer `%Y-%m-%dT%H:%M:%S.%fZ`（UTC）；并验证指标+范围过滤准确、`total` 等于不分页匹配数、单页不超过 `limit`、空结果返回空集合与零计数、同一瞬时不同偏移输入生成逐字节相同的键
- [x] 3.2 实现时间边界矩阵（design D5 表逐行断言）：瞬时事件 `start == q_lo` 命中、`start == q_hi` 不命中；区间事件 `end == q_lo` 不命中；起点在区间前/终点在内、起点在内/终点在区间后、完全覆盖查询区间三种区间事件均命中；并验证以下四组时间断言（查询边界均取**事件时区当地午夜转 UTC** 作为 `q_lo/q_hi`）：①日期级单日事件当天命中、前一天与后一天不命中；②日期级区间事件（`start=2026-11-12, end=2026-11-13`）：查 11-12 命中、查 11-13 命中、`end_key` 等于 11-14 当地午夜对应的 UTC 瞬时（不许忽略 `end`）；多日区间（`start=2026-11-12, end=2026-11-15`）：查 11-14 命中、查 11-16 不命中、`end_key` 等于 11-16 当地午夜对应的 UTC 瞬时；③DST 用例：`America/New_York` 2026-03-08 的 `end_key` 与 `start_key` 均按 UTC 解析后相减等于 `timedelta(hours=23)`（MUST NOT 用事件时区的 aware datetime 直接相减，同 tzinfo 相减是墙上时间差会得 24 小时）；④以 UTC 日界对齐查询相邻日期级事件时部分相交 → 命中（记录为正确行为，与 ① 不矛盾）
- [x] 3.3 实现分页不重不漏与精度保真：并验证分页合并结果等于一次性结果且顺序稳定；查询返回的日期级事件 `observedAt.start` 仍为日期字面量、`precision` 仍为 `day`
- [x] 3.4 实现 `get_event(event_id) -> StoredEvent | None`：返回完整内容与"是否已被取代"标记；并验证被取代事件可取回、不存在返回 None

## 4. 修正链

- [x] 4.1 实现默认过滤与开关：主语句附 `WHERE superseded = 0`，`include_superseded=True` 时不过滤；并验证 A→B 只返回 B、A→B→C 只返回 C、开关打开后三者全返回
- [x] 4.2 实现标记维护与不复活：修正写入同事务置被修正事件 `superseded = 1`；删除链中事件（删 B）与删除链末事件（删 C）后旧版本 MUST NOT 重新计入默认查询；并验证两种删除后默认查询分别为 `[C]` 与空集，且含取代事件时可取回 A/B

## 5. 删除

- [x] 5.1 实现 `delete_events(ids) -> DeleteResult(deleted, not_found)` 单事务批量删除：删行后对被外键置空 `correction_of` 的行，同步把 `payload_json` 中的 `correctionOf` 改为 `null`（不改变 `eventId`）；并验证删除后按 ID 与按范围均不可取得、返回计数正确
- [x] 5.2 实现引用双清与作用域不扩大：删除 A（被 B 修正）后 B 仍可查询，且按 ID 取回的 B 中 `correctionOf` 为 `null`；并验证列与载荷两处均已清空、B 其余字段不变、`eventId` 不变、且改写后的载荷与按契约模型重新序列化的结果逐字节相等
- [x] 5.3 实现不存在 ID 的宽容处理：删除不存在的 `eventId` 报告未删除、不抛错、库内容不变；并验证混合批次分别报告已删除与未找到数量
- [x] 5.4 实现删除不清取代标记：删除 B（A←B←C 的中间节点）后断言 A 的 `superseded` 仍为 1；并验证该断言与 4.2 的默认查询结果一致

## 6. 备份与恢复

- [x] 6.1 实现 `backup_to(path)`：同步写锁内用 `sqlite3.Connection.backup()` 生成快照，并写同目录 `<backup>.meta.json`（`chmod 0600`，含 `event_count`、`user_version`、备份时刻）；并验证在 `wal_autocheckpoint=0` 且源连接保持打开、存在未合并日志时备份仍包含全部已提交事件，且 meta 中的 `event_count` 等于源库行数
- [x] 6.2 实现 `verify_integrity(path, expected_count=None)`：按 design D7 的**固定顺序**执行——`integrity_check` → `foreign_key_check` → 期望值解析（`None` 时读 `<backup>.meta.json`，meta 缺失判失败且不从被验证文件自取）→ 真实读计数并与期望值比对 → 业务不变量（无悬空 `correction_of`、载荷可解析数等于行数、`precision='day'` 行 `observed_end_key` 非 NULL）；`sqlite3.DatabaseError` 一律归类失败；并验证：正常库通过；截断文件失败；纯主文件副本（无 meta）→ 期望值缺失失败；纯主文件副本 + 与源库一致的 meta → 仍失败且原因为真实读取失败（已实测：朴素 `shutil.copy` 后 `integrity_check` 为 `ok`、`SELECT count(*)` 报 `no such table`）；计数与期望值不符失败；人工插入 `end_key` 为 NULL 的日期级行失败
- [x] 6.3 实现恢复路径：从备份文件打开存储并完成校验后才标记可用；校验失败时报错且不返回半可用存储；并验证成功与失败两条路径

## 7. 异步门面与日志纪律

- [x] 7.1 创建 `custom_components/openhealthrouter/async_store.py`：`AsyncEventStore` 所有调用经 `asyncio.to_thread`，写路径持 `asyncio.Lock`，读路径不持写锁；并验证写入/查询/删除/备份异步接口行为与同步实现一致
- [x] 7.2 验证不阻塞事件循环且测试有判别力：写入线程内用 `threading.Event` 阻塞，断言独立 asyncio 任务在写入返回前完成；并验证同一测试在直接同步调用时失败、走异步门面时通过
- [x] 7.3 验证写入进行中读取可用（分两层，各有判别力）：①同步层（仅证明用例灵敏度）：写线程在 `BEGIN IMMEDIATE` 内被 `threading.Event` 挂住，主线程在 Event 未 set 期间以有界等待断言读已完成；变异自检：给读路径合成一把同一 `threading.Lock` 后该断言必须失败（这证明用例灵敏度，不代表 D8 的写锁位置）。②异步层（对应 D8 真实写锁位置）：写任务在 `BEGIN IMMEDIATE` 内被挂住**并持 `asyncio.Lock`** 时，用**外层** `asyncio.wait_for(store.query(...), 2)` 断言读成功返回已提交快照；变异自检：把 `AsyncEventStore.query` 改为 `async with self._lock` 后同一外层 `wait_for` 必须抛 `TimeoutError`（已实测：`wait_for` 放锁内则判别力为 0）；并验证正反两向均按此实现（只有返回值断言而无外层有界等待时，变异会将读从毫秒拖到超时而非报错）
- [x] 7.4 实现日志纪律：失败路径只记录 `event_id`、`metric`、错误类别；并验证失败确实产生日志且捕获输出中不含事件数值与 `payload_json` 片段

## 8. 全量回归

- [x] 8.1 全量门禁：`ruff format --check`、`ruff check`、`pyright`、`check_contracts.py`、`pytest`、敏感数据扫描全部通过；并验证本地一次跑通
- [ ] 8.2 独立 review 无阻断项后推送，CI 的 `check` 与 `hassfest` 两 job 全绿；并验证 CI 运行结果与该提交对应

## Workflow follow-up

- 归档前对照 `docs/roadmap.md` 第 2 期验收逐条确认；把"删除语义、迁移失败行为、备份校验口径"同步进 `docs/technology-stack.md` §5（当前措辞仍是旧的推导式修正链描述）。
- 本变更被审查接受后归档。
