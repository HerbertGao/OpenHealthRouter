# Tasks

## 1. 指标注册表与单位转换

- [x] 1.1 创建 `custom_components/openhealthrouter/metrics.py`：`MetricSpec` 记录（metric、allowed_units、canonical_unit）与 `body.weight`（kg，允许 lb）、`body.fat_percentage`（%，无转换）、`body.bmi`（kg/m²，无转换）三个指标的注册表数据，质量代码注册表（首期闭集五个代码：estimated、unit_converted、subject_unconfirmed、observed_time_from_ha_state、missing_context），`get_metric()` 查询未命中返回 None；并验证 `uv run pytest tests/unit -k metric` 相关用例通过
- [x] 1.2 实现 `convert_unit(value, from_unit, to_unit, metric)`：仅支持注册表声明的转换对（首期仅 kg↔lb，系数 0.45359237，往返误差 < 1e-9），不支持转换返回 None；并验证参数化测试覆盖 kg→lb、lb→kg、kg→kg（恒等）、jin→kg（拒绝）四例

## 2. 契约模型

- [x] 2.1 创建 `custom_components/openhealthrouter/models.py`：`ObservedAt`、`Source`、`Provenance`、`Quality`、`Component{metric, value, unit}`、`HealthEvent`，配置 `extra="forbid"`、`populate_by_name=True`、strict、`allow_inf_nan=False`，snake_case 内部名经别名生成器输出 camelCase；包含 `context`、`evidence.rawAttributes`、`quality.confirmation` 字段；并验证 Ruff/Pyright 通过且合法体重事件可实例化
- [x] 2.2 实现 after-validator：`value`/`components` 恰好其一、顶层 `unit` 与 `components` 互斥；`schemaVersion` 必须等于 `"1.0"`；`observedAt` 校验（precision 枚举、`timezone` 经 `zoneinfo` 验证为合法 IANA 名、day 精度时 start/end 均为纯日期、start/end 粒度一致、end>start）；并验证 reject 场景：互斥违规、互斥双缺、非 1.0 版本、day 精度带时刻、粒度不一致、end<=start、非法时区
- [x] 2.3 实现单位校验与转换联动：`metric` 须在注册表、单位须在允许集合（含每个组件级 metric/unit），发生转换时自动改写单位并追加 `unit_converted` 到 `quality.issues`；并验证体重 lb→kg 场景输出 kg + `unit_converted`，未注册指标、不允许单位、组件级违规被拒绝
- [x] 2.4 实现标记规则：`provenance.type == "derived.vendor"` 时 `quality.issues` 必须含 `estimated`，否则拒绝；`quality.issues` 成员必须在首期代码注册表内，未注册代码拒绝；estimated 判定基于输入、先于单位转换 validator 执行（design D5 顺序①）；并验证正反用例
- [x] 2.5 创建 `custom_components/openhealthrouter/identity.py`：两模式身份（有 `source.nativeId` 用原生记录身份；无则内容散列），共同规则——`recordedAt`/`quality` 不参与、datetime 归一 UTC 后序列化、components 按 metric 排序、紧凑排序 JSON + UTF-8 + SHA-256，输出 `evt_` + hex；模块不 import models（由 models 单向引入）；`canonical_json()` 内置数值有限性检查（纵深防御）；并验证：同输入两次 ID 一致、仅改 `recordedAt` 或 `quality` ID 不变、同 nativeId 不同值 ID 相同、组件乱序 ID 相同、+08:00 与 Z 表示 ID 相同、NaN 输入抛错
- [x] 2.6 实现事件 ID 自算：`model_validator(mode="after")` 调用 `identity.event_id()` 计算并赋值 `event_id`，输入已带 `eventId` 且与计算值不符则拒绝；该 validator MUST 在单位转换 validator 之后执行（ID 基于规范化值）；并验证无输入 ID 时自动赋值、错误输入 ID 被拒

## 3. Schema 生成与契约检查

- [x] 3.1 实现 `scripts/check_contracts.py` 真实逻辑：`--fix` 以 `model_json_schema(by_alias=True)` 生成、注入 `$schema` 声明、固定序列化（`indent=2, sort_keys=True, ensure_ascii=False` + 末尾换行）写入 `contracts/health-event.v1.schema.json`；默认行为生成到内存与已提交文件逐字节 diff，不一致则失败并提示 `--fix`；并验证模型不变时 check 通过、手工篡改已提交 Schema 后 check 失败、`--fix` 后恢复通过
- [x] 3.2 运行 `--fix` 生成 `contracts/health-event.v1.schema.json` 并提交生成物；并验证生成文件属性名为 camelCase、含 `$schema`、check 默认模式通过
- [x] 3.3 核对 CI workflow 契约检查步骤调用（无需改动，占位脚本行为已替换）；并验证 CI YAML 语法合法

## 4. Golden Vectors 与契约测试

- [x] 4.1 创建 `tests/fixtures/synthetic/vectors/` 与向量格式说明（向量字段：`name, input, expect, expected_id? (仅 accept), expected_issues?, expected_error_contains? (仅 reject，断言错误消息包含子串)`），落地首批向量 JSON（至少覆盖：合法体重、合法日期级区间（两端纯日期、无 nativeId 模式以锁定时刻不补全）、合法日期级体脂率且 derived.vendor+estimated、同 nativeId 差异值去重、组件乱序同 ID、时区等价同 ID、未知字段拒绝（断言含字段名）、不允许单位拒绝（断言含允许单位列表）、未注册质量代码拒绝（断言含代码集）、互斥同时出现拒绝、互斥双缺拒绝、NaN 拒绝、Infinity 拒绝、day 精度带时刻拒绝、纯日期配 second 精度拒绝、naive 时间（无时区）拒绝、非法时区拒绝、未注册指标拒绝、厂商派生缺 estimated 拒绝（含一例带 lb 单位的交互用例：转换未执行即拒绝 estimated 缺失，断言消息含 estimated）、输入 eventId 不符拒绝、空 issues 边界），accept 向量含期望 `eventId` 与 `issues`；并验证向量文件均为合法 JSON 且命名符合 `<source>-<scenario>.json`
- [x] 4.2 创建 `tests/contract/test_vectors.py`：参数化加载全部向量，accept 断言实例化通过且 `event_id`/`issues` 精确匹配期望值，reject 断言 `ValidationError` 且消息包含 `expected_error_contains`（若声明）；并验证全部向量测试通过
- [x] 4.3 引入 `jsonschema` 开发依赖，为 test_vectors.py 增加双跑：每个 accept 向量的 input 另经独立 Schema 校验器对照 `contracts/health-event.v1.schema.json` 复验通过；并验证双跑用例通过（注册表类规则不在 Schema 覆盖范围，由 5.2 保证）

## 5. 依赖声明与全量回归

- [x] 5.1 在 `manifest.json` requirements 加入 pydantic 版本声明——实现期修正：hassfest 按 HA dev 约束（2.14.1）校验，与稳定版 HA 2026.10.4 所带 2.13.5 冲突，改用 `pydantic>=2.12.2,<3` 区间覆盖双分叉（design D8 实现期修正段）；docs 支持矩阵已声明（HA >= 2026.10.x、Python >= 3.14.2）；`uv pip list` 中 pydantic 仅一个版本
- [ ] 5.2 全量回归：ruff format --check、ruff check、pyright、check_contracts.py、pytest、敏感数据扫描全部通过；并验证独立 review 无阻断项后推送 CI 全绿

## Workflow follow-up

- 本变更被审查接受后归档；归档前确认 docs/roadmap.md 第 1 期验收条目（体重事件、derived.vendor 标记、日期级精度、NaN/未知字段/互斥/非法时间拒绝、事件 ID 一致性、Schema/向量一致）全部满足。
