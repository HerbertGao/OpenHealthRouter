# Tasks

## 1. 策略、键集分页与序列化

- [x] 1.1 在 `store.py` 的 `QueryFilters` 增加可选 `metrics: frozenset[str] | None`（SQL 用 `metric IN (...)`，单值 `metric` 参数保留），并新增键集分页查询：游标用**显式混合比较**（`start < :s OR (start = :s AND (inserted < :i OR (inserted = :i AND event_id > :e)))`），禁止用行值 `(a,b,c) < (...)`；排序保持 `start DESC, inserted DESC, event_id ASC`；并验证多指标过滤、单指标兼容、分页无重复无跳行、"同一 `(start, inserted)` 下多个 event_id 跨页"、"分页期间插入排序在游标之后的事件"五类用例；键集路径 MUST 与 `query()` 共用同一 where 构件，并断言"同一 `QueryFilters`（含 `since`/`until` 边界相等值）下键集迭代结果与 `query(limit=大值)` 集合完全相同"
- [x] 1.2 创建 `custom_components/openhealthrouter/export.py`：`ExportSelection`（八个字段，四个开关默认 false）、`iter_export_batches(hass, store, selection, batch=200)`（每批读取经 `hass.async_add_executor_job` / `AsyncEventStore._run`，不在事件循环里直连 SQLite）、`select_events(...)` 便利包装；迭代器只在游标耗尽时结束，**空批次不得 break**；并验证默认面裁剪、四个开关各自独立、`quality_status` 三态过滤（缺省 `valid` 时 `suspect`/`invalid` 不出现）、以及"前 200 行全为 suspect、第 201 行 valid"不截断的用例
- [x] 1.3 实现 `to_ndjson(events, selection) -> bytes`：逐行紧凑 JSON、键排序、末尾换行、按 selection 裁剪字段；并验证同一输入两次调用逐字节相同
- [x] 1.4 实现 `filter_digest(selection) -> str`：策略规范化 JSON 的 SHA-256，稳定且与开关顺序无关；并验证等价 selection 得同一摘要、不同开关得不同摘要

## 2. 投递存储与迁移

- [x] 2.1 在 `migrations.py` 追加 v2：`CREATE TABLE IF NOT EXISTS deliveries`（10 列，含 `status` 枚举与 `failed ⇒ error_category` 的 CHECK）与 `CREATE INDEX IF NOT EXISTS idx_deliveries_created_at`；不改 `events` 表；并验证 v1 库升级后事件可查、重复执行不抛错、`user_version` 推进到 2
- [x] 2.2 在 `EventStore` 增加公开方法 `record_delivery(...)` / `list_deliveries(...)`，走既有 `_write()` 单写者路径与参数化 SQL；`AsyncEventStore` 增加对应异步包装（走写锁）；并验证 store 不可用时写入被拒（不静默成功）、成功/失败/零事件各写一条且字段一致、`event_count` 与 `len(event_ids_json)` 以及接收端实际行数三者相等
- [x] 2.3 验证审计列白名单：读取 `PRAGMA table_info(deliveries)` 断言列集合恰为声明字段；对整表序列化断言不含事件数值、原始属性键、URL 与密钥；并验证该断言在"给表加一列 payload_json"的变异下会失败

## 3. 签名与 Webhook 投递

- [x] 3.1 创建 `custom_components/openhealthrouter/webhook.py`：`sign_body(secret, body)`、`async_deliver(hass, target, body, delivery_id) -> DeliveryOutcome`（`async_get_clientsession`、`ClientTimeout(total=10)`、`data=body`、`allow_redirects=False`），把结果归类为 `success`/`timeout`/`network_error`/`http_error`；并验证 2xx、超时、500、302 四类结果
- [x] 3.2 验证签名覆盖实际字节：本地 aiohttp 接收端读原始 body 重算 HMAC，与 `X-OpenHealthRouter-Signature` 比较，断言完全相等；并验证"把签名改为对重新编码 JSON 计算"的变异会失败
- [x] 3.3 验证投递 ID 与时间戳：两次投递 ID 不同、时间戳为整数秒且在合理窗口；并验证 302 只发一次请求（接收端计数为 1）
- [x] 3.4 验证证书校验：对自签证书目标投递，断言失败并记 `network_error`，不产生成功记录

## 4. NDJSON 视图

- [x] 4.1 创建 `custom_components/openhealthrouter/views.py`：`ExportNdjsonView(HomeAssistantView)` 注册到 `GET /api/openhealthrouter/export.ndjson`，查询参数按 design D5 用 `probatio` + `PREVENT_EXTRA` 校验，`requires_auth=True`，非管理员 403，运行时不可用 404，非法参数 400；并验证 401（框架层，未认证）/403（非管理员）/400（非法参数）/404（运行时不可用）各一条，且 400/403/404 的响应体不含事件内容
- [x] 4.2 实现流式输出：`web.StreamResponse` 逐批 `write`，不设 `Content-Length`；并验证"响应无 Content-Length、接收端至少收到 2 个分块、导出目录与 www 目录无新文件、零匹配时返回 200 且 body 为空、全程无 `Detected blocking call` 警告"
- [x] 4.3 视图只注册一次（`hass.data` 标记）并在 setup 时接入、卸载后返回 404 且不触达存储；并验证重复 setup 不重复注册、卸载后请求得 404

## 5. 服务、预览与目标配置

- [x] 5.1 创建 `custom_components/openhealthrouter/services.py`：`async_register_admin_service(..., supports_response=SupportsResponse.OPTIONAL)` 注册 `export_webhook`；handler 内显式要求 `call.context.user_id` 非空且为管理员，否则 `Unauthorized`；并验证管理员调用返回预览、无 user_id 的内部调用被拒
- [x] 5.2 实现预览/确认两步：`confirm=False`（默认）只返回摘要（目标地址、指标、时间范围、实际事件数、四个开关、**实际生效的质量状态与"已排除非所选状态"表述**、**"是否包含厂商派生值与质量问题事件"说明及计数**、**"被修正旧版本默认不导出"说明**、不可回收说明）且不发请求不写记录；`confirm=True` 才发送；并验证默认调用后出站请求计数为 0、无投递记录，确认后才产生记录，以及"未配置目标时预览与确认各返回明确错误、出站计数 0、投递表无记录"
- [x] 5.3 在 Options Flow 增加目标配置（名称/URL/密钥）：URL 必须 `https://`、不含 userinfo、不含查询串；名称 ≤64 且不含 `://`；表单不回填密钥；并验证四类非法输入各自返回表单错误、合法保存后可预览、表单不回填密钥
- [x] 5.4 卸载时 `hass.services.async_remove(DOMAIN, "export_webhook")` 并加入 `RuntimeData.unsubs`；并验证卸载后服务不可调用、重装后可用

## 6. 集成测试（MVP 验收面）

- [x] 6.1 两出口字节一致：在**无订阅的合成库**上先写入 >200 条事件，用**同一份 `ExportSelection`**（`filter_digest` 相同）分别走视图与已确认投递，断言接收端 body 与下载内容逐字节相等，并断言两次读取前后 `COUNT(*)` 与 `MAX(inserted_at_utc)` 未变（排除并发写入导致的随机失败）；**正向行数断言**：接收端行数 == 下载行数 == 独立计算的期望匹配数，样本需跨分页且覆盖 `valid`/`suspect`/`invalid` 三态
- [x] 6.2 导出范围精确：请求最近 3 天体重，断言不含其他指标、其他日期、原始属性、设备与测量者（默认开关下）；并用 Asia/Shanghai 与 America/New_York 两个时区的合成日期级事件，逐条断言哪些按相交计入、哪些因右开边界排除；再断言恰好的 N 条（不只有否定断言）、瞬时事件在 `start == since` 计入而 `start == until` 排除、以及 `since`/`until` 均缺省时返回全部匹配事件（含一条日期级事件）
- [x] 6.3 零事件三态：空结果时预览 0 条、确认后不发请求、记 `skipped_no_events`、本地事件数不变
- [x] 6.4 失败与重试：目标超时后记录 `failed`+`timeout`、本地事件数不变；再次确认新增一条记录且旧记录原状
- [x] 6.5 审计与日志不泄露：投递成功后扫描投递表、服务响应与日志（含 aiohttp logger），断言无事件数值、原始属性、URL、密钥与异常堆栈
- [x] 6.6 空闲零外发：setup 后静置，断言出站请求计数为 0；并验证"在 setup 里偷偷发一次请求"的变异会使该用例失败
- [x] 6.7 迁移兼容：v1 库升级后事件可查、孤儿 `deliveries` 表存在时重装不失败

## 7. 真实设备验收与文档

- [ ] 7.1 真实体脂秤人工确认：本地逐项确认体重、体脂率、测量者与时间语义，把结论（不含真实数值与设备标识）写入 `docs/`；并验证文档中不含可识别个人的数据
- [ ] 7.2 脱敏回归向量：把确认过的形状写成 `tests/fixtures/synthetic/vectors/` 下的合成向量，并验证被现有契约测试加载
- [x] 7.3 补齐安装、备份、恢复与隐私说明：数据落点、备份范围、导出默认面与开关、删除边界、已发送数据不可回收、回滚前确认 `events` 表

## 8. 全量回归与 MVP 标记

- [x] 8.1 `manifest.json` 递增版本；`strings.json` 补齐服务、预览、目标配置与错误文案；并验证 hassfest 通过
- [x] 8.2 全量门禁：`ruff format --check`、`ruff check`、`pyright`、`check_contracts.py`、`pytest`、敏感数据扫描；并验证本地一次跑通
- [ ] 8.3 独立 review 无阻断项后推送，CI 的 `check` 与 `hassfest` 全绿；并验证结果对应该提交
- [ ] 8.4 MVP 标记：仅在 8.2/8.3 与 roadmap 第 4 期验收全部满足后，把 `docs/roadmap.md` 第 4 期状态改为"已完成（MVP）"

## Workflow follow-up

- 归档前对照 `docs/roadmap.md` 第 4 期验收逐条确认；把导出默认面、策略字段与审计字段表同步进 `docs/privacy.md` 与 `docs/technology-stack.md` §6。
- 本变更被审查接受后归档。
