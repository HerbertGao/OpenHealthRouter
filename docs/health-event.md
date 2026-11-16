# HealthEvent 模型

`HealthEvent` 是 OpenHealthRouter 的内部交换模型。它面向个人健康时间序列和证据链，不直接复制 FHIR，也不等同于 HA State。FHIR、Open mHealth 或其他格式应由出口适配器生成。

本文定义首版逻辑模型；实现时应以机器可校验的版本化 JSON Schema 和 golden vectors 为准。

## 1. 示例

```json
{
  "schemaVersion": "1.0",
  "eventId": "evt_01...",
  "subjectRef": "subject_local_1",
  "metric": "body.weight",
  "value": 72.4,
  "unit": "kg",
  "observedAt": {
    "start": "2026-11-13T07:30:00+08:00",
    "precision": "second",
    "timezone": "Asia/Shanghai"
  },
  "recordedAt": "2026-11-13T07:30:04+08:00",
  "source": {
    "kind": "ha_entity",
    "integration": "example_scale",
    "entityId": "sensor.scale_weight",
    "nativeId": "measurement-123"
  },
  "deviceRef": "device_local_1",
  "provenance": {
    "type": "observation",
    "capturedBy": "device",
    "converter": "entity-mapper@1"
  },
  "quality": {
    "status": "valid",
    "confirmation": "unconfirmed",
    "issues": []
  },
  "context": {},
  "evidence": {
    "rawAttributes": {}
  },
  "correctionOf": null,
  "derivedFrom": []
}
```

## 2. 字段约定

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `schemaVersion` | 是 | 契约版本；不兼容变更必须升级主版本 |
| `eventId` | 是 | 稳定、去重使用的全局事件 ID |
| `subjectRef` | 是 | 本地测量者引用；默认不使用姓名、手机号等直接标识 |
| `metric` | 是 | 受版本控制的指标名，如 `body.weight` |
| `value` / `components` | 二选一 | 单值测量或同一次测量的多个组成值 |
| `unit` | 数值测量必填 | 值对应的规范单位；多组件时放在各组件中 |
| `observedAt` | 是 | 测量时间及精度 |
| `recordedAt` | 是 | 本系统接收时间，带时区 |
| `source` | 是 | 来源类型、集成、实体和原生记录 ID |
| `deviceRef` | 否 | 本地设备引用；未知时保留为空，不猜测 |
| `provenance` | 是 | 观察或派生类型、采集者、转换器版本 |
| `quality` | 是 | 有效性、确认状态和结构化质量问题 |
| `context` | 否 | 餐前餐后、测量姿势等已明确提供的上下文 |
| `evidence` | 否 | 原始属性或证据引用；默认不随摘要导出 |
| `correctionOf` | 否 | 被本事件修正的旧事件 ID |
| `derivedFrom` | 否 | 派生计算所依据的事件 ID 列表 |

`value` 与 `components` 不得同时出现。首版实体映射只接受数值测量；文本状态、文件和复杂报告需要后续指标契约，不能强行转成数字。

## 3. 时间语义

`observedAt.start` 和可选 `end` 共同表达瞬间、日期或区间，`precision` 表达真实精度，`timezone` 记录来源时区。

**精确瞬间**

```json
{"start":"2026-11-13T07:30:00+08:00","precision":"second","timezone":"Asia/Shanghai"}
```

**只有日期**

```json
{"start":"2026-11-13","precision":"day","timezone":"Asia/Shanghai"}
```

**跨夜治疗区间**

```json
{
  "start":"2026-11-12T23:18:00+08:00",
  "end":"2026-11-13T07:06:00+08:00",
  "precision":"minute",
  "timezone":"Asia/Shanghai"
}
```

不得为了迎合时间戳字段，给日期级数据添加虚构时分秒。使用 HA 状态时间代替测量时间时，在 `quality.issues` 中加入 `observed_time_from_ha_state`。

## 4. 指标和组件

指标名描述测的是什么，不包含来源或设备名。首版建议只定义实际映射需要的指标：

- `body.weight`；
- `body.fat_percentage`；
- `body.bmi`；
- `blood_pressure`；
- 后续 BMC 适配需要的睡眠与呼吸指标。

单位采用明确、可机器处理的代码，并为每个映射维护允许单位和确定性转换。展示名称可以本地化，规范单位不能随 UI 语言变化。

同一次血压测量必须保持为一个事件：

```json
{
  "metric": "blood_pressure",
  "components": [
    {"metric":"blood_pressure.systolic","value":128,"unit":"mmHg"},
    {"metric":"blood_pressure.diastolic","value":82,"unit":"mmHg"}
  ]
}
```

不能仅靠相同时间把两个独立事件拼成血压。

## 5. 来源、派生和质量

`provenance.type` 首版至少区分：

- `observation`：设备测量、用户录入或外部系统提供的原始观察；
- `derived.vendor`：设备、厂商云或厂商 App 推算；
- `derived.openhealthrouter`：本项目根据其他事件计算；
- `derived.user`：用户确认或修正产生的数据。

`quality.status` 建议为 `valid`、`suspect` 或 `invalid`。`quality.confirmation` 区分 `not_required`、`unconfirmed` 和 `confirmed`。质量问题使用稳定代码，例如：

- `estimated`：估算值；
- `subject_unconfirmed`：测量者归属未确认；
- `observed_time_from_ha_state`：测量时间来自 HA 状态时间；
- `missing_context`：缺少指标契约要求的上下文；
- `unit_converted`：发生了单位转换。

体脂率可保存，但必须带 `derived.vendor` 和 `estimated`。质量问题不等于丢弃数据；导出策略可以决定是否包含它们。

## 6. 证据与修正

原始属性用于追溯映射过程，但可能含有身份、设备或厂商信息。它们属于敏感证据，不是默认导出字段。首版不保存 OCR 图片或 PDF；未来加入时应使用独立证据引用、访问控制和删除流程。

修正事件通过 `correctionOf` 指向旧事件。旧事件不能被原地改写，查询结果应能解释为何某条记录被排除。用户要求删除时则执行真正的数据删除，而不是只增加一个“已删除”标记。

## 7. 事件 ID 与校验

有原生 ID 时，`eventId` 以来源集成、设备、原生 ID 和指标为输入稳定生成；`recordNo`、平台样本 UUID 等仍保留在 `source.nativeId`。没有原生 ID 时，以来源实体、规范值、观察时间和相关属性生成内容散列。

写入前至少校验：

- 必填字段、字段类型和契约版本；
- 数值有限且单位属于该映射允许范围；
- `value` / `components` 互斥；
- 时间格式、精度和区间顺序；
- `correctionOf` / `derivedFrom` 引用存在且不构成环；
- 映射器无法确认的字段保持缺省或质量问题，不自动编造。

医学合理性与结构合法性不同。首版不用未经验证的阈值擅自判病或删除异常测量。
