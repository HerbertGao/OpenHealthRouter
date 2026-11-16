# Home Assistant 实体映射

实体映射决定“一个 HA 状态如何成为一个健康事件”。这是首版最核心的配置，也是最容易产生错误健康语义的位置。

## 1. 映射原则

- 只处理用户明确选择的实体，不自动把名称含 `weight`、`sleep` 的实体全部接入。
- 一个映射只产生一种逻辑指标；血压等多组件测量使用专门的组件映射。
- 来源集成负责设备协议，OpenHealthRouter 不修改源实体或设备配置。
- 映射配置声明事实和转换规则，不让大模型猜测字段。
- 无法确定测量者、时间或数据性质时，保留不确定性并添加质量问题。

## 2. 逻辑配置

以下 JSON 只用于解释字段，不表示首版支持 YAML 或要求用户手写 JSON；实际配置通过 Config Flow / Options Flow 完成。

```json
{
  "mappingId": "scale_weight",
  "enabled": true,
  "sourceEntity": "sensor.scale_weight",
  "subjectRef": "subject_local_1",
  "deviceRef": "device_local_1",
  "metric": "body.weight",
  "valueFrom": "state",
  "unit": "kg",
  "nativeIdFrom": "attributes.measurement_id",
  "observedAtFrom": "attributes.measured_at",
  "fallbackTime": "state.last_changed",
  "timePrecision": "second",
  "provenanceType": "observation",
  "qualityIssues": []
}
```

| 配置 | 要求 |
| --- | --- |
| `sourceEntity` | 必须存在且不能属于 OpenHealthRouter 自身 |
| `subjectRef` | 指向本地测量者；多人共用设备无法确认时应触发归属流程 |
| `deviceRef` | 尽量使用 HA Device Registry 的稳定本地引用 |
| `metric` | 必须来自已注册指标契约 |
| `valueFrom` | 首版支持 `state` 或指定数值属性 |
| `unit` | 固定规范单位，或从受信任的实体单位字段读取后转换 |
| `nativeIdFrom` | 可选；只有真正的测量 ID 才能使用，不能用属性集合冒充 |
| `observedAtFrom` | 可选；优先使用来源提供的测量时间 |
| `fallbackTime` | 使用 HA 状态时间时必须添加对应质量问题 |
| `provenanceType` | 明确是原始观察还是厂商派生 |
| `qualityIssues` | 记录映射本身已知的限制，如 `estimated` |

## 3. 状态处理

仅处理映射选中的 `state_changed`：

1. 读取新状态及必要属性。
2. 忽略 `unknown`、`unavailable`、空值和非有限数值。
3. 验证并转换单位；不支持的单位应拒绝，不静默按数值原样保存。
4. 提取原生 ID、测量时间和测量者。
5. 构造 `HealthEvent`，添加映射版本及质量问题。
6. 幂等写入；重复事件只更新接入统计。
7. 持久化成功后更新数据源状态。

HA 重启后不默认回放 Recorder。需要历史导入时，应作为独立的“导入”操作展示时间来源、覆盖范围和重复风险。

## 4. 时间选择

优先级为：真实测量时间、日期级来源时间、HA 状态时间。

- `last_changed` 表示状态最后变化，不必然是测量发生时间。
- `last_updated` 可能因属性变化而更新，也不能自动等同于测量时间。
- 来源只有“某一天”时，映射必须生成 `precision: day`。
- 来源只有相对时间或本地化字符串时，应先由确定性解析器验证；失败则拒绝或降级并标记，不能猜测。

对只提供最新状态的设备，OpenHealthRouter 只能记录启用映射之后观察到的变化，无法保证捕获每一次相同值测量。

## 5. 米家体脂秤首条链路

优先使用现有 HA 集成，例如设备已被官方 Xiaomi Home / Xiaomi BLE 或用户已验证的第三方集成支持时，直接选择其实体。不要因为实体名称包含“小米”就假定属性结构一致，每类来源都需要独立映射和样例。

### 体重

- 指标：`body.weight`；
- 来源：`ha_entity`；
- 数据性质：`observation`；
- 单位：规范为 `kg`；
- 测量者：来自明确映射，自动归属不确定时添加 `subject_unconfirmed`。

### 体脂率与 BMI

- 指标分别为 `body.fat_percentage`、`body.bmi`；
- 数据性质为 `derived.vendor`，除非有明确证据表明是 OpenHealthRouter 自己计算；
- 添加 `estimated`；
- 保留厂商原始值和单位，不用“看起来更精确”的公式重算覆盖。

共享体脂秤无法可靠识别站立者时，宁可让事件等待用户确认，也不能把数据静默归到最近使用者。

## 6. bmc-e6-ha 后续适配

bmc-e6-ha 不需要重写采集逻辑，适配器读取其已经生成的实体或原始记录：

- `recordNo` 写入 `source.nativeId`，并结合设备和指标生成 `eventId`；
- BLE、云端和 mini-agent 重复到达时按同一原生记录去重，同时保留可用来源信息；
- `recordDate` 只有日期时保持日期精度；
- AHI、治疗时长等与云端评分分开建模；
- 云端评分使用 `derived.vendor`，不能覆盖原始设备观察；
- HA 中“最新一晚”的实体只是投影，不能代替完整历史事件。

具体指标和单位必须在适配实现前加入契约及 golden vectors，本文不提前虚构完整 BMC 字段表。

## 7. 已实现口径（第 3 期）

- **时间**：优先取映射声明的属性；属性带偏移时换算到 HA 配置时区，不带偏移时按配置时区解释，纯日期保持日期级精度。回退到 `last_changed` 的事件强制带 `observed_time_from_ha_state`。
- **单位**：映射声明的单位在保存时按注册表**字面量**校验；运行时实体自带单位与映射单位归一后必须相同，不同即跳过（`unit_not_allowed`）。非规范单位由契约层换算为规范单位并标记 `unit_converted`。
- **接入状态**：三个投影传感器只含计数、最近摄入时间与原因码；状态、属性、`entity_id`、`unique_id` 均不得包含指标名、测量者或健康数值。
- **原因码**：跳过原因码是与契约质量代码不相交的闭集（10 个），只出现在日志与接入状态中。

## 8. 映射验收

每个来源映射至少需要以下 golden cases：

1. 正常数值、单位和测量时间生成预期事件。
2. 来源只给日期时不生成虚构时刻。
3. 不支持单位、缺失数值和不可用状态被明确拒绝。
4. 同一原生记录重复到达不产生新事件。
5. 映射版本变化不会意外重写历史事件。
6. 厂商派生值带有派生和估算标记。
7. 多人设备无法确认测量者时不会静默错误归属。
8. OpenHealthRouter 自身状态实体不会被再次摄入。
