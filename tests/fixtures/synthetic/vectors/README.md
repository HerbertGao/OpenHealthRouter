# Golden Vectors 格式

每个向量一个 JSON 文件，命名 `<source>-<scenario>.json`。全部为合成数据（约定见上级 README）。

## 字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 场景名（与文件名一致） |
| `input` | 是 | HealthEvent 输入（camelCase，JSON 形态） |
| `expect` | 是 | `accept` 或 `reject` |
| `expected_id` | accept | 模型自算的 `eventId`，精确匹配（字节级锁定） |
| `expected_issues` | accept | 校验后 `quality.issues` 的精确内容 |
| `expected_error_contains` | reject | 断言 `ValidationError` 消息包含的子串 |

## 规则

- `expected_id` 是 characterization lock：它锁定当前实现的确定性行为，不是"正确性"证明；正确性由 spec scenario 与单元测试保证。身份字段集或规范化规则变更 = breaking change，需全量重生成并向量审查。
- accept 向量同时经 Pydantic（本目录测试）与生成 Schema（`tests/contract/test_vectors.py` 双跑）校验。
- 新增场景 = 新增一个文件；禁止在向量中放真实数据。
- Infinity 用 `1e999` 表达（严格 JSON 合法，`json.load` 解析为 inf）；NaN 无法以严格 JSON 表达，由 `tests/unit/test_models.py` 的 NaN 用例覆盖，不设向量。
