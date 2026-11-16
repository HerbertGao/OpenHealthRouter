# 合成测试数据约定

本目录只允许合成数据。禁止真实个人健康数据、真实设备导出、真实姓名、真实联系方式。

## 命名

`<source>-<scenario>.json`，例如 `scale-weight-normal.json`、`scale-fat-derived.json`。

## 固定取值

| 用途 | 约定值 |
| --- | --- |
| 通用时间戳 | `2026-01-01T00:00:00+08:00`（瞬时）、`2026-01-01`（日期级） |
| 通用体重 | `72.4`，单位 `kg` |
| 通用体脂率 | `18.5`，单位 `%`，须带 `derived.vendor` + `estimated` |
| 测量者引用 | `subject_local_1` / `subject_local_2` |
| 设备引用 | `device_local_1` |
| 来源实体 | `sensor.synthetic_scale_weight` |

## 数据类别覆盖

合成样例演进时必须覆盖以下类别（第 1 期起逐步填充）：规范事件、原始实体属性、测量者/设备本地引用、备注与测量上下文、Webhook 密钥占位（假值如 `test-secret-do-not-use`）、审计元数据。密钥类一律使用明显假值，禁止任何形似真实令牌的字符串。
