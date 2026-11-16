# Proposal

## Why

本项目计划完全由 AI 编码，但当前仓库只有设计文档，没有任何可执行工程骨架：没有统一的包管理与锁定环境，没有 Lint / 类型 / 测试 / CI 门禁，没有 Agent 工作边界说明。第 0 期先把这条"AI 安全修改代码的轨道"铺好，否则从第 1 期（HealthEvent 契约）开始的所有实现都会在不一致的环境中产生不可复验的 diff。

## What Changes

- 新增 `pyproject.toml`、`.python-version`、`uv.lock`：统一声明开发依赖（uv 管理），并锁定版本。
- 新增 `AGENTS.md`：记录 AI 工作边界、统一命令、敏感数据禁区（不重复需求文档内容）。
- 新增 OpenSpec 变更模板与首个变更示例流程约定，使"规格 → 实现 → 门禁 → Review"可重复。
- 新增 GitHub Actions CI：Ruff format check、Ruff lint、Pyright、契约检查入口、pytest、hassfest。
- 新增测试目录骨架与合成数据约定；所有测试禁止使用真实个人健康数据。
- 新增 HA custom integration 最小 manifest（仅声明，不实现业务功能）。
- 新增契约检查脚本入口 `scripts/check_contracts.py`（占位，供第 1 期填充实现）。

## Capabilities

### New Capabilities

（无——本变更为纯工具链/工程骨架变更，无 spec 层面的行为变化，已在 `.openspec.yaml` 设置 `skip_specs: true`。）

### Modified Capabilities

（无——项目尚无既有 specs。）

## Impact

- 新增文件：`pyproject.toml`、`.python-version`、`uv.lock`、`AGENTS.md`、`scripts/check_contracts.py`、`.github/workflows/ci.yml`、测试目录骨架、`custom_components/openhealthrouter/manifest.json`。
- 新增开发依赖（锁定）：ruff、pyright、pytest、pytest-homeassistant-custom-component、homeassistant（测试用）。
- 运行时依赖：本变更不新增（Pydantic 属第 1 期契约变更）。
- 无破坏性变更；无用户可见行为。
- 后续第 1 期（HealthEvent 契约）依赖本变更的 CI 与测试骨架。
