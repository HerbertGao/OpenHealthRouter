# Design

## Context

参见 proposal.md - Why。现状约束：仓库已有第 0 期骨架（uv + Ruff/Pyright basic + pytest asyncio auto + CI + hassfest 全绿）；`docs/health-event.md` 定义了逻辑模型与示例，是本设计的冻结需求源；`scripts/check_contracts.py` 为占位；开发环境经 homeassistant 2026.10.4 带入 pydantic 2.13.5（实测，`uv pip list`）。本变更是首个产生运行时行为的变更。

## Goals / Non-Goals

**Goals:**

- 契约模型、JSON Schema、golden vectors 三者单一来源、永不手工漂移。
- 校验失败信息对人类与 AI 都可定位（字段名 + 规则）。
- 事件 ID 生成确定性可回归（golden vectors 锁定字节级结果）。
- 契约检查接入 CI，模型变更与 Schema 提交不同步时必红。

**Non-Goals:**

- 不实现 SQLite 存储、实体映射、导出（第 2/3/4 期）。
- 本期注册表只含三个指标；血压复合指标与 BMC 指标后续按期加入（注册表加行即扩展，无需 schema 形状变更）。
- 不实现 metric 级 provenance 硬约束：体脂率/BMI 的 `derived.vendor` + `estimated` 标记由第 3 期映射层负责，契约层只保证一般规则（`derived.vendor` 必须带 `estimated`）。与 docs/health-event.md §5 措辞的口径差异在此声明：roadmap 第 1 期验收以"能表达 + 向量验证"为准，不上升到模型规则。
- 不做医学合理性校验（异常值不拒绝）。
- 不做英文场景文案国际化（quality issue 代码为稳定英文标识即可）。

## Decisions

### D1: 单一 `models.py` 承载全部契约模型；`identity.py` 不 import models

`HealthEvent` 与 `ObservedAt`、`Source`、`Provenance`、`Quality`、`Component` 同文件，理由：首期内聚高于拆分，模型间相互引用同文件最简单。指标注册表独立 `metrics.py`（数据表性质），事件 ID 独立 `identity.py`。

事件 ID 由模型在 `model_validator(mode="after")` 内调用 `identity.event_id()` 自算赋值；若输入已带 `eventId` 且与计算值不符则拒绝。这要求 models 可以 import identity，因此 **identity.py 只对 dict/Any 操作、不 import models**，避免循环依赖。

- 备选：按域拆多个模型文件 → 拒绝：五六个小类跨文件增加 AI 导航成本，无实际解耦收益。

### D2: 模型配置 `extra="forbid"`、`populate_by_name=True`、strict、`allow_inf_nan=False`

Pydantic 模型内部用 snake_case Python 名，经 `alias_generator` 输出 camelCase JSON（`event_id` → `eventId`），与 `docs/health-event.md` 示例一致。配置：`ConfigDict(extra="forbid", populate_by_name=True, strict=True, allow_inf_nan=False)`。`allow_inf_nan=False` 是 NaN/Infinity 拒绝的模型层机制（实测：strict 模式默认**不**拒绝 NaN，该配置才拒绝）。strict 的实际语义是拒绝 str/bool 等向数值的强制转换；int 字面量填入 float 字段被接受（JSON 中 `128` 是合法血压值），此为有意行为。

模型包含冻结逻辑模型的全部字段：`context: dict[str, Any] = {}`、`evidence.rawAttributes: dict[str, Any] = {}`、`quality.confirmation`（`not_required`/`unconfirmed`/`confirmed`，默认 `unconfirmed`）。`extra="forbid"` 下若缺这些字段，第 3/5 期携带 context 的合法事件会被误拒——纳入它们是零成本的未来兼容。

- 备选：容忍模式 + 自定义校验器 → 拒绝：宽容默认与"未知字段必须拒绝"的需求直接冲突。

### D3: `value`/`components` 互斥与组件规则用 `model_validator(mode="after")`

顶层 `unit` 与 `components` 互斥（多组件时单位在各组件内，见 docs §4）。after-validator 检查 `value`/`components` 恰好一者非 None，且 `unit` 与 `components` 不得同现。

- 备选：Discriminated union 标签字段 → 拒绝：向事件 JSON 引入实现泄漏的标签字段，改变对外契约形状。

### D4: 指标注册表为模块级数据 + 纯函数查询；复合指标用声明字段预留

`metrics.py` 定义 `MetricSpec(metric, allowed_units, canonical_unit)` 记录集与 `get_metric(name) -> MetricSpec | None`、`convert_unit(value, from_unit, to_unit, metric) -> float | None`（`from` 是 Python 关键字，不能作参数名）。单位转换表显式列出（kg↔lb，系数 0.45359237，往返误差 < 1e-9；% 与 kg/m² 无转换），不支持转换返回 None。

每个 `Component{metric, value, unit}`（字段按 docs §4：metric、value、unit）适用与顶层相同的注册表与允许单位规则。未来复合指标（如血压 `blood_pressure`）通过给 `MetricSpec` 增加 `component_metrics` 声明实现，本期不实现该字段。

- 备选：Pint 等单位库 → 拒绝：三个指标引入整库过重，显式转换表可审计（与 docs/technology-stack.md 的最小依赖原则一致）。

### D5: 事件 ID 两模式身份（与 docs/health-event.md §7 对齐）

**模式 A（有 `source.nativeId`）**：身份 = `{schemaVersion, subjectRef, metric, source.integration, source.nativeId, deviceRef}`。同一 recordNo 经 BLE/云端/mini-agent 多次到达时值或时间精度差异不影响 ID——这是第 5 期去重验收的根基。

**模式 B（无 nativeId，内容散列）**：身份 = `{schemaVersion, subjectRef, metric, source.entityId, 规范化 value/components, observedAt, provenance}`。

两种模式共同规则：

- `recordedAt`、`quality`（含 issues 追加）不参与身份；
- 散列前规范化：单位已转 canonical；datetime 统一 `astimezone(UTC)` 后 isoformat（同一时刻 +08:00 与 Z 表示产生相同 ID）；`components` 按 `metric` 排序（数组顺序不影响 ID）；
- 规范序列化：`json.dumps(identity_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`；
- 输出 `evt_` + SHA-256 hex；
- 空列表/空字典字段（`issues: []`、`context: {}`）在导出时保留，散列输入中固定缺省规则，由 golden vectors 锁定。

执行顺序约束：模型上的 after-validator 按注册顺序执行，顺序 MUST 为：①派生标记检查（2.4，基于**输入**判定 `derived.vendor` 必须含 `estimated`，一律拒绝缺失输入，不做任何自动补全）→ ②单位转换（2.3，自动追加仅限 `unit_converted`）→ ③`eventId` 自算（2.6，ID 必须基于已规范化的单位与值计算）。Pydantic 同模式 validator 按定义顺序执行，实现时以定义位置保证该顺序。auto-append 永不适用于 `estimated`——缺失即拒绝（与 docs/health-event.md §7"不自动编造"一致）。

首版不宣称 RFC 8785 兼容；外部生产者出现时再评估（与 docs/technology-stack.md 一致）。

### D6: Schema 生成与漂移检查在 check_contracts.py 内闭环

`check_contracts.py` 提供 `--fix`（生成并写盘）与默认 check（生成到内存，与已提交文件**逐字节 diff**，不一致则失败并提示 `--fix`）。生成规则：

- `model_json_schema(by_alias=True)`——默认 `by_alias=False` 会输出 snake_case 属性名，与 camelCase 契约冲突；
- 手工注入 `"$schema": "https://json-schema.org/draft/2020-12/schema"`——`model_json_schema()` 默认不输出该键（实测）；
- 固定序列化：`json.dumps(indent=2, sort_keys=True, ensure_ascii=False)` + 末尾换行，保证跨环境字节一致。

CI 调用默认 check；实现者改模型后运行 `--fix` 提交生成物。

### D7: golden vectors 为 JSON 文件 + 参数化测试，Pydantic 与 Schema 双跑

`tests/fixtures/synthetic/vectors/` 下每场景一个 JSON：`{name, input, expect: "accept"|"reject", expected_id? (仅 accept), expected_issues?}`。`tests/contract/test_vectors.py` 参数化加载：accept 场景断言模型通过且 `event_id`/`issues` 精确匹配；reject 场景断言 `ValidationError`。

双跑（docs/technology-stack.md §4.2 要求）：accept 向量的 `input`  additionally 用 `jsonschema`（仅开发依赖）对照生成的 `contracts/health-event.v1.schema.json` 复验通过。注册表类规则（指标/单位/issues 成员资格）不在 JSON Schema 表达范围内，双跑只覆盖结构性 reject；此类规则由 Pydantic 侧向量保证。

- 备选：场景写在 Python 测试内 → 拒绝：JSON 向量可被 Schema 独立校验工具复用。

### D8: manifest requirements pin 与支持矩阵声明

uv.lock 存在 HA 双版本分叉（实测：`homeassistant 2026.2.3` for python<3.14.2 → pydantic 2.12.2；`homeassistant 2026.10.4` for python>=3.14.2 → pydantic 2.13.5），单一 requirements 字符串无法匹配两个分叉。因此 pin 决策为：**pin 等于"最低支持 HA 版本"所带入的 pydantic 精确版本**（本期 = HA 2026.10.4 → `pydantic==2.13.5`），并在 manifest 注释不可行的前提下于 docs 声明支持矩阵（HA >= 2026.10.x、Python >= 3.14.2）。不 pin 范围（`>=2,<3`）——HA 对依赖做精确校验，范围约束与 HA 自带 pin 冲突会在用户安装时失败。

- 备选：不声明 requirements、复用 HA 内置 pydantic → 拒绝：custom integration 不允许隐式依赖宿主包，hassfest 要求 requirements 显式声明。

**实现期修正（CI 证据驱动）**：hassfest 按 HA dev 约束校验（2026-11 要求 pydantic==2.14.1），而稳定版 HA 2026.10.4 锁 2.13.5——精确 pin 必然与其中一个冲突。改为区间 `pydantic>=2.12.2,<3`：覆盖两个锁分叉，pip 在 HA 环境中解析至 HA 自身 pin，无安装冲突。原"精确 pin"结论限于此事实（hassfest 跟踪 dev 而稳定版滞后）不成立，其余论证（显式声明、支持矩阵）不变。

## Risks / Trade-offs

- [pydantic pin 与支持矩阵绑定，HA 升级需同步] → 升级 HA 支持范围时同步更新 pin（独立 OpenSpec 变更）。
- [strict + `allow_inf_nan=False` 对来源数据中宽松类型直接拒绝] → 映射层（第 3 期）负责转换后构造模型，契约不做隐式转换。
- [golden vectors 锁定事件 ID 字节结果，身份字段集或规范化改动导致全量 ID 变化] → 向量测试即回归网；身份字段集变更按 breaking change 处理，升 schemaVersion 并全量更新向量与 Schema 文件名。
- [Pyright basic 对 Pydantic validator 的推导偶发误报] → 局部 `# type: ignore` 需注释理由，禁止关闭规则。
- [dates vs datetimes：`observedAt.start/end` 允许 date 或 datetime，`precision=day` 时纯日期] → after-validator 强制 start/end 粒度一致（同 date 或同 datetime），`precision=day` 时两者必须均为纯日期；比较前不混用（Python 混比抛 TypeError）。
- [体脂率/BMI 派生标记靠映射层自觉] → 第 1 期向量含 fat_percentage + derived.vendor + estimated 正例；第 3 期映射层 golden cases 再锁一次（docs/entity-mapping.md §7 已有要求）。

## Migration Plan

无数据迁移。本变更合入后契约校验能力生效（占位脚本的 SKIP 路径由真实 check 替代，CI 步骤调用不变）。回滚：还原本变更提交，占位脚本随之还原，不影响第 0 期成果。

## Open Questions

无。pydantic 精确版本号（2.13.5，以 uv.lock 目标分叉实测为准）、`allowedSignersFile` 本地验签配置属实现期事项，不改变方案与任务拆分。
