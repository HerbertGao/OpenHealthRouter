# AGENTS.md

本文件告诉 AI Agent 如何在本仓库工作。需求与架构见 `docs/` 与 `openspec/`，本文件不复制其内容，只给指针、命令与禁区。

## 项目一句话

OpenHealthRouter：本地优先的健康数据路由层，Home Assistant custom integration 形态。设计阶段文档见 `docs/`（架构、HealthEvent、实体映射、隐私、技术栈、分期）。

## 统一命令

```bash
uv sync                    # 安装锁定依赖（含 dev）
uv run ruff format         # 格式化
uv run ruff format --check # CI 用：检查格式
uv run ruff check          # Lint
uv run pyright             # 类型检查（basic）
uv run pytest              # 全部测试
uv run python scripts/check_contracts.py   # 契约检查（占位）
```

本地门禁顺序：ruff format --check → ruff check → pyright → 敏感数据扫描（`grep -rEn '1[3-9][0-9]{9}' tests/fixtures/` 无命中）→ check_contracts.py → pytest。CI 额外跑 hassfest（见 `.github/workflows/ci.yml`）。全部通过才算"验证通过"；仅生成 diff 不算。

## OpenSpec 工作流

- 新能力/需求变更：先 `openspec-cn new change "<name>"`，写 proposal → design → tasks（规格行为变化时写 specs）。
- 实现已有变更：`openspec-apply-change`（按 tasks.md 逐项，完成即勾）。
- 变更完成并审查通过后：`openspec-cn archive "<name>"`。
- 常用：`openspec-cn status --change "<name>" --json`、`openspec-cn validate <name>`。

## 敏感数据禁区

- 测试、fixture、CI 产物禁止真实个人健康数据、真实设备导出、真实姓名/手机号；只用 `tests/fixtures/synthetic/` 约定的合成值。
- 日志、异常、诊断输出禁止健康值、Webhook 密钥、完整请求体。
- 真实设备验证结果只在本机人工确认，不入库。

## 人工审批点（必须停下问人）

1. 数据库 Schema 迁移与删除语义变更。
2. 新增/变更网络出口（Webhook 目标、自动同步）。
3. 新增运行时依赖（manifest.json `requirements`）。
4. commit / push / merge / 发布——除非用户已明确授权该目标。

## 工作约定

- 每次改动最小化，只完成 tasks 描述的行为；发现范围外问题先提出，不默默吸收。
- 测试与实现同一任务完成，不拖欠到最后一组。
- Python 版本与依赖版本以 `uv.lock` / `.python-version` 为准；升级走独立变更。
