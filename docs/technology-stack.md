# 技术栈与 AI 编码工作流

本文把首版架构落实为具体技术选型。当前仍是设计文档，不代表下列文件、命令或 CI 已经存在。

## 1. 结论

首版采用 **HA 原生 Python Integration + Pydantic 契约 + 标准库 SQLite + HA 原生 Web 能力**，开发过程采用 **OpenSpec 规格驱动 + uv/Ruff/Pyright/pytest 自动门禁 + 独立 AI Review**。

完全 AI 编码不等于取消验证或人工责任。AI 编写实现、测试、迁移和文档；人负责确认需求、审批数据删除/迁移/安全变更以及合并发布，不需要手写代码。

不引入 FastAPI、SQLAlchemy、Alembic、Redis、Celery、React、独立 Add-on 或多智能体运行平台。它们都没有为首版的单进程、低并发、事件驱动链路提供必要收益。

## 2. 技术栈总表

| 层 | 选型 | 用途 | 备注 |
| --- | --- | --- | --- |
| 实现语言 | Python | Integration、领域逻辑、存储和导出 | 版本跟随目标 HA Core，不自行选择另一套运行时 |
| 宿主 | Home Assistant Core custom integration | 配置、实体订阅、认证、状态和生命周期 | 首版唯一必需运行时 |
| HA 配置 | Config Entries、Config Flow、Options Flow、Selectors | 映射、目标和本地测量者配置 | 单实例 Config Entry；不要求 YAML |
| HA 运行时 | `ConfigEntry.runtime_data` | 保存类型化 Store、Exporter 等运行对象 | 不使用全局 `hass.data` 字典堆放状态 |
| 领域模型 | Pydantic v2 | 严格校验、类型化模型、生成 JSON Schema | 唯一新增运行时第三方依赖 |
| HA 输入校验 | `voluptuous`、HA Selectors | Config Flow 和 Service 的 HA 边界校验 | 不拿 Pydantic 替换 HA UI 契约 |
| 数据存储 | Python `sqlite3` + SQLite WAL | 事件、投递和审计 | 参数化 SQL；不使用 ORM |
| 时间 | `datetime`、`date`、`zoneinfo` | RFC 3339、日期级精度和时区 | 不引入 Arrow/Pendulum |
| 序列化 | `json` + Pydantic JSON 导出 | 规范事件、NDJSON、稳定散列输入 | 禁止 NaN/Infinity |
| 身份与签名 | `hashlib`、`hmac`、`secrets`、`uuid` | SHA-256 事件 ID、HMAC、随机投递 ID | 不引入认证服务 |
| HTTP | HA 注入的 `aiohttp.ClientSession` | 用户确认后的 HTTPS Webhook | 共享 session，不自行创建连接池 |
| 文件下载 | 经 HA 认证的管理员 `HomeAssistantView` | 流式 NDJSON 下载 | 不写 `www`，不留临时明文导出 |
| 日志 | Python `logging` + HA 日志 | 结构化错误类别和关联 ID | 不记录健康值、密钥或完整载荷 |
| 包与环境 | `uv`、`pyproject.toml`、`uv.lock` | 开发环境、锁定开发依赖 | 不发布 PyPI 包；HA 负责运行集成 |
| 代码质量 | Ruff、Pyright | 格式化、Lint、静态类型检查 | 不重复引入 Black/isort/Flake8/mypy |
| 测试 | pytest、pytest-homeassistant-custom-component | 单元、契约、SQLite 和 HA 集成测试 | 使用合成健康数据 |
| 规格工作流 | OpenSpec + `AGENTS.md` | AI 可检索的需求、任务、边界和验收 | 不另建复杂项目管理服务 |
| CI | GitHub Actions | 干净环境门禁、契约检查、hassfest | AI 生成 diff 不能替代 CI 结果 |

## 3. 语言与版本策略

### 3.1 为什么是 Python，而不是 TypeScript

Home Assistant custom integration 的宿主接口是 Python。即使 TypeScript 在前端 AI 编码中常见，引入它也只会让首版多一个进程、IPC 协议和部署单元。

Python 对本项目的优势是：HA API 可直接调用，AI 训练语料和示例充足，Pydantic/pytest 生态成熟，纯函数映射和契约样例容易测试。首版没有自定义前端，因此不需要 TypeScript。

未来如果确实需要复杂自定义面板，再在独立前端目录使用 HA 原生 Lit/TypeScript；不得为了“AI 更熟悉”提前增加这一层。

### 3.2 版本不追逐最新

- Python 开发版本与最低支持的 HA Core 版本保持一致，写入 `.python-version` 和 `pyproject.toml`。
- CI 至少覆盖最低支持 HA 版本和当前目标 HA 版本；不同 HA 版本使用各自要求的 Python 环境。
- `manifest.json` 的 Pydantic requirement 使用 HA 要求的精确版本 pinning，开发依赖也由 `uv.lock` 锁定。
- HA Core 只作为开发/测试依赖，不写入 Integration 的运行时 `requirements`。
- 升级 HA、Python 或 Pydantic 都是独立 OpenSpec 变更，必须运行完整契约和集成测试，不做无审阅的自动升级。

这样避免文档写死某个 Python 小版本，而 HA 稳定版与开发版要求已经变化。

## 4. 运行时设计

### 4.1 HA 原生边界

首版只允许一个 Config Entry，对应一个 HA 实例、一个 SQLite 数据库和一个写锁。运行对象通过类型化 `ConfigEntry.runtime_data` 管理，卸载时取消监听并等待在途写入结束。

实体接入是事件驱动，不是轮询，因此不需要 `DataUpdateCoordinator`。状态 Sensor 由接入/导出结果驱动更新。

HA 边界继续使用原生能力：

- Config Flow / Options Flow 管理映射与目标；
- `voluptuous` 和 Selector 校验 UI/Service 输入；
- `async_track_state_change_event` 订阅选定实体；
- `hass.async_add_executor_job` 执行同步 SQLite 工作；
- HA 注入的 aiohttp session 发送 Webhook；
- 需要认证的 `HomeAssistantView` 提供 NDJSON 下载。

Config Entry 中的 Webhook 密钥对拥有 HA 主机或备份访问权的人仍可能可读。不得宣称 HA 提供了独立 KMS；保护措施是限制主机与备份访问、禁止日志和诊断输出。

### 4.2 Pydantic 与 JSON Schema 单一来源

Pydantic 模型是 HealthEvent 和内部 Mapping 定义的**编写来源**，生成的 JSON Schema 是发布给外部消费者的契约。两者不能分别手改。

```text
Pydantic models
    ├─ 运行时严格校验 → 类型化 Python 对象
    └─ 生成并提交 contracts/health-event.v1.schema.json
                              ├─ 外部消费者
                              └─ golden vectors 校验
```

模型约束：

- `extra = "forbid"`，未知字段默认拒绝；
- 使用严格类型、有限浮点数和显式字段别名；
- `value` / `components` 互斥在模型层验证；
- 时间精度、区间顺序和引用关系由确定性 validator 验证；
- 映射器先产生候选模型，后续内部函数只接收已验证类型，不重复校验；
- Schema 使用版本化文件名，兼容变更与 breaking change 按契约主版本管理。

CI 重新生成 Schema 并检查 Git diff，防止 AI 只改模型而忘记发布契约。golden vectors 同时跑 Pydantic 和生成后的 JSON Schema，包含合法、非法和边界样例。

不选 dataclass + 手写 JSON Schema，是因为完全 AI 编码时，两套手写定义更容易漂移。不引入独立 `jsonschema` 运行时依赖；如契约测试需要独立 Schema 验证器，它只属于开发依赖。

### 4.3 序列化与事件 ID

首版事件 ID 由本 Integration 生成：对规定字段构造规范字典，再以 UTF-8、排序 key、无多余空白的 JSON 字节计算 SHA-256，输出 `evt_` 加小写 hex。

规范序列化必须：

- 排除 `recordedAt`、日志、审计等非身份字段；
- 在散列前完成指标、单位、时区和时间精度规范化；
- 拒绝 NaN/Infinity；
- 固定空值与缺省字段规则；
- 用 golden vectors 锁定结果。

首版不宣称该 JSON 序列化是通用 RFC 8785 实现。未来允许外部生产者自行生成事件 ID 或要求跨语言重算时，再单独评估标准 JSON Canonicalization Scheme；当前不为此增加依赖。

## 5. SQLite 数据访问与可靠性

### 5.1 为什么不用 ORM

首版只有少量事件、投递和审计查询，SQL 形态稳定。`sqlite3` 足以表达唯一约束、外键、事务和迁移，SQLAlchemy/Alembic 不会自动解决锁、备份、删除或 HA 事件循环问题。

ORM 的升级触发条件是：关系查询和迁移复杂到手写 SQL 已难以验证，或者确实需要支持第二种数据库。数据量大但查询仍简单，不构成引入 ORM 的理由。

### 5.2 最小可靠写入模型

- 一个 Config Entry 对应一个 `asyncio.Lock` 写锁，所有写事务串行。
- 每次数据库操作在独立连接中完成，并在一个 `hass.async_add_executor_job` 调用内执行完整同步函数；连接不跨线程复用。
- 初始化启用 WAL；每个连接都设置 `foreign_keys=ON`、`busy_timeout` 和 `synchronous=FULL`。
- 多步写入使用 `BEGIN IMMEDIATE`，事件、修正关系和同源游标在同一事务提交。
- `eventId` 唯一约束是最终幂等保障，应用层查询不能替代数据库约束。
- 读查询短事务、参数化、分页，使用索引列过滤，不在 `payload_json` 上做临时全表 JSON 搜索。
- 任何 `await`、网络请求或 HA 调用都不能发生在 SQLite 事务内。

首版利用健康测量低频特点，不另写有界队列或常驻数据库线程。若实测出现持续写入积压，再基于指标设计持久化背压；不能凭理论峰值增加第二套任务系统。

### 5.3 数据布局

事件表保留规范 `payload_json`，同时把 `event_id`、`subject_ref`、`metric`、观察起止时间、接收时间和 `correction_of` 作为真实列/索引。投递与审计表只保存目标本地引用、过滤摘要、事件 ID、状态、时间和错误类别，不复制健康载荷。

数据库位于 `hass.config.path("openhealthrouter", "events.db")`。专用目录权限设为仅 HA 运行用户可访问，数据库文件权限按 `0600` 管理；不直接写 HA `.storage` 内部文件。

### 5.4 迁移、备份与删除

- 使用 `PRAGMA user_version` 和按版本排列的 Python 迁移函数，禁止 ORM 自动改写生产表。
- 迁移在订阅实体之前执行；每个迁移在单个事务内完成，失败则拒绝启动写入。
- 升级前使用 SQLite Backup API 创建一致性快照，限制快照权限，并在测试中演练恢复。
- 运行中不能只复制主 DB 文件充当一致性备份；WAL 状态和 HA 备份范围必须纳入恢复测试。
- 恢复后执行 `integrity_check`、外键检查和关键业务不变量检查。
- 删除表示从活动数据库及后续导出中移除；不承诺 SSD 物理擦除，也不重写既有 HA 备份、已下载 NDJSON 或远程接收方数据。
- 首版不自动设置保留期，不静默清理，也不把 `secure_delete`/`VACUUM` 宣称为可证明擦除。

## 6. 导出与安全实现

### 6.1 NDJSON

NDJSON 通过 HA 认证视图流式下载，并要求当前 HA 用户具有管理员权限。请求复用统一 Export Policy，数据库分批读取后立即写响应，不生成完整临时文件，也不把文件放入公开 `www` 目录。

这不是多人健康数据权限系统；首版只把配置、导出和删除开放给受信任的 HA 管理员。

### 6.2 Webhook

- 仅用户确认后触发，默认没有后台外发。
- 使用 HA 注入的共享 aiohttp session、有限超时和严格 TLS 证书校验。
- 同一份规范化 body bytes 同时用于 HMAC-SHA256 签名和网络发送，避免签名内容与实际载荷不同。
- 请求包含时间戳、随机 UUID 投递 ID 和签名；接收方负责时间窗口及投递 ID 重放校验。
- 网络结果只更新投递记录，不回滚已提交健康事件。
- 首版失败由用户明确重试，不引入 Celery、Outbox 守护进程或自动重试策略。

## 7. 面向 AI 的仓库结构

以下是实现目标结构，保持浅层模块，避免为每个小函数创建包：

```text
OpenHealthRouter/
├── AGENTS.md                    # 工作边界、命令、禁区，不重复需求
├── pyproject.toml               # uv/Ruff/Pyright/pytest 统一配置
├── uv.lock
├── .python-version
├── custom_components/openhealthrouter/
│   ├── manifest.json
│   ├── __init__.py              # HA 生命周期与 runtime_data
│   ├── config_flow.py
│   ├── models.py                # Pydantic 契约编写来源
│   ├── mapping.py               # 纯函数实体映射与规范化
│   ├── store.py                 # 参数化 SQL、事务与查询
│   ├── migrations.py            # user_version 迁移
│   ├── export.py                # 统一过滤、NDJSON 与投递协调
│   ├── webhook.py               # HMAC 与共享 aiohttp session
│   ├── view.py                  # 认证管理员 NDJSON 下载
│   ├── sensor.py                # 少量状态投影
│   └── strings.json
├── contracts/
│   ├── health-event.v1.schema.json # 生成，不手改
│   ├── metrics/                 # 指标、单位和转换规则
│   └── vectors/                 # 跨任务复用的合法/非法样例
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── storage/
│   └── integration/
├── openspec/
│   ├── specs/                   # 已接受能力的 truth source
│   └── changes/                 # 提案、delta spec、tasks
├── scripts/check_contracts.py
└── .github/workflows/ci.yml
```

`docs/` 解释架构和决策原因；`openspec/specs/` 描述可验收需求；Pydantic/Schema 定义数据形状；`AGENTS.md` 只告诉 Agent 如何工作。四者不复制同一份字段说明。

## 8. 完全 AI 编码工作流

### 8.1 最简闭环

1. **规格**：主 Agent 为一个窄变更创建 OpenSpec proposal、delta specs 和 tasks，明确非目标、验收场景、隐私与迁移影响。
2. **风险审批**：普通实现可直接继续；需求扩张、数据库迁移/删除、安全边界、关键依赖和发布必须先由人确认。
3. **实现**：AI 同时编写最小实现及直接相关测试。测试变化必须能追溯到 delta spec，不允许仅为变绿而删除断言。
4. **本地门禁**：运行格式化、Lint、类型、契约和测试；失败由 AI 修复根因，不关闭规则。
5. **独立 Review**：非作者 Agent 对照规格检查 diff、调用方、迁移与测试有效性；作者自审不能作为通过证据。
6. **CI**：在干净检出和锁定环境中重复全部门禁，保存对应提交的结果，不产生含健康数据的构建产物。
7. **人工合并/发布**：AI 不自行 commit、push、merge 或发布，除非获得该目标的明确授权。

### 8.2 Agent 分工

默认一个主 Agent 完成一个小任务，避免多 Agent 上下文和合并成本。只有文件边界清晰时才并行：

- 主 Agent：维护 OpenSpec、选择任务、集成修改和最终验证；
- Explore Agent：只读定位 HA API、调用方或既有测试；
- 实现 Subagent：分别处理契约、存储或导出中的独立任务，不共享文件；
- Code Reviewer：只读检查正确性、安全和规格符合性；
- Reality Checker：核对“已完成”是否有实际测试和真实 HA 行为证据。

不运行常驻多智能体服务，不让多个 Agent 同时修改同一文件，不让 reviewer 的建议自动扩大需求。

### 8.3 AI 完成定义

“AI 写完”只表示生成了 diff。“完成”必须同时满足：

- 对应 OpenSpec 验收场景有执行证据；
- Ruff、Pyright、pytest、契约检查和 hassfest 全部通过；
- 关键存储故障、权限和导出边界有测试，而非只测 happy path；
- 独立 Review 无阻断项；
- CI 验证的是同一个提交；
- 文档、Schema、迁移和任务状态一致。

绿灯仍不能证明医学语义正确。真实设备映射必须由合成 golden vector 加一次用户确认的真实设备验证共同确认，真实健康数据不得进入仓库或 CI。

## 9. 测试与 CI 分层

| 层 | 必测内容 | 工具 |
| --- | --- | --- |
| 纯单元 | 映射、单位、时间精度、事件 ID、过滤、HMAC | pytest |
| 契约 | Schema 生成无漂移、golden vectors、非法字段、版本兼容 | Pydantic、Schema 校验脚本 |
| 存储 | 幂等、事务回滚、外键、修正链、删除、并发写、逐版本迁移、备份恢复 | 临时 SQLite |
| HA 集成 | Config Flow、实体监听、卸载、runtime_data、管理员视图、禁止递归摄入 | pytest-homeassistant-custom-component |
| 导出 | 同一过滤结果、NDJSON 分帧、TLS/超时、签名重放、失败不丢本地事件 | aiohttp 本地测试服务 |
| 纵向验收 | 合成体脂秤状态 → 事件 → 查询 → NDJSON/Webhook | HA 测试夹具 |

CI 顺序为：Ruff format check → Ruff lint → Pyright → 契约生成/向量检查 → pytest → hassfest。测试不使用真实个人健康数据，不访问真实外部 Webhook，也不以覆盖率数字替代关键风险场景。

## 10. 明确拒绝的首版依赖

| 依赖或机制 | 不采用原因 | 重新评估条件 |
| --- | --- | --- |
| FastAPI / 独立 REST 服务 | HA 已提供配置、认证视图和生命周期 | HealthTracker 等外部客户端确需批量摄入 |
| SQLAlchemy / Alembic | 当前 SQL 与迁移数量有限 | 多数据库或复杂关系迁移成为主要维护成本 |
| PostgreSQL / TimescaleDB | 增加独立服务和备份运维 | 多进程、多机写入或规模实测超 SQLite |
| Redis / Celery / Outbox worker | 首版没有自动后台投递 | 用户已接受无人值守同步及运维成本 |
| React / 自定义前端 | Config Flow 和认证下载足够 | HA 原生 UI 无法表达关键工作流 |
| Pint 等单位库 | 首版指标少，显式转换更易审计 | 指标与复合单位增长到难以维护 |
| Sentry / 云遥测 | 健康数据泄露面与外部依赖不必要 | 有明确脱敏、自托管和用户授权方案 |
| 多智能体运行平台 | 开发编排不需要生产运行时 | 任务规模证明主 Agent 无法可靠管理 |
| FHIR / MCP SDK | 出口尚未进入首版 | 对应 OpenSpec 能力被明确接受 |

## 11. 推荐实施顺序

按工程骨架、HealthEvent 契约、本地存储、HA 接入、主动导出与体脂秤验收、第二连接器的顺序实施。每一步都应独立验收，不允许先让 AI 生成整个平台再集中调试。

具体交付物、进入条件、人工审批点和阶段完成定义见[分期实施计划](roadmap.md)。

## 12. 官方参考

- [Home Assistant：Integration manifest](https://contributors.home-assistant.io/core/integrations/manifest/)
- [Home Assistant：Async 与 executor](https://contributors.home-assistant.io/core/internals/asyncio/)
- [Home Assistant：ConfigEntry runtime_data](https://contributors.home-assistant.io/core/quality-scale/runtime-data/)
- [Home Assistant：注入 Web Session](https://contributors.home-assistant.io/core/quality-scale/inject-websession/)
- [Pydantic：JSON Schema](https://docs.pydantic.dev/latest/concepts/json_schema/)
- [Python：sqlite3 Backup API](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup)
- [SQLite：PRAGMA](https://sqlite.org/pragma.html)
- [uv](https://docs.astral.sh/uv/)、[Ruff](https://docs.astral.sh/ruff/)、[Pyright](https://microsoft.github.io/pyright/)、[pytest](https://docs.pytest.org/en/stable/)
- [OpenSpec](https://github.com/Fission-AI/openspec)
