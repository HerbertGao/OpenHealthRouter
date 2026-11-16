# Tasks

## 1. 包管理与工具配置

- [x] 1.1 创建 `pyproject.toml`（项目元信息、dev 依赖：ruff/pyright/pytest/pytest-asyncio/pytest-homeassistant-custom-component/homeassistant）、`.python-version`（3.14）与 `.gitignore`（含 `.venv/`、`__pycache__/`、`.pytest_cache/`、`uv.lock` 不忽略），并验证 `uv lock` 成功生成 `uv.lock` 且 `uv sync` 可安装
- [x] 1.2 在 `pyproject.toml` 写入 Ruff 配置（`[tool.ruff]`、`[tool.ruff.lint]` 选默认规则集 + `I` 导入排序、`[tool.ruff.format]`）与 Pyright 配置（`[tool.pyright]`，basic 模式，仅检查本仓库 `custom_components/`、`scripts/`、`tests/`），并验证 `uv run ruff check` 与 `uv run pyright` 在空代码状态下正常退出
- [x] 1.3 在 `pyproject.toml` 写入 pytest 配置（`[tool.pytest.ini_options]`：`asyncio_mode = "auto"`、`testpaths = ["tests"]`），创建 `tests/conftest.py` 与 `tests/unit/`、`tests/contract/`、`tests/storage/`、`tests/integration/`、`tests/fixtures/synthetic/` 目录及占位 `__init__.py`，并验证 `uv run pytest` 以"无测试收集"状态退出码 0/5 且路径正确

## 2. 工作边界与合成数据约定

- [x] 2.1 编写 `AGENTS.md`：统一命令清单（`uv sync`、`uv run ruff format`、`uv run ruff check`、`uv run pyright`、`uv run pytest`、`openspec-cn` 常用命令）、OpenSpec 工作流入口（规格改动先 propose、实现用 apply、完成归档 archive）、敏感数据禁区（测试与 CI 禁用真实健康数据与真实设备导出、日志禁止健康值与密钥）、人工审批点（数据库迁移、网络出口、发布），并验证其中每条命令按原样可执行
- [x] 2.2 编写 `tests/fixtures/synthetic/README.md`：合成数据约定（固定时间戳如 `2026-01-01T00:00:00+08:00`、通用体重值 72.4、禁止真实姓名与真实日期模式），并验证约定文本覆盖 proposal 第 0 期验收所列全部数据类别（规范事件、原始属性、测量者标识、备注、密钥、审计）

## 3. 集成元数据与占位校验

- [x] 3.1 创建 `custom_components/openhealthrouter/manifest.json`（`domain: openhealthrouter`、`name`、`version: 0.1.0`、`codeowners`、`documentation` 指向本仓库、`requirements: []`、`iot_class: local_push`），并验证通过 hassfest（本地 `uv run python -m script.hassfest --integration-path custom_components/openhealthrouter` 或 CI action）无错误
- [x] 3.2 创建 `custom_components/openhealthrouter/__init__.py` 与 `strings.json` 最小合法内容（空 docstring、`{"title": "OpenHealthRouter"}`），并验证 Ruff 与 Pyright 对 `custom_components/` 检查通过
- [x] 3.3 创建 `scripts/check_contracts.py` 占位：校验 `contracts/health-event.v1.schema.json` 存在时其为合法 JSON，不存在时打印跳过提示并以退出码 0 结束；并验证 `uv run python scripts/check_contracts.py` 在当前仓库（尚无该文件）输出跳过提示且退出码 0

## 4. CI 流水线

- [x] 4.1 创建 `.github/workflows/ci.yml`：checkout → `astral-sh/setup-uv`（启用缓存）→ `uv sync` → 依次执行 `ruff format --check`、`ruff check`、`pyright`、`scripts/check_contracts.py`、`pytest` → `home-assistant/actions/hassfest@master`，并验证 workflow YAML 语法合法（`python -c "import yaml; yaml.safe_load(...)"` 或 act 干跑）
- [x] 4.2 在 CI 中加入"敏感数据静态扫描"步骤（grep 检查 `tests/fixtures/` 与 workflow 产物路径不含已知真实数据模式，如真实手机号段），并验证该步骤对合成 fixture 不误报、对注入的真实模式样例能失败

## 5. 端到端流程验证

- [x] 5.1 在仓库中做一次最小模拟改动（如向 `tests/fixtures/synthetic/` 添加一个合成样例 JSON），实际走"本地全部门禁 + 独立 review"流程，并验证最终提交上 Ruff/Pyright/pytest/check_contracts 全部通过
- [x] 5.2 验证 CI 在首个真实提交上全绿，并确认 `AGENTS.md` 中描述的 Agent 全流程（规格 → 实现 → 门禁 → review）可被新的 Agent 会话仅依据仓库内文件复现

## Workflow follow-up

- 本变更被审查接受后，由 `openspec-cn archive` 归档，归档前确认 docs/roadmap.md 第 0 期验收条目全部满足。
