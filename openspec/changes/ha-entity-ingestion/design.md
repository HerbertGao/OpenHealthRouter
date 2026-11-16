# Design

## Context

参见 proposal.md - Why。现状约束（均已核对本仓代码与已安装的 HA 源码）：`models.py` 要求事件字段完整、`quality.issues` 只允许 5 个已注册代码、`derived.vendor` 必须带 `estimated`、日期级事件的 `start` 必须是纯日期；无 `nativeId` 时事件身份是内容散列，**包含 `source.entityId` 与 `provenance`**；`store.py` 每操作一个连接、写入幂等并返回 `(created, duplicates)`；`async_store.py` 现用 `asyncio.to_thread`；manifest 尚未声明 `config_flow`；`tests/integration/` 为空。支持矩阵：HA >= 2026.10.x、Python >= 3.14.2。

## Goals / Non-Goals

**Goals:**

- 配置与映射全在 HA UI 完成；映射错误在保存时暴露。
- 映射逻辑是纯函数，可在无 HA 运行时下单测穷举分支。
- 摄入失败不冒泡到事件循环，不污染"接入成功"状态。
- 卸载后无订阅、无在途写、无已关库写入。

**Non-Goals:**

- 不做设备发现、Recorder 历史导入、多组件（血压）映射、自定义卡片、导出、任何外发（`docs/roadmap.md` 第 3 期"明确不做"）。
- 不引入 `DataUpdateCoordinator`：接入是事件驱动而非轮询（`docs/technology-stack.md` §4.1），模块名不得暗示轮询。
- 不改契约与存储 schema（`user_version` 保持 1）。

## Decisions

### D1: 单 Config Entry；用 `async_set_unique_id` 阻止第二条

`ConfigFlow.async_step_user` 调 `await self.async_set_unique_id(DOMAIN)` 再 `self._abort_if_unique_id_configured()`。仅设置 `unique_id` 并不阻止重复条目——阻止由 `_abort_if_unique_id_configured` 产生。条目只承载实例级不变量，映射全部放 `entry.options["mappings"]`。

- 备选：映射放 `entry.data` → 拒绝：`data` 创建后不可改，改映射需重建 entry。
- 备选：每条映射一个 entry → 拒绝：与会"一个数据库、一个写锁"的实例模型冲突。

### D2: 映射字段闭集（Pydantic 校验，存 JSON 兼容 dict）

`EntityMapping`：`mapping_id`、`source_entity`、`subject_ref`、`metric`、`unit`、`unit_attribute`（None 表示用固定 `unit`）、`value_attribute`（None 表示取实体状态）、`device_ref`、`native_id_attribute`、`observed_at_attribute`、`fallback_time`（`"state.last_changed"` 或 None）、`provenance_type`、`quality_issues`、`enabled`。

值取自实体状态且实体自带 `unit_of_measurement` 时，归一后必须与映射解析出的单位**完全相同**（归一函数见下，只做空白/大小写/上标/微符号折叠加一条显式别名 `lbs`→`lb`，不做语义推断），不同即跳过（`unit_not_allowed`）。这条刻意不做"同属允许集合即可"：磅与千克都属 `body.weight` 允许集合，若按集合判定就无法区分"映射声明千克、实体上报磅"这种错配，会造成按千克静默入库。以磅上报的实体应把映射单位声明为磅，由契约层换算并打 `unit_converted`。归一化的适用范围必须分清：**保存映射时的单位校验走字面量成员判定**（声明 `lbs` 直接拒绝，只接受注册表里的 `lb`/`kg` 写法）；归一化只用于两处运行时比较——实体单位与映射单位是否一致、实体单位是否属于允许集合。否则 `lbs` 会通过保存校验、随后在 `convert_unit` 里查不到转换对而返回 `None`，落进无原因码可用的空白。

运行时比较必须两侧同时归一：注册表 `allowed_units` 也按同一函数归一后再比较，否则 `body.bmi` 的 `kg/m²`归一成 `kg/m2` 后就再也命不中自己的允许集合；输出单位一律取注册表字面量——这是 `docs/entity-mapping.md` §3"不支持的单位应拒绝，不静默按数值原样保存"的直接落地。

单位保留两条来源（对应 `docs/entity-mapping.md` §2"固定规范单位，或从受信任的实体单位字段读取后转换"）：固定单位在保存时校验，属性单位在运行时校验，因此 `unit_missing` 与 `unit_not_allowed` 两个原因码都有可达出口。

刻意不含 `time_precision`：精度必须由实际时间值形态决定（纯日期 → `day`，日期时间 → `second`）。若允许映射声明精度，"声明 second + 来源给纯日期"会撞上契约的"纯日期配秒精度被拒绝"，只能靠运行时静默纠正，不如不暴露这个自由度。

- 备选：保留 `time_precision` → 拒绝：与契约冲突，产生两处真相。
- 备选：只用 voluptuous dict → 拒绝：映射要在纯函数入口再次校验并给出字段级错误。

### D3: 摄入核心是纯函数 `map_state_to_event(mapping, view) -> HealthEvent | Skipped`

`StateView`（与 HA 解耦）：`entity_id`、`state`、`attributes`、`last_changed`（tz-aware datetime）、`timezone_name`、`integration`。函数返回事件或带原因码的跳过结果，**永不抛异常**（内部把契约校验错误归为 `event_invalid`）。

两类码互不相交：`REASON_CODES`（10 个，只进日志与接入状态）与契约 `ISSUE_CODES`（5 个质量代码，只进事件 `quality.issues`）。原因码一旦被写进 `quality.issues`，契约会判未知代码并让所有事件退化为 `event_invalid`，因此映射层禁止交叉。HA 侧只负责取状态、组 `StateView`、调函数、写库或记原因码。

- 备选：在回调内直接构造事件 → 拒绝：分支无法穷举测试。

### D4: 监听用 `async_track_state_change_event`，任务自持集合

只订阅已启用映射的实体（空集合不订阅）。回调不 `await` 写库，而是创建任务并登记到 `runtime.tasks: set[asyncio.Task]`；任务结束自行 `discard`。任务内部 `try/except Exception` 兜底归类原因码。

- 备选：`hass.async_create_task` → 拒绝：不随条目卸载管理，卸载后仍在写。
- 备选：交给 `entry.async_create_background_task` 就认为卸载会等待 → **拒绝**：HA 先执行 `async_unload_entry`，之后才处理 `async_on_unload` 回调与后台任务，因此"在 unload 里关库"会早于任务收尾（见 D8）。

### D5: 执行器注入的类型是可调用对象，不是 Executor

`hass.async_add_executor_job` 是 `Callable[..., asyncio.Future]`（内部即 `loop.run_in_executor(None, ...)`），不是 `concurrent.futures.Executor` 实例。因此签名定为：

```python
AsyncEventStore(store, executor: Callable[..., Awaitable[Any]] | None = None)

async def _run(self, func, *args):
    if self._executor is None:
        return await asyncio.to_thread(func, *args)
    return await self._executor(func, *args)
```

`open` 同样接受 `executor` 关键字（默认 None），使迁移与首连接也走注入执行器——否则"迁移失败 → ConfigEntryNotReady"跑到默认线程池，与 §4.1 不一致。第 2 期测试不传该参数即保持原行为。

- 备选：`loop.run_in_executor(executor, ...)` + 传 Executor 实例 → 拒绝：与 HA API 不符，实现时必须再包一层。

### D6: 时间与时区规则固定

- 时区名：`hass.config.time_zone`；`last_changed` 是 tz-aware datetime，使用时 `astimezone(ZoneInfo(hass.config.time_zone))` 再取值。
- 属性值三种形态：带偏移 → `astimezone(配置时区)` 后写入；不带偏移（naive）→ 按**配置时区**解释后写入（禁止依赖进程时区，HA 容器通常为 UTC，否则会静默偏 8 小时）；纯日期 → 日期级。
- 日期级：按**配置时区**切日，`start` 保持纯日期，`precision="day"`；禁止从属性值的 tzinfo 反推时区名。
- 回退：仅当 `fallback_time == "state.last_changed"` 时使用状态时间，并强制追加 `observed_time_from_ha_state`；未开启回退且属性缺失 → `time_unavailable`。
- 属性存在但无法解析为 ISO → `time_not_parseable`（不猜测）。
- 两个时间码的边界：`time_unavailable` = 未声明属性，或属性缺失/为空且未开回退；`time_not_parseable` = 属性非空但不符合 ISO 形态。空串与 `None` 归前者。

- 备选：UTC 切日 → 拒绝：会让本地 00:00–08:00 的测量落到前一天。
- 备选：用 `last_updated` → 拒绝：属性变化会刷新它。

### D7: 接入状态是不可变快照 + 三个固定传感器

`IngestionState`（frozen dataclass）：`ingested_total`、`last_ingested_at`、`last_skip_reason`；初始值为 `0 / None / None`，对应传感器状态 `0 / unknown / unknown`。传感器用固定 `entity_id`/`unique_id`（`_ingested_total`、`_last_ingested`、`_last_skip_reason`），名称不含指标/测量者；属性只允许白名单键（如 `reason`）。

- 备选：把最近事件值做成传感器 → 拒绝：健康数值会进状态机与 Recorder（`docs/privacy.md`）。
- 备选：把指标名编进 entity_id 便于识别 → 拒绝：entity_id 会进 Recorder 与日志。

### D8: 卸载协议：重入短路 → 停止接收 → 有界排空 → 无锁关库

只保留以下一种写法（不再并列其他版本）：

```python
async def async_unload_entry(hass, entry) -> bool:
    runtime = entry.runtime_data
    if runtime.unload_ok:  # ① 已成功卸载：幂等短路
        return True

    # ② 先卸平台：失败则"什么都没动"，条目仍完好可用
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not ok:
        return False

    # ③ 停止接收 → 排空 → 关库
    runtime.closed = True
    for unsub in runtime.unsubs:
        unsub()
    try:
        tasks = list(runtime.tasks)
        if tasks:
            inner = asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.wait_for(asyncio.shield(inner), timeout=10)  # 真排空
            except TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.wait(tasks, timeout=5)  # 超时：有界收尾
                inner.cancel()
            except asyncio.CancelledError:
                await asyncio.wait(tasks, timeout=5)  # 卸载自身被取消：先有界等待
                inner.cancel()  # 再取消句柄，避免未 retrieve
                raise
    finally:
        runtime.store.store.close()  # 幂等关库：只置标志、无 I/O、不取锁
    runtime.unload_ok = True
    return True
```

`close()` 是幂等的（只置 `_closed = True`），所以重试与取消路径都安全地重复调用。

为什么关库不走 `AsyncEventStore.close()`：后者先抢 `asyncio` 写锁，超时路径上会**无界**等待那个已超时的写，等于推翻"有界卸载"。`EventStore.close()` 只置 `_closed = True`（无 I/O、无锁），同步调用即可，且必须放在 `finally` 里，取消路径同样执行。`AsyncEventStore.store` 是公开属性（`async_store.py` 已有该 property），骨架里的 `runtime.store.store.close()` 成立。

平台卸载放在最前面是刻意的：它是唯一可能失败且失败后需要"条目仍能用"的一步。先做它，失败就原样返回，此时订阅未退、库未关、条目状态未变，HA 重试或用户重试都能继续。若把它放到最后，一旦失败就会出现"条目显示已加载、实际已停止摄入且库已关闭"的僵尸状态。

`closed` 与 `unload_ok` 的分工：`closed` 表示"已停止接收并开始排空"，`unload_ok` 表示"平台卸载与收尾都已完成"。平台卸载失败时两者都仍为假——什么都没动，重试会重新尝试卸载平台。若已过平台卸载、但在排空或收尾阶段被取消，则 `closed` 为真、`unload_ok` 为假；此时重试会再次调用 `async_unload_platforms`（平台已卸载，正常返回成功）随后幂等重跑排空与 `close()`。短路只看 `unload_ok`——若只看 `closed`，会把"没卸完"报告成"已卸载"。HA 对同一 entry 的 unload 串行，"卸载进行中"窗口不会被第二次调用进入。

排空的范围：

1. **仅 ③a 是"真排空"**。超时或取消时只承诺"卸载不阻塞、关库不泄漏、无未处理异常"，不承诺线程已返回。
2. **良性范围只含已建连的写**。`close()` 只置标志、不关既有连接、连接每操作新建，所以已进入建连的写会正常完成；**已提交到执行器但尚未建连**的写会拿到 `StoreUnavailable`，写入闭包必须吞掉它并归为 `store_error`，不得逃逸成未处理任务异常（这与"持久化失败不得报告为成功"一致：原因码归 `store_error`，且不更新摄入计数与最近摄入时间）。

- 备选：只依赖 `entry.async_on_unload(store.close)` → 拒绝：回调顺序在 `async_unload_entry` 之后，库会先被关。
- 备选：直接 cancel 不等 → 拒绝：`cancel()` 只打断协程 `await`，执行器线程仍在写，关库与之竞争。

### D9: 存储不可用走 `ConfigEntryNotReady`

setup 时若存储进入不可用状态（迁移失败、权限不足、目录不可写），抛 `ConfigEntryNotReady` 让 HA 退避重试；`ConfigEntryError` 保留给不可恢复的配置错误（本期不产生）。

### D10: Options 保存后重载条目

Options Flow 写回 `entry.options["mappings"]` 后立即 `await self.hass.config_entries.async_reload(entry.entry_id)`，让订阅集合按新映射重建。否则新映射要等 HA 重启才生效。

- 备选：注册 update listener 做增量重订阅 → 拒绝：本期映射数量小，重载语义更简单且不会漏退订。

### D11: 类型化 runtime_data

```python
type OhrConfigEntry = ConfigEntry[RuntimeData]
```

`RuntimeData` 持有 `AsyncEventStore`、`IngestionState` 快照、`tasks: set[asyncio.Task]`、`unsubs: list[Callable[[], None]]`、`closed` 与 `unload_ok` 标志；不使用 `hass.data` 字典。订阅回调的取消函数必须存进 `unsubs` 并由卸载流程显式调用——不用 `entry.async_on_unload`，因为那个回调在 `async_unload_entry` 之后才执行，退订越晚、与关库的竞态窗口越大。要求 HA >= 2026.10（支持 `ConfigEntry[...]` 泛型），与 README 支持矩阵一致。

### D12: 测试双层

- `tests/unit/test_mapping.py`：表驱动覆盖纯函数全部分支，含"同一 `StateView` 连续调用两次产生相同 `eventId`"（锁定身份稳定性）、配置时区切日、UTC 22:00 属本地次日的边界。
- `tests/integration/`：Config Flow 成功/重复/非法输入、映射保存后重载生效、状态变化写库并逐字段保真、强制重复触发去重、跳过原因码、递归防护、启动不回填（含正向对照）、卸载后写入调用为 0、传感器不含健康内容、日志不含健康值。

## Risks / Trade-offs

- [启用 `config_flow` 会改变集成在 HA 中的可见形态，且 hassfest 要求文案齐全] → 本期补齐 `config`/`options`/`error`/`abort` 文案；CI 的 hassfest 作为门禁。
- [状态短时间多次跳变会堆任务] → 每个任务只做一次写入并登记集合；若实测堆积再按实体合并（本期不预先实现）。
- [质量标记与派生规则分散在契约与映射两处] → 保存时校验复用契约的 `ISSUE_CODES` 与 `derived.vendor → estimated` 规则，运行期契约再校验一次作为兜底（失败归 `event_invalid`）。
- [卸载竞态由"取消不等于排空"引起] → D8 的 closed 检查是唯一防线，需有专项用例覆盖。
- [executor 注入改动第 2 期签名] → 新参数默认 None；第 2 期测试不改即通过。

## Migration Plan

manifest 增加 `config_flow: true` 并递增 `version`；已有安装升级后需在 UI 完成一次配置（此前无条目，无数据迁移）。回滚：删除集成即可，数据库保留在 `config/openhealthrouter/`。

## Open Questions

无。
