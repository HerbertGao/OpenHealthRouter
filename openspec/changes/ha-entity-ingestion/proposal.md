# Proposal

## Why

第 1 期给了契约，第 2 期给了可靠的本地存储，但两者目前都没有入口：用户无法把家里已有的 HA 实体变成健康事件。第 3 期补上这条链路——用 Config Flow 选实体、把状态映射成 HealthEvent、写进本地库——这样"米家体脂秤 → 本地事件库"才第一次真正跑通，第 4 期的导出也才有真实数据。

## What Changes

- 新增单实例 Config Entry：首次通过 Config Flow 创建，不允许第二条 entry；不要求用户编辑 YAML。
- 新增 Options Flow 管理映射：添加、编辑、停用、删除映射，每步由 HA 原生表单完成。
- 新增映射模型：源实体、测量者引用、设备引用、指标、单位来源（固定或实体属性）、取值位置、原生 ID 位置、观察时间属性与回退开关、数据性质、附加质量标记；精度由时间值形态决定，映射不声明精度。写入前校验实体存在且非自身、指标已注册、固定单位属于允许集合、质量标记已注册。
- 新增状态监听：只订阅已配置实体；`state_changed` 触发确定性映射，构造 HealthEvent 后幂等写入本地库。
- 新增不可摄入状态处理：`unknown`/`unavailable`、非数值、非法单位、缺失测量时间一律跳过并记录原因码，不写库、不抛到事件循环。
- 新增时间语义：优先来源提供的观察时间；回退到 HA 状态时间时标注 `observed_time_from_ha_state`；来源只给日期时保持 `precision: day`。
- 新增递归防护：本集成自身实体永不被摄入。
- 新增接入状态投影：三个 Sensor 暴露已摄入计数、最近摄入时间、最近跳过原因码；状态、属性、entity_id 与 unique_id 都不含指标名、测量者或健康数值。
- 新增生命周期处理：setup 失败走 `ConfigEntryNotReady`；卸载时移除监听、关闭存储、无在途任务与句柄泄漏。
- 存储接线改为 HA executor：`AsyncEventStore` 支持注入执行器，HA 侧使用 `hass.async_add_executor_job`（第 2 期 design D8 已预留该切换，签名不变）。
- 明确不引入：设备发现、BLE/厂商协议、Recorder 历史回填、自定义前端、任何外部网络投递。

## Capabilities

### New Capabilities

- `ha-entity-ingestion`: 把已配置的 Home Assistant 实体状态转为健康事件并写入本地库的能力——配置生命周期、映射校验、状态摄入、不可摄入处理、时间语义、递归防护、接入状态投影与卸载清理。

### Modified Capabilities

（无——`health-event` 契约与 `health-event-store` 存储行为都不变；本期只在存储之外新增一层调用方。）

## Impact

- 新增代码：`config_flow.py`、`mapping.py`（映射模型 + 校验 + 纯函数状态映射）、`ingestion.py`（订阅、任务集合与接入状态聚合；刻意不命名为 coordinator，本期不使用轮询协调器）、`sensor.py`。
- 修改：`__init__.py`（setup/unload 与 runtime_data）、`manifest.json`（`config_flow: true`）、`strings.json`（表单与错误文案）、`async_store.py`（执行器注入，签名向后兼容）。
- 新增测试：`tests/integration/`（Config Flow、监听、跳过分类、递归防护、卸载清理、传感器状态）与 `tests/unit/`（纯函数映射）。
- 依赖：不新增第三方依赖。
- 破坏性：无。数据库 schema 不变（仍 `user_version = 1`）。
- 需人工审批：启用 `config_flow` 会改变集成在 HA 中的可见形态；首次接线会在 HA 配置目录写入 `openhealthrouter/events.db`。
- 阻塞后续：第 4 期导出与第 5 期第二连接器都依赖本期产生的真实事件。
