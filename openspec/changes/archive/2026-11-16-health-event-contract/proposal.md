# Proposal

## Why

所有后续阶段（SQLite 存储、HA 实体映射、导出）都建立在统一数据语言之上。当前仓库只有 `docs/health-event.md` 中的逻辑模型描述，没有机器可校验的契约：字段约束、时间语义、单位规则、事件 ID 稳定性都无法自动验证，完全 AI 编码下多套手写定义必然漂移。第 1 期先把契约变成可执行、可生成 Schema、可用 golden vectors 回归的单一事实源。

## What Changes

- 新增 Pydantic v2 模型（`HealthEvent`、`ObservedAt`、`Source`、`Provenance`、`Quality`、`Component`），包含冻结逻辑模型的全部字段（含 `context`、`evidence.rawAttributes`、`quality.confirmation`），以 `docs/health-event.md` 为编写依据。
- 定义 `Component{metric, value, unit}` 形状与组件级注册表校验；顶层 `unit` 与 `components` 互斥。
- 新增指标注册表（首期限定：`body.weight`、`body.fat_percentage`、`body.bmi`），含允许单位与确定性单位转换；未来复合指标（血压）以注册表声明字段扩展，无需改契约形状。
- 定义 `quality.issues` 首期闭集（五个稳定代码）与注册表校验；新增代码为兼容变更。
- 新增两模式事件 ID（与 `docs/health-event.md` §7 对齐）：有 `source.nativeId` 时以原生记录身份去重（第 5 期多通道验收根基），无则以规范化内容散列；模型自算 `eventId`，输入不符即拒绝。
- 新增 `contracts/health-event.v1.schema.json` 自动生成；`scripts/check_contracts.py` 从占位升级为真实校验（重新生成 Schema 并检查 Git diff 无漂移）。
- 新增 golden vectors：合法、非法（未知字段、互斥违规、互斥双缺、NaN/Infinity、非法时间/时区、不支持指标/单位、未注册质量代码）、边界（日期级精度、空 issues）；accept 向量另经生成 Schema 独立双跑（新增 jsonschema 开发依赖）。
- 新增 manifest.json `requirements: ["pydantic==<pin>"]`——本项目首个运行时第三方依赖（预批准见 docs/technology-stack.md §3.2/§4.2）；pin 等于最低支持 HA 版本所带入的 pydantic 精确版本（uv.lock 存在双版本分叉，见 design D8）。
- `scripts/check_contracts.py` 占位逻辑（SKIP）替换为真实校验，CI 步骤调用不变。

## Capabilities

### New Capabilities

- `health-event`: 健康事件的校验、规范化与身份生成。涵盖字段与类型约束、时间精度语义、单位合法性与转换、来源/派生/质量标记、事件 ID 稳定性、JSON Schema 契约发布。

### Modified Capabilities

（无——项目尚无主 specs。）

## Impact

- 新增代码：`custom_components/openhealthrouter/models.py`、`metrics.py`、`identity.py`（事件 ID 与规范序列化合并为一个模块，避免循环依赖，见 design D1）。
- 结构说明：实现文件布局以 design D1/D4 为准；与 docs/technology-stack.md §7 目录树中 `contracts/metrics/`、`serialization.py` 的表述有偏差，以本变更为准（第 0 期后技术栈文档未更新）。
- 新增数据：`contracts/health-event.v1.schema.json`（生成物）、`tests/fixtures/synthetic/` 下 golden vectors 扩充。
- 修改：`manifest.json`（requirements 增加 pydantic 精确版本）、`scripts/check_contracts.py`（占位→真实）。
- 新增运行时依赖：pydantic（精确 pin 于 manifest 与 uv.lock）。
- 破坏性：无（本变更不读写任何既有数据；schemaVersion 从 1.0 起）。
- 阻塞后续：第 2 期存储、第 3 期映射都依赖本契约。
