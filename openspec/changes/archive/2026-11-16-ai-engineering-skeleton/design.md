# Design

## Context

参见 proposal.md - Why。补充现状约束：目标宿主 Home Assistant Core 当前要求 Python >= 3.14（依据 core 仓库 pyproject.toml）；custom integration 的依赖经 manifest.json 的 `requirements` 声明，而开发工具链由仓库自身管理；项目尚无 git 仓库初始化之外的任何工程文件（只有 docs/ 与 openspec/）。

## Goals / Non-Goals

**Goals:**

- AI 在任意干净检出上能用同一条命令链完成"格式化 → Lint → 类型 → 测试"验证。
- 依赖版本完全锁定，环境与 CI 可复现。
- `AGENTS.md` 让任意 Agent 无需对话即可知道工作边界与禁区。
- CI 在合并前重复全部本地门禁，外加 hassfest 校验 integration 元数据。
- 本变更结束时，一次空跑改动（如给 docs 加一行）能走通"OpenSpec → 实现 → 门禁 → Review"全流程。

**Non-Goals:**

- 不实现任何业务功能（无 HealthEvent、无存储、无导出）。Pydantic 契约属于第 1 期。
- 不做 CI 矩阵（多 HA 版本 / 多 Python 版本）。单版本起步，矩阵是后续变更。
- 不引入 pre-commit、Docker devcontainer、发布流水线。
- 不配置覆盖率门槛数字（避免诱导为覆盖率写无意义测试；回归测试要求由第 1 期起按风险逐期定义）。

## Decisions

### D1: 开发环境统一用 uv，`.python-version` 固定 3.14

`pyproject.toml` + `uv.lock` + `.python-version`（`3.14`）。Integration 不发布 PyPI，`pyproject.toml` 仅供开发依赖与工具配置，不作为运行时安装源；运行时依赖（未来的 Pydantic）走 `manifest.json` 的 `requirements`。

- 备选：pip + requirements-dev.txt → 拒绝：无锁定传递依赖的完整解析，AI 环境重建容易漂移。
- 备选：Poetry → 拒绝：同等能力下 uv 更快且配置面更小，与 Ruff/Pyright 同属一个现代工具链。

### D2: 质量工具 = Ruff（format + lint）+ Pyright，不做 mypy/Black/isort

Ruff 统一承担 format 与 lint（配置于 `pyproject.toml` `[tool.ruff]`），Pyright 承担类型检查（`[tool.pyright]`，basic 模式起步，核心纯函数模块后续可在第 1 期升为 strict）。

- 备选：mypy → 拒绝：与 Pyright 二选一即可，Pyright 对 untyped 依赖的容忍策略更适合 HA 生态。
- 备选：Black + isort + flake8 组合 → 拒绝：三工具职责被 Ruff 单一覆盖，配置与执行面更小。

### D3: pytest 配置放在 pyproject，异步测试用 pytest-asyncio（mode=auto）

HA 生态的 `pytest-homeassistant-custom-component` 已携带其所需的 pytest 插件集；本仓库只显式声明 `asyncio_mode = "auto"` 与测试路径，不重复造 fixture。第 0 期只建目录与合成数据约定，第一个真实 fixture 在第 1/3 期落地。

### D4: CI 单 job 顺序执行，uv 缓存加速

GitHub Actions 一个 job 内顺序执行：`ruff format --check` → `ruff check` → `pyright` → `check_contracts.py` → `pytest` → `hassfest`（官方 action `home-assistant/actions/hassfest@master`）。使用 `astral-sh/setup-uv` 并缓存 uv 缓存目录（pytest-homeassistant-custom-component 会拉取 homeassistant 全量依赖，体积大，无缓存时 CI 首次运行可能超过 10 分钟）。

- 备选：多 job 并行 → 拒绝：第 0 期串行足够，且避免重复安装大依赖；出现真实瓶颈再拆分。

### D5: `AGENTS.md` 只写"怎么工作"，不写需求

内容限定：统一命令（`uv sync` / `uv run ruff format` / `uv run ruff check` / `uv run pyright` / `uv run pytest`）、OpenSpec 工作流入口（propose/apply/archive）、敏感数据禁区（测试与 CI 禁用真实健康数据、日志禁打健康值与密钥）、人工审批点（迁移、网络出口、发布）。需求与架构细节继续由 `docs/` 与 OpenSpec specs 承载，`AGENTS.md` 用指针引用而不复制。

### D6: 测试目录与合成数据约定

```text
tests/
├── conftest.py          # 共享夹具（空骨架）
├── unit/                # 纯函数：映射、单位、事件 ID
├── contract/            # Schema 与 golden vectors（第 1 期填充）
├── storage/             # SQLite 行为（第 2 期填充）
└── integration/         # HA 夹具测试（第 3 期填充）
tests/fixtures/synthetic/  # 合成样例，命名 <source>-<scenario>.json
```

约定写入 `AGENTS.md` 与 tests 目录 README：fixture 必须是合成值（如 `72.4` 配固定时间戳），禁止真实设备导出、真实姓名、真实日期模式。

### D7: 最小 manifest 仅满足 hassfest 校验

`custom_components/openhealthrouter/manifest.json` 只含：`domain`、`name`、`version`（custom integration 必填）、`codeowners`、`documentation`（指向本仓库）、空的 `requirements`、`iot_class: local_push`（实体监听语义）。不含 config_flow 声明（第 3 期加，加了 hassfest 就会要求对应代码存在）。

### D8: `scripts/check_contracts.py` 本期为占位

只校验 `contracts/health-event.v1.schema.json` 存在且为合法 JSON Schema（此时文件尚不存在，脚本以退出码 0 + 提示跳过）；第 1 期替换为真实校验（Schema 生成 diff 检查 + golden vectors 双跑）。占位是为让 CI 链路今天就是通的，不留断链。

## Risks / Trade-offs

- [pytest-homeassistant-custom-component 与最新 HA Core 存在版本滞后] → CI 中固定 homeassistant 与该插件的兼容组合并锁定；升级 HA 作为独立变更处理。
- [Pyright 对 HA 内部类型覆盖不全，产生噪音诊断] → 起步 basic 模式 + 仅对本仓库代码检查；第 1 期再评估 strict 范围。
- [uv 缓存失效时 CI 首次运行慢] → 接受；锁文件 + 缓存命中后增量运行通常在分钟级。
- [`.python-version` 与 HA 未来版本要求漂移] → 升级 HA 支持范围时同步更新 `.python-version`，作为独立 OpenSpec 变更验收。
- [hassfest 对 manifest 的要求随 HA 版本演进] → CI 跑 hassfest 本身即防护网，失败即按当期 HA 要求修正。

## Migration Plan

无运行时迁移（本变更不触碰任何运行系统）。落地步骤：

1. `uv init` 骨架 + 写入 `pyproject.toml` 全部工具配置 → `uv lock`。
2. 落地 `AGENTS.md`、tests 骨架、`scripts/check_contracts.py`、manifest。
3. 接入 CI 并在本仓库首个真实提交上验证全绿。
4. 回滚策略：删除新增文件即可，无状态、无数据。

## Open Questions

无。版本号细节（manifest `version` 初值、`codeowners` 账号）属实现期可定事项，不改变方案与任务拆分。
