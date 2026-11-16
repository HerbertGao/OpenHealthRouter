# Spec Delta

## Purpose

把本地健康事件按统一策略筛选并投递到用户确认的出口（管理员 NDJSON 下载、HTTPS Webhook），同时留下不含载荷的投递审计，并保证失败可见、可重试、默认不外发。

## ADDED Requirements

### Requirement: 统一导出策略与数据层级开关

导出 MUST 由同一套策略实现筛选：指标集合、时间范围、质量状态与四个数据层级开关。默认值 MUST 为：质量状态 `valid`、原始属性关闭、设备引用关闭、测量者标识关闭、测量上下文关闭。关闭时序列化 MUST 用占位或空值表达（测量者置 `redacted`、设备与原生 ID 置空（`null`）、上下文与原始属性置空），MUST NOT 静默保留原值。导出产物是**裁剪投影**，不是可再校验的 `HealthEvent`：`eventId` 原样保留为不透明引用，契约模型校验只适用于四个开关全开的情形；对裁剪投影 MUST NOT 重算 ID 或调用契约校验。NDJSON 与 Webhook 在**相同策略输入且相同事件快照**（两次出口之间无写入）下 MUST 产出逐字节相同的 NDJSON；导出窗口内的并发写入按 R2 的快照语义处理。

#### Scenario: 默认面不含设备、测量者、上下文与原始属性

- **WHEN** 以默认开关导出任意事件
- **THEN** 输出中 `deviceRef` 为空、`subjectRef` 为 `redacted`、`context` 与 `evidence.rawAttributes` 为空、`source.nativeId` 为空

#### Scenario: 显式开启才保留

- **WHEN** 请求时把设备引用与测量者开关都打开
- **THEN** 输出保留原始 `deviceRef` 与 `subjectRef`

#### Scenario: 两个出口逐字节一致

- **WHEN** 以相同策略分别执行 NDJSON 导出与 Webhook 投递，且两次执行之间无写入
- **THEN** 两者发送的 NDJSON 字节完全相同（因此事件集合、顺序与字段裁剪都相同）

#### Scenario: 质量状态过滤

- **WHEN** 策略要求只导出 `valid` 状态
- **THEN** 结果不含 `suspect` 或 `invalid` 事件

#### Scenario: 空结果不报错

- **WHEN** 策略匹配不到任何事件
- **THEN** NDJSON 导出返回空内容且成功

### Requirement: 确定性顺序与稳定分页

导出 MUST 使用固定排序键 `观察开始时间降序、写入时间降序、事件 ID 升序`。读取 MUST 使用键集分页而不是 `OFFSET`，使**已发出的分页不重复、不跳行**；导出按快照语义：导出进行中新增的事件若排序在游标之前、或在导出期间被标记为已被取代，允许不出现在本次结果中；两个出口各自按自己的快照边界读取，**不承诺跨调用一致**（R1 的前置条件成立时除外）。多指标 MUST 在同一次查询内完成过滤，不得按指标分段拼接。

#### Scenario: 顺序稳定

- **WHEN** 对同一策略连续导出两次且期间无新事件
- **THEN** 两次输出的事件顺序完全一致

#### Scenario: 分页期间新增事件不造成重复或跳行

- **WHEN** 导出进行中写入一条排序在游标之后的事件
- **THEN** 已发出的分页不出现重复行，后续分页按序包含它；排序在游标之前的新事件允许不出现在本次结果中

### Requirement: 时间窗口与区间事件语义

时间窗口 MUST 为 UTC 键上的左闭右开 `[since, until)`；瞬时事件按观察时间落在窗口内计入；日期级与区间事件 MUST 按与窗口**相交**计入，归组基准是事件自身时区的当地午夜，比较基准是 `observation_keys` 产出的 UTC 键。窗口 MISSING 视为无界。

#### Scenario: 日期级事件按相交计入

- **WHEN** 查询窗口是某个 UTC 日，而某日期级事件的当地日与之部分相交
- **THEN** 该事件计入结果

#### Scenario: 当地日边界不同时不误算

- **WHEN** 窗口右界正好落在某日期级事件当地日的起点
- **THEN** 该事件不计入（右开）

#### Scenario: 瞬时事件的相等边界

- **WHEN** 合成三条瞬时事件分别落在左界之前、正好等于左界、正好等于右界
- **THEN** 只有"正好等于左界"那条计入，另外两条排除

#### Scenario: 缺省边界视为无界

- **WHEN** 请求不提供 `since` 与 `until`
- **THEN** 返回全部匹配指标的事件（含日期级事件）

### Requirement: 管理员认证的 NDJSON 下载

导出 MUST 通过需要认证的路由 `GET /api/openhealthrouter/export.ndjson` 提供，并 MUST 要求管理员权限。查询参数 MUST 为：可重复的 `metric`、ISO 8601 的 `since` 与 `until`、可重复的 `quality_status`、布尔开关 `include_raw_attributes`/`include_device_refs`/`include_subject`/`include_context`。未知参数或非法取值 MUST 返回 400 且响应体不含事件内容；非管理员 MUST 返回 403 且不含事件内容；运行时不可用时 MUST 返回 404。响应 MUST 流式输出（无 `Content-Length`、分块传输），MUST NOT 生成完整临时文件，MUST NOT 写入公开静态目录。

#### Scenario: 管理员可流式下载

- **WHEN** 管理员按策略请求下载
- **THEN** 响应为分块传输、逐行 JSON，行数与策略匹配数一致

#### Scenario: 非管理员被拒绝

- **WHEN** 非管理员用户请求同一 URL
- **THEN** 返回 403，响应体不含任何事件内容

#### Scenario: 非法参数被拒绝

- **WHEN** 请求携带未知参数或非 ISO 时间
- **THEN** 返回 400，响应体不含任何事件内容

#### Scenario: 卸载后不再导出

- **WHEN** 集成卸载后再次请求该 URL
- **THEN** 返回 404，且不触达存储

### Requirement: 投递前预览与显式确认

Webhook 投递 MUST 分两步：先预览，再以显式确认参数发送。预览 MUST 返回目标地址、指标集合、时间范围、实际事件数量、四个层级开关的取值、实际生效的质量状态取值（含"已排除非所选状态事件"的显式表述）、"是否包含厂商派生值与质量问题事件"的说明及对应计数、"被修正旧版本默认不导出"的说明，以及"已发送数据不可回收"的说明；并 MUST NOT 发起网络请求。缺少确认参数时 MUST 只预览。

#### Scenario: 默认只预览

- **WHEN** 未提供确认参数调用投递
- **THEN** 返回预览摘要且不产生任何网络请求与投递记录

#### Scenario: 确认后才发送

- **WHEN** 提供确认参数调用投递
- **THEN** 发起一次请求并按结果记录投递

### Requirement: Webhook 投递与签名

投递 MUST 使用 HTTPS 并校验证书（MUST NOT 提供忽略证书校验的开关），MUST 使用有限超时，MUST NOT 跟随重定向。请求 MUST 使用共享 aiohttp session、以 `data=<bytes>` 发送，并携带 `X-OpenHealthRouter-Timestamp`、`X-OpenHealthRouter-Delivery-Id` 与 `X-OpenHealthRouter-Signature: sha256=<hex>`；签名 MUST 基于实际发送的 body 字节。3xx、4xx、5xx MUST 归类为 HTTP 错误（不跟随跳转），超时与连接失败 MUST 分别为超时与网络错误。

#### Scenario: 签名与实际 body 一致

- **WHEN** 一次投递到达接收端
- **THEN** 对接收到的原始 body 用同一密钥重算 HMAC，与签名头完全相等

#### Scenario: 每次投递唯一 ID

- **WHEN** 连续触发两次已确认投递
- **THEN** 两次请求的投递 ID 不同，且都出现在投递记录中

#### Scenario: 重定向不跟随

- **WHEN** 目标返回 302
- **THEN** 记为 HTTP 错误、只发出一次请求、不向跳转地址发送任何内容

#### Scenario: 自签证书被拒绝

- **WHEN** 目标使用不受信任的证书
- **THEN** 投递失败并记为网络错误，不发送成功记录

### Requirement: 投递审计持久化

投递 MUST 落库到专用表（数据库版本升级），记录且仅记录：投递 ID、目标本地引用、过滤摘要哈希、时间范围、实际发送事件数、事件 ID 列表、状态与错误类别、创建时间。MUST NOT 保存事件载荷、原始属性、URL 或密钥。状态 MUST 为 `success`、`failed`、`skipped_no_events` 之一；`failed` MUST 带错误类别。`event_count` MUST 等于实际写入 body 的事件行数。既有 `events` 表 MUST 保持不变。

#### Scenario: 成功投递留下审计

- **WHEN** 一次投递得到 2xx
- **THEN** 记录状态 `success`，事件数等于接收端实际收到的行数，事件 ID 列表与之一一对应

#### Scenario: 审计列集合固定

- **WHEN** 读取投递表结构
- **THEN** 列集合恰为声明字段，不含载荷、URL 或密钥列

#### Scenario: 审计不含敏感内容

- **WHEN** 投递成功后序列化整表
- **THEN** 找不到事件数值、原始属性键、URL 与密钥字符串

### Requirement: 零事件与失败语义

零匹配事件时 MUST NOT 发起网络请求，MUST 记录 `skipped_no_events` 且事件数为 0。投递失败 MUST NOT 影响已保存事件，MUST NOT 记为成功。重试 MUST 由用户再次确认触发，MUST 新增记录而不是覆盖历史。

#### Scenario: 零事件跳过

- **WHEN** 已确认的策略匹配不到事件
- **THEN** 不发请求，记录 `skipped_no_events`，本地事件数不变

#### Scenario: 目标不可用

- **WHEN** 目标超时
- **THEN** 记录 `failed` 与超时类别，本地事件数与内容不变

#### Scenario: 重试新增记录

- **WHEN** 用户对同一策略再次确认投递
- **THEN** 新增一条记录，上一次记录保持原状

### Requirement: 目标配置与密钥处理

Webhook 目标 MUST 在集成选项中配置（名称、URL、密钥）。URL MUST 为 HTTPS 且 MUST NOT 含用户信息或查询串；名称 MUST 为不含 `://` 的短标识。密钥 MUST NOT 出现在 URL、事件、投递记录、服务响应、日志或诊断输出中，且表单 MUST NOT 回填密钥。

#### Scenario: 未配置目标时拒绝

- **WHEN** 未配置目标就请求预览或投递
- **THEN** 返回明确错误且不发请求

#### Scenario: 非 HTTPS 或带用户信息的 URL 被拒绝

- **WHEN** 提交 `http://` 或含 `user:pass@` 或带查询串的 URL
- **THEN** 配置被拒绝

#### Scenario: 名称不能冒充 URL

- **WHEN** 提交含 `://` 的目标名称
- **THEN** 配置被拒绝

#### Scenario: 密钥不出现在任何输出面

- **WHEN** 触发一次预览、一次成功投递与一次失败投递
- **THEN** 服务响应、投递表、日志与诊断输出均不含密钥

### Requirement: 迁移与回滚安全

数据库升级 MUST 只新增投递表与索引，MUST 使用 `IF NOT EXISTS` 以容忍表已存在的回滚后重装场景，MUST NOT 修改既有事件表。升级后既有事件 MUST 仍可按 ID 与按范围查询。

#### Scenario: v1 库升级

- **WHEN** 版本 1 数据库升级
- **THEN** 事件保持可查，投递表与索引就绪

#### Scenario: 重复迁移不抛错

- **WHEN** 投递表已存在时再次执行该迁移
- **THEN** 迁移成功、不抛错、既有数据不变

### Requirement: 导出不留敏感痕迹

导出与投递路径 MUST NOT 把健康值、原始属性、完整载荷、URL 或密钥写入日志；失败日志 MUST NOT 附带异常堆栈。诊断信息 MUST 只包含目标引用、事件计数、投递 ID 与结果类别。

#### Scenario: 日志只含元数据

- **WHEN** 触发一次成功投递与一次失败投递
- **THEN** 日志含目标引用、事件计数与结果类别，不含事件数值、原始属性、URL 或密钥，也不含异常堆栈

### Requirement: 默认无后台外发

集成 MUST NOT 在无用户确认的情况下发起任何外部请求。

#### Scenario: 空闲期零外发

- **WHEN** 集成完成 setup 并保持一段空闲时间
- **THEN** 所有出站请求计数为 0
