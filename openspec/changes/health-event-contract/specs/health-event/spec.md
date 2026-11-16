# Spec Delta

## Purpose

健康事件契约能力：把设备与手工来源的测量统一为可校验、可去重、可发布的 HealthEvent，是所有存储、映射与导出行为的输入边界。

## ADDED Requirements

### Requirement: 事件字段校验与未知字段拒绝

HealthEvent MUST 包含 `schemaVersion`、`subjectRef`、`metric`、`observedAt`、`recordedAt`、`source`、`provenance`、`quality` 字段；`eventId` 可缺省，缺省时由模型按需求"事件 ID 稳定性与两模式身份"自算赋值，携带时与计算值不符 MUST 被拒绝；`recordedAt` MUST 为带时区的日期时间；`schemaVersion` MUST 等于 `"1.0"`；`value` 与 `components` 必须恰好出现一个；顶层 `unit` 与 `components` 不得同时出现；所有字段类型 MUST 匹配契约定义。携带契约未定义字段的事件 MUST 被拒绝。

#### Scenario: 合法单值事件通过校验

- **WHEN** 输入包含全部必填字段、`schemaVersion` 为 `"1.0"`、`metric` 为 `body.weight`、`value` 为有限数值 72.4、单位 `kg` 的事件
- **THEN** 校验通过并产出规范事件对象

#### Scenario: 未知字段被拒绝

- **WHEN** 事件携带契约未定义的任意额外字段
- **THEN** 校验失败并报告该字段名

#### Scenario: value 与 components 同时出现被拒绝

- **WHEN** 事件同时包含 `value` 和 `components`
- **THEN** 校验失败，错误指明两者互斥

#### Scenario: value 与 components 同时缺失被拒绝

- **WHEN** 事件既无 `value` 也无 `components`
- **THEN** 校验失败

#### Scenario: 非有限数值被拒绝

- **WHEN** `value` 为 NaN 或 Infinity
- **THEN** 校验失败，事件不进入任何后续处理

#### Scenario: 非 1.0 版本被拒绝

- **WHEN** `schemaVersion` 为 `"1.0"` 以外的任意值
- **THEN** 校验失败并指明仅接受 `"1.0"`

### Requirement: 观察时间精度语义

`observedAt` MUST 包含 `start`、`precision`、`timezone`；`timezone` MUST 为合法 IANA 时区名；`precision` 取值限于 `second`、`minute`、`day`。日期时间形式的 `start`/`end` MUST 带时区（naive 值拒绝）。`precision` 为 `day` 时 `start` 与可选 `end` MUST 为纯日期，不得携带虚构时分秒；纯日期形式的 `start`/`end` 仅允许 `precision` 为 `day`（second/minute 精度配纯日期拒绝）。`start` 与 `end` MUST 粒度一致（同为日期或同为日期时间）。`end` 存在时 MUST 晚于 `start`。

#### Scenario: 日期级时间不补全时刻

- **WHEN** `observedAt` 为 `{start: "2026-01-01", precision: "day", timezone: "Asia/Shanghai"}`
- **THEN** 校验通过且规范化输出保持日期形式，不生成 `T12:00:00` 之类占位时刻

#### Scenario: 日期级区间两端均为纯日期

- **WHEN** `observedAt` 为 `{start: "2026-11-12", end: "2026-11-13", precision: "day", timezone: "Asia/Shanghai"}`
- **THEN** 校验通过且两端均不补全时刻

#### Scenario: 粒度不一致被拒绝

- **WHEN** `start` 为纯日期而 `end` 携带时刻
- **THEN** 校验失败

#### Scenario: 区间时间顺序校验

- **WHEN** `observedAt.end` 早于或等于 `start`
- **THEN** 校验失败

#### Scenario: 纯日期配秒精度被拒绝

- **WHEN** `observedAt` 为 `{start: "2026-01-01", precision: "second", timezone: "Asia/Shanghai"}`
- **THEN** 校验失败，指明纯日期仅允许 day 精度

#### Scenario: 无时区时间被拒绝

- **WHEN** `observedAt.start` 为 `"2026-01-01T00:00:00"`（无偏移信息）或 `recordedAt` 为 naive 日期时间
- **THEN** 校验失败，指明必须带时区

#### Scenario: 非法时区被拒绝

- **WHEN** `timezone` 为 `Asia/Shanghai` 之外的非 IANA 字符串（如 `Mars/Olympus`）
- **THEN** 校验失败并指明时区非法

#### Scenario: recordedAt 独立于观察时间

- **WHEN** 同一身份字段的事件以不同 `recordedAt` 两次进入校验
- **THEN** 两次产出的 `eventId` 相同

### Requirement: 指标注册表与单位规则

`metric` MUST 属于指标注册表；数值测量 MUST 携带单位，且单位属于该指标的允许单位集合；`components` 中每个组件的 `metric` 与 `unit` 适用同样的注册表与允许单位规则。注册表外指标、不允许单位 MUST 被拒绝。经转换的单位 MUST 在 `quality.issues` 中标记 `unit_converted`。

#### Scenario: 注册表外指标被拒绝

- **WHEN** 事件使用未注册的 `metric`（如 `body.muscle_mass`）
- **THEN** 校验失败并指明指标未注册

#### Scenario: 不允许单位被拒绝

- **WHEN** `body.weight` 事件使用注册表允许集合之外的单位（如 `jin`）
- **THEN** 校验失败并列出该指标的允许单位

#### Scenario: 组件级指标与单位同样受注册表约束

- **WHEN** `components` 中某组件使用未注册指标或不允许单位
- **THEN** 校验失败并指明具体组件

#### Scenario: 单位转换留下质量标记

- **WHEN** 来源单位为 `lb` 的体重事件被转换为 `kg`
- **THEN** 规范事件单位为 `kg` 且 `quality.issues` 包含 `unit_converted`

### Requirement: 来源、派生与质量标记

`provenance.type` MUST 为 `observation`、`derived.vendor`、`derived.openhealthrouter`、`derived.user` 之一。`derived.vendor` 事件 MUST 同时携带 `estimated` 质量问题。`quality.status` 限于 `valid`、`suspect`、`invalid`；`quality.issues` 成员 MUST 属于首期代码集（`estimated`、`unit_converted`、`subject_unconfirmed`、`observed_time_from_ha_state`、`missing_context`），未注册代码 MUST 被拒绝。新增代码 MUST 以注册表加行方式扩展，属兼容变更，不升 schemaVersion 主版本。

#### Scenario: 厂商派生值必须带估算标记

- **WHEN** `provenance.type` 为 `derived.vendor` 且 `quality.issues` 不含 `estimated`
- **THEN** 校验失败

#### Scenario: 观察类型无需估算标记

- **WHEN** `provenance.type` 为 `observation` 且事件其余部分合法
- **THEN** 校验通过，`estimated` 非必需

#### Scenario: 未注册质量代码被拒绝

- **WHEN** `quality.issues` 包含首期代码集之外的字符串（如 `"looks_weird"`）
- **THEN** 校验失败并列出允许的代码集

### Requirement: 事件身份的两模式计算

事件身份 MUST 按两模式计算（与 `docs/health-event.md` §7 对齐）：有 `source.nativeId` 时身份为 `{schemaVersion, subjectRef, metric, source.integration, source.nativeId, deviceRef}`；无 `source.nativeId` 时身份为内容散列 `{schemaVersion, subjectRef, metric, source.entityId, 规范化 value/components, observedAt, provenance}`。`recordedAt`、`quality`、`correctionOf`、`derivedFrom`、`context`、`evidence` MUST 不参与 v1 身份（修正链外键语义由第 2 期定义）。身份字段经规范序列化（UTF-8、排序 key、无多余空白）后取 SHA-256，输出 `evt_` + hex。

#### Scenario: 相同身份输入产生相同 ID

- **WHEN** 同一事件以完全相同的规定身份字段再次进入校验
- **THEN** 两次生成的 `eventId` 完全一致

#### Scenario: 仅接收时间变化不改变 ID

- **WHEN** 仅 `recordedAt` 与 `quality`（含 issues 追加）与上一次提交不同
- **THEN** 生成的 `eventId` 与上一次相同

#### Scenario: 同一原生记录跨通道到达不重复计数

- **WHEN** 同一 `source.nativeId`、`source.integration` 且 `deviceRef` 一致（或同为空）的事件经不同通道到达，值或观察时间略有差异
- **THEN** 生成的 `eventId` 相同

#### Scenario: 组件顺序不影响 ID

- **WHEN** 同一多组件事件的 components 数组以不同顺序提交
- **THEN** 生成的 `eventId` 相同

#### Scenario: 时区表示不影响 ID

- **WHEN** 同一观察时刻以 `+08:00` 与 `Z` 两种偏移表示提交
- **THEN** 生成的 `eventId` 相同

### Requirement: 事件 ID 自算与输入校验

模型 MUST 自算 `eventId` 并赋值；输入若携带与计算值不符的 `eventId` MUST 被拒绝。

#### Scenario: 缺省 eventId 自动赋值

- **WHEN** 合法事件输入未携带 `eventId`
- **THEN** 校验通过且模型 `eventId` 等于按身份字段计算的值

#### Scenario: 输入 eventId 与计算值不符被拒绝

- **WHEN** 输入携带的 `eventId` 与按身份字段计算的值不一致
- **THEN** 校验失败

### Requirement: JSON Schema 契约发布与漂移检测

`contracts/health-event.v1.schema.json` MUST 由契约模型自动生成（camelCase 属性名，含 `$schema` 声明，固定序列化格式）；任何模型变更后重新生成的 Schema 与已提交文件不一致时，契约检查 MUST 失败并提示重新生成。accept 类 golden vectors MUST 同时通过生成 Schema 的独立校验。

#### Scenario: Schema 与模型一致时检查通过

- **WHEN** 模型未变更，重新生成的 Schema 与已提交文件逐字节一致
- **THEN** 契约检查通过

#### Scenario: 模型变更未同步 Schema 时检查失败

- **WHEN** 模型被修改而 `contracts/health-event.v1.schema.json` 未重新生成
- **THEN** 契约检查失败并指明 Schema 已过期

#### Scenario: accept 向量通过 Schema 独立校验

- **WHEN** 任意标记为 accept 的 golden vector 的 input 经独立 JSON Schema 校验器对照生成 Schema 校验
- **THEN** 校验通过
