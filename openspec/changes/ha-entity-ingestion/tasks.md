# Tasks

## 1. 集成测试夹具与助手（先行，供后续任务共用）

- [x] 1.1 创建 `tests/integration/conftest.py`：临时数据库路径夹具、合成体脂秤实体夹具（有效体重、带偏移的 ISO 时间属性、naive 日期时间属性、纯日期属性、磅单位属性、`unknown`、`unavailable`、非数值、非法单位、缺失时间各一）、以及注册实体到 entity registry 的辅助（用于断言 `source.integration`）；并验证夹具数据全为合成值（符合 `tests/fixtures/synthetic/README.md`），且 `uv run pytest tests/integration -q` 能收集到用例
- [x] 1.2 创建共享断言助手：读库事件数与事件列表、读接入状态快照、对状态实体做全量序列化（state + 递归 attributes + entity_id + unique_id）；并验证助手在已知状态的库上返回预期值（自测一条）

## 2. 映射模型、纯函数与保存校验

- [x] 2.1 创建 `custom_components/openhealthrouter/mapping.py`：`EntityMapping`（Pydantic，字段闭集见 design D2，**不含**精度字段）、`StateView`、`Skipped`（含原因码）、`REASON_CODES` 闭集（10 个）；并验证模型字段校验用例通过（多余字段被拒绝），且断言 `REASON_CODES` 长度为 10 并与契约 `ISSUE_CODES` 无交集
- [x] 2.2 实现 `map_state_to_event(mapping, view) -> HealthEvent | Skipped`：值来源（状态/属性）、单位与 `unit_converted`、来源字段填充（kind/entity_id/integration/native_id）、`provenance` 组成固定（类型 + captured_by + converter 版本，禁含时间戳与随机值）、时间三种形态（带偏移换算到配置时区、naive 按配置时区解释、纯日期→日期级）、回退标记、质量标记合并；所有失败返回原因码而非抛异常；并验证表驱动覆盖 spec 全部摄入分支、naive 与带偏移样例给出同一瞬间、`time_unavailable`（属性缺失/为空）与 `time_not_parseable`（非空但非 ISO）分界、实体自带单位与映射单位冲突时跳过（归一函数单测覆盖 `LBS`→`lbs`、`lbs`→`lb` 别名、`kg/m²`↔`kg/m2` 上标折叠三例；端到端用例只用注册表字面量写法；另断言映射声明 `lbs` 在保存校验阶段被拒（保存校验走字面量成员判定，不走归一）），并验证允许集合也按同一归一比较：属性单位给 `kg/m2` 的 BMI 映射能成功入库且输出单位为 `kg/m²`，以及同一 `StateView` 连续调用两次得到相同 `eventId`
- [x] 2.3 实现 `validate_mapping(mapping, entity_check) -> list[str]`：实体存在、非本域、指标已注册、单位在允许集合、质量代码在 `ISSUE_CODES`、`derived.vendor` 必须含 `estimated`、时间来源至少一处（属性或回退，二者皆无则拒绝）；单位来源为属性时不校验具体单位值；并验证八类错误各自返回可识别错误键（用注入的 `entity_check`，不依赖 HA）

## 3. 执行器注入

- [x] 3.1 修改 `async_store.py`：`__init__(store, executor=None)` 与 `open(path, *, executor=None)`，统一 `_run()` 走注入 callable（默认 `asyncio.to_thread`），全部公开方法复用；并验证 `uv run pytest tests/storage -q` 原有用例不改动仍通过
- [x] 3.2 新增执行器路径测试：注入记录调用的 callable，断言 open（含迁移）、写入、查询、删除、备份都经过它；并验证注入与默认路径跑同一断言集结果一致

## 4. 集成元数据与配置流程

- [x] 4.1 `manifest.json` 增加 `config_flow: true` 并递增 `version`；`strings.json` 补齐 `config`、`options`、`error`、`abort` 文案；并验证 hassfest 通过
- [x] 4.2 实现 `config_flow.py` 的 `OhrConfigFlow`：`async_step_user` 内 `async_set_unique_id(DOMAIN)` + `_abort_if_unique_id_configured()`，创建单条条目；并验证集成测试：首次成功、重复 abort 且条目数为 1
- [x] 4.3 实现 Options Flow：列表/新增/编辑/停用/删除映射，保存前调 `validate_mapping` 并映射为表单错误键；保存后 `hass.config_entries.async_reload`；并验证集成测试：合法映射保存后选项可见且重载后订阅生效、七类非法输入各自返回对应错误键且 options 未变

## 5. 运行时装配与状态监听

- [x] 5.1 修改 `__init__.py`：`RuntimeData`（store、state 快照、tasks 集合、`unsubs` 列表、closed 与 unload_ok 标志）与 `type OhrConfigEntry = ConfigEntry[RuntimeData]`；setup 打开存储（`hass.config.path("openhealthrouter", "events.db")`）并注入 `hass.async_add_executor_job`；存储不可用抛 `ConfigEntryNotReady`；并验证集成测试：setup 后 `runtime_data` 可用且 pyright 通过、迁移失败场景进 `ConfigEntryNotReady`
- [x] 5.2 实现订阅：只订阅已启用映射的实体（空集合不订阅），回调创建登记到 `runtime.tasks` 的任务；并验证集成测试：已配置实体变化写出 1 条事件，未配置实体变化不写、摄入计数不变；每个订阅的取消函数都追加进 `runtime.unsubs`（断言长度等于订阅数）
- [x] 5.3 实现递归与回填防护：忽略本域实体、启动不读 Recorder；并验证集成测试：自身传感器变化不新增事件；启动前后事件数不变，且**随后一次真实变化恰好新增 1 条**（正向对照，排除"根本没订阅"）

## 6. 摄入行为：字段保真、去重与失败语义

- [x] 6.1 逐字段保真：断言事件的指标、数值、**该指标规范单位**、测量者、设备、`source.kind`、`source.entity_id`、`source.integration`、`native_id` 与映射/实体一致（映射声明磅时按换算结果断言并同时断言 `unit_converted`）；并验证两个不同源实体在相同数值与时间下产生两条事件（来源参与身份，不被误判重复）
- [x] 6.2 重复到达不新增：用时间来源为**不可变 ISO 属性**（`fallback_time=None`）的映射，配 `hass.states.async_set(..., force_update=True)` 强制第二次回调，断言库行数不变、新建计数不增长、重复计数增长，且对 `EventStore.insert_events` 的 spy 记录到两次调用；并验证对照组——回退到 `state.last_changed` **且不声明 native_id 属性**的映射在同样 `force_update` 下因观察时间变化而必须新增 1 行（断言两次 `eventId` 不同、第二次 spy 返回 `duplicates=0`，确保身份模式为内容散列而非原生 ID）
- [x] 6.3 原因码逐码覆盖：为闭集十码各构造唯一来源用例——`state_unknown`、`state_unavailable`、`value_missing`、`value_not_numeric`、`unit_missing`（属性单位缺失）、`unit_not_allowed`（属性单位不在允许集）、`time_unavailable`（声明属性但缺失且未开回退）、`time_not_parseable`、`event_invalid`（构造契约拒绝输入）、`store_error`（注入写入失败）；并验证每例断言原因码正确且库行数不变，至少一类同时断言摄入计数不变
- [x] 6.4 写入失败语义：先证明订阅生效（同类变化在未注入失败时能入库），再注入一次存储写入异常，断言摄入计数与最近摄入时间不变、原因码为 `store_error`、随后一次真实变化正常写入；并用 `loop.set_exception_handler` 装 spy 断言无未处理任务异常，同时加一条"故意让写闭包抛异常"的正向对照（spy 必须被触发，证明该断言有判别力）
- [x] 6.5 日志纪律：以 `caplog` 断言跳过与失败日志包含源实体 ID 与原因码，且不含体量数值、单位字符串与源属性内容；并验证该断言在把数值拼进日志的变异实现下会失败

## 7. 接入状态投影与隐私

- [x] 7.1 创建 `sensor.py`：三个传感器（已摄入计数、最近摄入时间、最近跳过原因），固定 `entity_id`/`unique_id`、名称不含指标或测量者、属性仅白名单键；并验证集成测试：启动态为 `0 / unknown / unknown`（跳过原因字段只允许闭集成员或为空）、摄入后计数与时间更新、跳过时计数不变且原因更新
- [x] 7.2 隐私断言：对三个传感器的 state、递归 attributes、entity_id、unique_id 做序列化，断言不含体量数值、单位字符串、指标名与源属性键；并验证该断言在"把最近值放进 attributes"或"把指标编进 entity_id"的变异实现下均失败
- [x] 7.3 无外发：在摄入期间 monkeypatch `socket.socket.connect` 使其抛错，断言摄入仍成功且事件入库；并验证该用例在引入任何出站调用时会失败

## 8. 卸载与重载

- [x] 8.1 实现 `async_unload_entry`（按 design D8 的唯一骨架）：**首行按 `unload_ok` 短路** → **先卸载平台，失败即原样返回 False（不动订阅与存储）**→ 置 `closed`、退订、`wait_for(shield(gather(*tasks, return_exceptions=True)), 10s)` 真排空（超时先 cancel 再有界 `wait`；被取消时有界 `wait` 后重抛）→ `finally` 内调用同步 `store.close()`（幂等、无锁无 I/O）→ 置 `unload_ok`；写入闭包在进入执行器前检查 `closed`；并验证集成测试（都按计数器断言，不靠日志文本）：先在 `EventStore.insert_events`（同步层）打 spy，卸载**前**同一实体变化使 spy 计数 ≥ 1（正向对照，证明订阅确实生效）；卸载后该实体再变化时计数不再增长，且未捕获到 `StoreUnavailable`、无未处理任务异常
- [x] 8.2 卸载瞬间有在途写入：用确定性钩子（monkeypatch `EventStore.insert_events` 先等一个 `threading.Event` 再执行原逻辑），在写入未返回时触发卸载，随后放行该 Event；断言钩子确实被进入过（计数 ≥ 1）、卸载完成、无未处理的 `StoreUnavailable`、写入数据可用、且关闭发生在线程返回之后；并验证重复卸载幂等：未发生取消时对 `EventStore.close` 计数断言恰为 1（不是仅断言"第二次不抛错"）；取消路径单独断言"`close` 至少执行一次、监听已退订、无未处理任务异常"（取消时放弃排空，故不断言恰为 1）；另加一条"平台卸载失败后重试"用例：首次让 `async_unload_platforms` 返回 False → 断言返回 False、`close` 调用次数为 0、订阅仍在且存储仍可用（条目未被破坏）；重试让它返回 True → 断言返回 True、`close` 恰为 1，且期间无未处理异常
- [x] 8.3 重载语义：断言 reload 前后库行数不变、reload 后一次变化恰新增 1 条、reload 期间不产生额外写入；并验证映射删除后该实体再变化不再入库（历史事件仍在）

## 9. 全量回归与发布准备

- [x] 9.0 把 `pyproject.toml` 的 `homeassistant` 下限提升到与支持矩阵一致（HA >= 2026.10），重新锁定并确认 `ConfigEntry[RuntimeData]` 泛型可在 pyright 下通过；并验证 `uv sync` 成功且 `uv run pyright` 全绿
- [x] 9.1 全量门禁：`ruff format --check`、`ruff check`、`pyright`、`check_contracts.py`、`pytest`、敏感数据扫描；并验证本地一次跑通
- [ ] 9.2 独立 review 无阻断项后推送，CI 的 `check` 与 `hassfest` 全绿；并验证结果对应该提交

## Workflow follow-up

- 归档前对照 `docs/roadmap.md` 第 3 期验收逐条确认；把"回退必打标记""接入状态与 entity_id 不含健康内容""单位转换由契约层完成"同步进 `docs/entity-mapping.md`；若 roadmap 的"单位转换"表述与契约层职责不一致，一并澄清。
- 本变更被审查接受后归档。
