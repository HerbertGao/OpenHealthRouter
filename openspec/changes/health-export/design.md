# Design

## Context

参见 proposal.md - Why。现状约束（已核对本仓代码与已安装 HA 2026.10.4 源码）：`store.py` 的 `QueryFilters` 只支持**单个** `metric`，排序为 `observed_start_key DESC, inserted_at_utc DESC, event_id`，默认排除被修正版本；`EventStore` 的写路径统一经私有 `_write()`（`threading.Lock` 单写者 + 显式事务），`AsyncEventStore` 再套一层 `asyncio.Lock`；`apply_migrations` 支持追加 v2 且每个迁移一个事务；`__init__.py` 目前不注册任何视图或服务；HA 的 `async_get_clientsession` 返回**进程级共享 session**（不随 entry 卸载关闭），`HomeAssistantView` **没有注销 API**，`async_register_admin_service` 只在 `call.context.user_id` 存在时校验管理员。

## Goals / Non-Goals

**Goals:**

- 默认导出面最小（不含设备、测量者、上下文、原始属性），且每个开关都可显式打开。
- 两个出口在字节层面同一份输出；顺序与分页确定，无 `OFFSET` 抖动。
- 审计满足 `docs/privacy.md` §5 的"事件 ID + 过滤摘要 + 计数 + 结果类别"，但不含载荷与密钥。
- 失败与零事件都在审计里可区分，且都能被用户显式重试。

**Non-Goals:**

- 不做自动/定时导出、重试队列、Outbox 守护进程。
- 不做多人权限体系；只有"HA 管理员"一个粒度。
- 不做 FHIR / Open mHealth / MCP 出口；不改事件身份规则；不改 `events` 表。
- 不做前端面板（预览走服务返回值）。

## Decisions

### D1: 策略是纯数据，默认面按 `docs/privacy.md` §2 取"否"

```python
@dataclass(frozen=True)
class ExportSelection:
    metrics: frozenset[str] | None = None  # None = 全部已注册指标
    since: datetime | None = None
    until: datetime | None = None
    quality_status: frozenset[str] = frozenset({"valid"})
    include_raw_attributes: bool = False
    include_device_refs: bool = False
    include_subject: bool = False
    include_context: bool = False
```

四个开关默认全关，对应 `docs/privacy.md` §2 表格里"测量者与设备标识：默认否，除非用户明确包含"。关闭时的序列化规则：`subjectRef` → `"redacted"`，`deviceRef` → `null`，`source.nativeId` → `null`，`context` → `{}`，`evidence.rawAttributes` → `{}`。

代价必须写清：默认导出数据无法区分对象，且**裁剪后的行不再是可通过契约校验的 `HealthEvent`**——`subjectRef`、`deviceRef`、`source.nativeId` 都是身份字段，裁剪后 `eventId` 已无法与身份字段对上。因此：`eventId` 原样保留为不透明引用；契约校验只适用于四开关全开；任何测试都不得对裁剪投影重算 ID 或调用模型校验。置空一律用 `null`（`deviceRef`、`source.nativeId`），不要用空串，避免与"缺失字段"语义混淆。

这是冻结源的选择，需要区分时用户显式打开 `include_subject` / `include_device_refs`。

- 备选：默认保留设备与测量者（作者先前倾向）→ 拒绝：与 §2 表格冲突，且评审明确指出这是"设计否决冻结需求"。
- 备选：直接删掉 `subjectRef` 字段 → 拒绝：契约必填字段被删会让输出无法再被契约校验。

### D2: 质量过滤按 `quality.status` 匹配，在读取后过滤

`quality_status` 匹配事件的 `quality.status`（`valid`/`suspect`/`invalid`），不做 issues 子串匹配。过滤在内存中做：`events` 表的索引列不含质量，本期导出量级不需要为它加列。

因此一批经质量过滤后可能为空，而 SQL 游标尚未耗尽：**迭代器只在游标耗尽时结束，绝不因空批次 `break`**。否则导出会被静默截断，且两个出口会"一致地少数据"，一致性测试也发现不了。

- 备选：给 `events` 加 `quality_status` 列 → 拒绝：为了导出改事件表，收益不足。

### D3: 读取用键集分页，多指标走一次查询

`QueryFilters` 增加可选 `metrics: frozenset[str] | None`，SQL 用 `metric IN (...)`；`metric` 单值参数保留以兼容既有调用。导出读取用键集分页而非 `OFFSET`：

```text
WHERE (:last_start IS NULL OR
       observed_start_key < :last_start
       OR (observed_start_key = :last_start
           AND (inserted_at_utc < :last_inserted
                OR (inserted_at_utc = :last_inserted AND event_id > :last_id))))
ORDER BY observed_start_key DESC, inserted_at_utc DESC, event_id ASC
LIMIT :batch
```

窗口谓词**复用** `QueryFilters.since/until` 的既有语义，不新增谓词：`store.py` 现实现为
`since → (observed_end_key IS NULL AND observed_start_key >= :s) OR (observed_end_key IS NOT NULL AND observed_end_key > :s)`、
`until → observed_start_key < :u`——瞬时事件是 `[s,u)`，日期级与区间事件按**相交**计入，正好等于本变更 R3 的要求。
键集分页查询 MUST 与 `query()` 共用同一 where 构件，不得复制重写，否则会出现"存储语义正确、导出语义漂移"。

**不能用行值比较 `(a,b,c) < (:a,:b,:c)`**：SQLite 的行值比较是全字典序升序，等价于
`a<:a OR (a=:a AND b<:b) OR (a=:a AND b=:b AND c<:c)`；而排序末位是 `event_id ASC`，
游标之后的行满足 `c > :c`，会被整批排除，同组内还会重复。必须写成上面显式的混合比较。

这样流式期间新写入的事件只会落在"更早/更晚"的确定侧，已发出的分页不会重复。

- 备选：多指标逐指标分页再拼接 → 拒绝：拼接后无法保证全局序，正是"两个出口一致"要防的错。
- 备选：`OFFSET` 分页 → 拒绝：并发写入会跳行/重复。

### D4: 单一导出迭代器，视图流式消费、Webhook 物化

```python
async def iter_export_batches(hass, store, selection, batch=200) -> AsyncIterator[list[HealthEvent]]
def to_ndjson(events, selection) -> bytes      # 单批序列化，含字段裁剪
```

视图每批 `await response.write(to_ndjson(batch, selection))`；Webhook 用 `b"".join(...)` 物化（HMAC 需要对完整字节签名）。`select_events(...) -> list` 只是迭代器的便利包装，不得成为流式路径的实现。

- 备选：只提供 `select_events` 返回全量 → 拒绝：内存无界，且"流式"断言无法成立。

### D5: NDJSON 视图契约固定

`GET /api/openhealthrouter/export.ndjson`，参数全部可选：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `metric` | 可重复 | 缺省=全部已注册指标 |
| `since` / `until` | ISO 8601 | 左闭右开 |
| `quality_status` | 可重复 | 缺省=`valid` |
| `include_raw_attributes` / `include_device_refs` / `include_subject` / `include_context` | 布尔 | 缺省 false |

校验用 `probatio`（HA 自带依赖，见 `pyproject` 解析结果，非本期新增）的 `Schema` + `PREVENT_EXTRA`：未知参数或非法取值返回 400（响应体不含事件内容）。未认证 401 由 `requires_auth=True` 的框架层直接给出，本期不另写断言；已认证但非管理员 403；`runtime_data` 不可用 404。

### D6: 视图只注册一次；卸载不注销，靠运行时守卫

HA 的 `register_view` 没有注销 API。因此：首次 setup 用 `hass.data` 标记确保只注册一次；`get` 先取 `entry.runtime_data`，不可用即 404。卸载后视图仍挂在路由上，但既不触达存储也不返回数据；重装复用同一实例，不会重复注册冲突。

- 备选：每次 setup 注册、卸载"注销" → 拒绝：API 不存在，任务会不可实现。

### D7: 预览与确认是两次调用

服务 `export_webhook` 参数含 `confirm: bool = False`。`confirm=False` 时只返回预览摘要，字段与 spec R4 逐项对齐：
目标地址、指标集合、时间范围、实际事件数量、四个层级开关、**实际生效的 `quality_status`（含"已排除非所选状态事件"表述）**、
**"是否包含厂商派生值与质量问题事件"的说明与计数**（`query()` 只过滤 `superseded`，厂商派生事件默认**会**被导出，因此这里必须报"包含 N 条"而不是"已排除"）、**"被修正旧版本默认不导出"**（查询固定 `superseded = 0`）、不可回收说明；
**不发请求、不写投递记录**；`confirm=True` 才发送。这样 `docs/privacy.md` §3 的"每次手动导出至少展示…"有了落点，且默认行为安全。

- 备选：直接发送，事后返回结果 → 拒绝：§3 要求发送前展示，无预览就等于没确认。

### D8: 服务注册与管理员判定

`async_register_admin_service(hass, DOMAIN, "export_webhook", handler, schema, supports_response=SupportsResponse.OPTIONAL)`。仅靠它不够：它只在 `call.context.user_id` 存在时校验管理员，无 `user_id` 的内部调用会被放行。handler 内显式要求 `call.context.user_id` 非空且对应用户 `is_admin`，否则 `Unauthorized`。卸载时 `hass.services.async_remove(DOMAIN, "export_webhook")`，并把该清理加入 `RuntimeData.unsubs`。

### D9: 投递走共享 session，签名覆盖实际字节

```python
body = b"".join(to_ndjson(batch, selection) for batch in batches)
signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
await async_get_clientsession(hass).post(
    url,
    data=body,
    headers=headers,
    timeout=aiohttp.ClientTimeout(total=10),
    allow_redirects=False,
)
```

`data=body`（bytes）走 `BytesPayload`，不二次编码；不传 `ssl=False`；`allow_redirects=False` 使 3xx 原样返回并按 HTTP 错误归类。不关闭共享 session（它属 HA 进程）。

- 备选：`json=` → 拒绝：库自己编码，签名会对不上。
- 备选：自建 session → 拒绝：与 `docs/technology-stack.md` §4.1 冲突。

### D10: 投递表结构与写入路径

```text
deliveries(
  delivery_id     TEXT PRIMARY KEY,
  target_ref      TEXT NOT NULL,          -- 选项里的名称，≤64 字符，不含 "://"
  filter_digest   TEXT NOT NULL,          -- 策略规范化 JSON 的 sha256
  since_key       TEXT,                   -- UTC 键（store.KEY_FORMAT）
  until_key       TEXT,
  event_count     INTEGER NOT NULL CHECK (event_count >= 0),
  event_ids_json  TEXT NOT NULL,          -- JSON 数组，满足 privacy §5
  status          TEXT NOT NULL CHECK (status IN ('success','failed','skipped_no_events')),
  error_category  TEXT,
  created_at      TEXT NOT NULL,
  CHECK (status <> 'failed' OR error_category IS NOT NULL)
)
CREATE INDEX idx_deliveries_created_at ON deliveries (created_at)
```

`filter_digest` 存哈希而不是可读的指标列表：`指标 + 起止时间 + 计数` 组合起来本身就能读出"某人某周测了几次体脂"，比不透明的事件 ID 更泄露。事件 ID 是内容散列，脱离 `events` 表不可解读，满足 §5 又不增加可读性。

写入走 `EventStore` 同一 `_write()` 路径（新增公开方法），异步侧经 `AsyncEventStore` 写锁，不新建连接分支，保持"迁移失败拒写"与单写者语义。

- 备选：存可读 metrics 与时间范围 → 拒绝：审计本身会成为健康画像。
- 备选：不存事件 ID（作者先前倾向）→ 拒绝：`docs/privacy.md` §5 明确要求。

### D11: 零事件与失败三态

- 匹配 0 条：不发请求，记 `skipped_no_events`，`event_count=0`，`event_ids_json=[]`。
- 2xx：`success`，`event_count` = 实际写入 body 的行数。
- 超时 / 连接失败 / 非 2xx（含 3xx）：`failed` + `timeout` / `network_error` / `http_error`。

`event_count` 必须与接收端实际行数一致，由集成测试交叉断言。

### D12: 迁移用 `IF NOT EXISTS`，并记录回滚边界

v2 迁移：`CREATE TABLE IF NOT EXISTS deliveries ...` + `CREATE INDEX IF NOT EXISTS`。回滚（卸载集成）后 `user_version` 仍是 2，v1 代码不会重新执行 v1 迁移——这一点写进文档：回滚前应确认 `events` 表存在；重装 v2 时 `IF NOT EXISTS` 使重复迁移不抛错。

- 备选：普通 `CREATE TABLE` → 拒绝：回滚后重装会因表已存在而永久 `ConfigEntryNotReady`。

### D13: 目标配置与 URL 校验

选项 `webhook_target = {name, url, secret}`。保存时：`url` 必须 `https://`、不含 userinfo、不含查询串；`name` ≤64 字符且不含 `://`（防止把 URL 塞进审计的 `target_ref`）。表单不回填密钥；错误提示不回显完整 URL。

### D14: 日志纪律

投递成功/失败只记录 `target_ref`、`delivery_id`、`event_count`、`status`、`error_category`。失败分支**禁止** `exc_info`（堆栈会带出 URL 与 body）；异常文本不写入日志。

### D15: 测试分层

- 单元：策略默认面与开关矩阵、`to_ndjson` 逐字节稳定、签名对同一 bytes、键集分页序、`filter_digest` 稳定性。
- 集成：视图 401（框架层）/403/400/404 与分块传输、两个出口**在无订阅的合成库上**逐字节一致、预览不发请求、确认后发送、超时/3xx/自签证书、零事件三态、NDJSON 零匹配返回 200 空体、重试新增记录、`event_count` 与接收端行数交叉一致、审计列白名单与内容扫描、迁移 v1→v2 与孤儿表、空闲期出站计数为 0、日志不含敏感内容、读取全程经 executor（无 `Detected blocking call` 警告），以及**正向行数断言**（下载行数与独立计算的期望匹配数相等，跨分页且含三态质量样本）、**窗口边界断言**（瞬时事件 `start == since` 计入、`start == until` 排除；边界缺省视为无界）、**快照一致性断言**（两次出口之间无写入时逐字节相同）。
- 真实设备：人工确认记录写进 `docs/`（不含真实标识），脱敏向量进 `tests/fixtures/synthetic/vectors/`。

## Risks / Trade-offs

- [默认导出无法区分对象（D1）] → 文档显式说明，需要区分时打开 `include_subject`/`include_device_refs`；这是冻结源的选择。
- [事件 ID 列表可能很长（D10）] → 单次导出量级有限；若将来出现上万条，再考虑改为 ID 摘要 + 计数，届时同步修订 `docs/privacy.md` §5。
- [视图无法注销（D6）] → 用运行时守卫 404 兜住；重装不复用注册路径，避免冲突。
- [共享 session 被目标拖慢] → 每次请求独立超时，不关闭共享 session。
- [回滚后 user_version 停在 2（D12）] → 文档写明回滚前确认 `events` 表存在；迁移用 `IF NOT EXISTS` 防重装失败。
- [审计被误当作"发送内容"的证据] → 只存哈希摘要与不透明 ID，文档明确它不能还原内容。

## Migration Plan

部署：`manifest.version` 递增；启动时追加 v2（只加表与索引，`IF NOT EXISTS`）。回滚：卸载集成即可，`deliveries` 表留在库中；v1 代码不读它，但也不会重跑 v1 迁移——需人工确认 `events` 表完好。已发送到外部的数据无法回收，UI/文档逐项说明。

## Open Questions

无。
