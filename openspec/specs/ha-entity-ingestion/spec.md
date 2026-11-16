# ha-entity-ingestion Specification

## Purpose

把 Home Assistant 已有实体的状态确定性地转换为健康事件并写入本地库，覆盖配置生命周期、映射校验、不可摄入处理、时间语义、递归防护、接入状态与卸载清理。

## Requirements

### Requirement: 单实例配置且无需 YAML

集成 MUST 通过 Config Flow 创建，并 MUST 用 `async_set_unique_id` 加 `_abort_if_unique_id_configured` 阻止第二条条目（仅设置 `unique_id` 不构成阻止）。映射 MUST 全部通过 Options Flow 表单管理，用户 MUST NOT 需要编辑 YAML。

#### Scenario: 首次配置成功

- **WHEN** 用户通过 Config Flow 完成首次配置
- **THEN** 创建恰好一条配置条目，集成加载完成

#### Scenario: 重复配置被拒绝

- **WHEN** 已存在一条配置条目时再次发起配置
- **THEN** 流程以 already_configured 中止，条目数仍为 1

#### Scenario: 表单报错不落库

- **WHEN** 用户在表单中提交无效输入
- **THEN** 表单返回对应错误键并停留在输入步骤，已保存的映射不变

### Requirement: 映射定义

每条映射 MUST 声明唯一映射 ID、源实体、测量者引用、指标与数据性质；MUST 声明值来源（实体状态或指定属性）与单位来源（固定单位或实体属性）；MAY 声明观察时间属性、时间回退开关、设备引用、原生 ID 属性与附加质量标记。映射 MUST NOT 单独声明精度——精度 MUST 由实际时间值形态决定。

#### Scenario: 值来自实体状态

- **WHEN** 映射的值来源为实体状态，实体状态为数值
- **THEN** 事件数值等于该状态值（若发生单位换算则为换算结果）

#### Scenario: 值来自指定属性

- **WHEN** 映射声明了值属性，实体状态非数值但该属性为数值
- **THEN** 事件数值等于该属性值

#### Scenario: 值来源缺失

- **WHEN** 映射声明的值属性在状态中不存在且实体状态不可解析为数值
- **THEN** 该次变化被跳过，原因码指明数值缺失

#### Scenario: 单位来自实体属性

- **WHEN** 映射声明单位来源为实体属性，且该属性给出允许集合内的单位
- **THEN** 事件单位按该属性解析（非规范单位按契约层规范化为标准单位）

### Requirement: 映射写入前校验

保存映射时 MUST 拒绝：不存在的源实体、属于本集成自身的源实体、未注册的指标、固定单位不属于该指标允许集合、不在已注册质量代码集内的标记、声明为厂商派生却缺少 `estimated` 的映射，以及**既未声明观察时间属性、又未开启时间回退**的组合（时间来源至少一处）。单位来源为实体属性时，具体单位值在运行时判定，保存时不校验。

#### Scenario: 合法映射被保存

- **WHEN** 用户为已存在的体重实体提交合法映射
- **THEN** 映射被保存并出现在选项列表中

#### Scenario: 不存在或自身实体被拒绝

- **WHEN** 提交的源实体不存在或属于本集成
- **THEN** 保存被拒绝，错误键分别指明实体不存在或不能使用自身实体

#### Scenario: 未注册指标或不允许单位被拒绝

- **WHEN** 提交未注册指标，或单位不在该指标允许集合内
- **THEN** 保存被拒绝，错误键分别指明指标未注册或单位不被允许

#### Scenario: 未注册质量标记被拒绝

- **WHEN** 映射声明的质量标记不在已注册代码集内
- **THEN** 保存被拒绝，错误键指明标记未注册

#### Scenario: 厂商派生缺估算标记被拒绝

- **WHEN** 映射声明数据性质为厂商派生但未声明 `estimated`
- **THEN** 保存被拒绝，错误键指明缺少估算标记

### Requirement: 状态摄入与字段保真

集成 MUST 只订阅已启用映射的源实体，并在其状态变化时构造恰好一个规范健康事件写入本地库。事件 MUST 填充来源信息：`kind` 为 `ha_entity`、`entity_id` 为映射源实体、`integration` 为该实体所属平台（缺失时退化为实体域），有原生 ID 属性时填充 `native_id`。指标、数值、单位、测量者与设备 MUST 与映射一致，唯一例外是单位规范化要求的换算（此时单位取规范单位、数值为换算结果）。

#### Scenario: 一次状态变化产生一条保真事件

- **WHEN** 已配置实体产生一次有效状态变化
- **THEN** 本地库恰好新增一条事件，其指标、数值、单位、测量者、设备与来源实体均与映射一致

#### Scenario: 两个实体不互相顶替

- **WHEN** 两个不同源实体在相同测量者、指标、数值与时间下各产生一次变化
- **THEN** 本地库有两条不同事件，后到者不被当作重复丢弃

#### Scenario: 未配置实体不被摄入

- **WHEN** 未被任何映射引用的实体发生状态变化
- **THEN** 不产生事件，摄入计数不变

### Requirement: 幂等与重复到达

同一映射在相同身份输入下重复到达 MUST NOT 新增行。接入计数 MUST 以存储返回的"新建"数量为准，重复 MUST 只增加重复计数。

#### Scenario: 强制重复触发不新增事件

- **WHEN** 以强制更新方式让同一状态与属性再次触发一次状态变化（映射时间来源为不可变属性，观察时间与身份字段不变）
- **THEN** 两次回调都被处理，本地库行数不变，新建计数不增长，重复计数增长

#### Scenario: 身份不同的两次变化各自新增

- **WHEN** 同一实体先后产生两个不同的有效测量（观察时间不同）
- **THEN** 本地库新增两条事件，新建计数增长两次

### Requirement: 不可摄入状态不写入

集成 MUST 跳过不可作为测量的变化，MUST NOT 写入本地库，也 MUST NOT 让异常逃逸到事件循环。每次跳过 MUST 记录一个原因码，原因码 MUST 属于闭集且 MUST 与契约质量代码互不相交（原因码只出现在日志与接入状态中）。

#### Scenario: 跳过不写库且不抛异常

- **WHEN** 已配置实体发生任何不可摄入的变化
- **THEN** 本地库行数不变，回调不向外抛异常，接入状态记录一个闭集内的原因码

### Requirement: 原因码闭集与判定边界

原因码闭集固定为十个：`state_unknown`、`state_unavailable`、`value_missing`、`value_not_numeric`、`unit_missing`、`unit_not_allowed`、`time_unavailable`、`time_not_parseable`、`event_invalid`、`store_error`。边界 MUST 按下列判定：`time_unavailable` 为未声明时间属性、或属性缺失/为空且未开回退；`time_not_parseable` 为属性非空但非 ISO；`unit_missing` 为属性单位缺失/为空；`unit_not_allowed` 为单位不在允许集合内，或与映射单位归一后不同；`value_missing` 为映射声明的值属性缺失或为空；`value_not_numeric` 为取值来源存在但不是有限数值；`store_error` 为写入本地库失败。

#### Scenario: 未知与不可用状态分别记录

- **WHEN** 已配置实体变为 `unknown`，随后又变为 `unavailable`
- **THEN** 两次都不写库，最近跳过原因码依次为 `state_unknown` 与 `state_unavailable`

#### Scenario: 非数值状态被跳过

- **WHEN** 状态无法解析为有限数值
- **THEN** 不写库，最近跳过原因码为 `value_not_numeric`

#### Scenario: 不允许单位被跳过

- **WHEN** 状态携带该指标不允许的单位
- **THEN** 不写库，最近跳过原因码为 `unit_not_allowed`

#### Scenario: 单位属性缺失被跳过

- **WHEN** 映射声明单位来源为实体属性，但该属性缺失或为空
- **THEN** 不写库，最近跳过原因码为 `unit_missing`

#### Scenario: 实体自带单位与映射单位冲突被跳过

- **WHEN** 值取自实体状态、实体自带单位属性为磅，而映射解析出的单位为千克
- **THEN** 不写库，最近跳过原因码为 `unit_not_allowed`

#### Scenario: 契约拒绝时记录 event_invalid

- **WHEN** 构造出的事件未通过契约校验
- **THEN** 不写库，最近跳过原因码为 `event_invalid`

### Requirement: 跳过原因码的优先级

多个跳过条件同时成立时，记录的原因码 MUST 按固定优先级取第一个命中项：`state_unavailable`、`state_unknown`、`value_missing`、`value_not_numeric`、`unit_missing`、`unit_not_allowed`、`time_unavailable`、`time_not_parseable`、`event_invalid`；`store_error` MUST 只由写入路径产生，不参与该排序。

#### Scenario: 状态不可用优先于其他条件

- **WHEN** 实体状态为 `unavailable`，同时其单位属性缺失
- **THEN** 最近跳过原因码为 `state_unavailable`，而不是 `unit_missing`

#### Scenario: 数值缺失优先于单位问题

- **WHEN** 映射声明的值属性缺失，同时实体单位与映射单位冲突
- **THEN** 最近跳过原因码为 `value_missing`

### Requirement: 观察时间与时区语义

观察时间 MUST 优先取映射声明的属性；仅当映射开启回退时，才可使用 HA 状态时间，且该事件 MUST 追加 `observed_time_from_ha_state` 质量问题。时区 MUST 取 HA 配置时区：带偏移的属性值 MUST 换算到配置时区后写入；不带时区的日期时间 MUST 按配置时区解释（不得按进程时区）。来源属性为纯日期时 MUST 产出日期级精度且不补全时刻；属性存在但无法解析时 MUST 以 `time_not_parseable` 跳过。

#### Scenario: 使用来源属性时间

- **WHEN** 状态携带与映射匹配且可解析的时间属性
- **THEN** 事件观察时间与该属性表示同一瞬间，且质量问题不含状态时间回退标记

#### Scenario: 开启回退时打标记

- **WHEN** 映射开启回退且状态未提供可用时间属性
- **THEN** 事件观察时间取状态时间并换算到配置时区，质量问题包含 `observed_time_from_ha_state`

#### Scenario: 未开启回退时跳过

- **WHEN** 映射未开启回退且状态未提供可用时间属性
- **THEN** 不写库，最近跳过原因码为 `time_unavailable`

#### Scenario: 日期级属性保持日期精度

- **WHEN** 时间属性为纯日期
- **THEN** 事件为日期级精度，日期按配置时区解读且不生成虚构时刻

#### Scenario: 无法解析的时间属性被跳过

- **WHEN** 时间属性存在但不是可解析的 ISO 日期或日期时间
- **THEN** 不写库，最近跳过原因码为 `time_not_parseable`

### Requirement: 单位规范化

允许集合内的非规范单位 MUST 由契约层规范化为该指标的标准单位，并在事件质量标记中包含 `unit_converted`；固定单位来源在保存时 MUST 已属允许集合，属性单位来源在运行时 MUST 属允许集合，否则跳过。映射声明的单位 MUST 按注册表字面量校验（不接受仅归一后相同的写法）；归一化只用于运行时比较。值取自实体状态且实体自带单位属性时，两单位 MUST 经归一后完全相同（比较两侧都归一，即注册表允许集合也按同一规则归一后比较，输出一律取注册表中的规范单位字面量）；归一规则固定为：去首尾空白、大小写折叠、上标数字折叠为上标对应数字（`²`→`2`、`³`→`3`）、微符号折叠为 `u`（`µ`/`μ`→`u`），以及单条显式别名 `lbs`→`lb`；除此之外不做别名映射（其余同义写法由用户在映射里对齐）；不同即跳过并记 `unit_not_allowed`，MUST NOT 按映射单位静默入库。想摄入以磅上报的实体，唯一正路是映射单位声明磅，由契约层换算并标记 `unit_converted`。

#### Scenario: 磅值被规范化为千克

- **WHEN** 体重映射声明单位为磅且状态给出磅值
- **THEN** 事件单位为千克、数值为换算结果，且质量标记包含 `unit_converted`

### Requirement: 递归与历史回填防护

集成 MUST NOT 摄入本集成自身实体。集成启动时 MUST 只订阅未来变化，MUST NOT 读取 HA Recorder 历史补写事件。

#### Scenario: 自身实体不产生事件

- **WHEN** 本集成产生的传感器状态发生变化
- **THEN** 不产生新事件

#### Scenario: 启动不回填且随后仍能摄入

- **WHEN** 集成在已有历史记录的 HA 上启动、实体状态未变化
- **THEN** 事件数不因启动而增加；随后同一实体的一次真实变化仍恰好新增一条事件

### Requirement: 接入状态投影

集成 MUST 提供状态实体暴露已摄入计数、最近摄入时间与最近跳过原因码。初始值固定：计数为 0；最近摄入时间与最近跳过原因码为空，对应状态取 `unknown`；跳过原因字段只允许闭集成员或为空。状态值、属性、`entity_id`、`unique_id` 与友好名称 MUST NOT 包含指标名、测量者、健康数值或原始属性键；属性 MUST 只包含固定白名单键。

#### Scenario: 摄入后状态更新

- **WHEN** 一次有效变化成功写入本地库
- **THEN** 已摄入计数加一，最近摄入时间更新为该事件时间

#### Scenario: 跳过不影响摄入计数

- **WHEN** 变化被跳过
- **THEN** 已摄入计数不变，最近跳过原因码反映该次跳过

#### Scenario: 投影不含健康内容

- **WHEN** 序列化状态实体的状态、属性、entity_id 与 unique_id
- **THEN** 结果中不出现体重量值、单位字符串、指标名或源实体属性键

### Requirement: 持久化失败不得报告为成功

写入本地库失败时 MUST NOT 更新已摄入计数或最近摄入时间；MUST 记录 `store_error` 原因并保持集成可用。

#### Scenario: 写入失败不更新接入成功状态

- **WHEN** 本地库写入抛出错误
- **THEN** 已摄入计数与最近摄入时间保持不变，最近跳过原因码为 `store_error`

#### Scenario: 失败后仍可继续摄入

- **WHEN** 一次写入失败之后出现新的有效变化
- **THEN** 该事件被正常写入并更新接入状态

### Requirement: 卸载清理

卸载时 MUST 先停止订阅并标记运行时为已关闭，再等待在途任务（有界）、最后关闭存储；运行时已关闭时重复卸载 MUST 直接返回成功，MUST NOT 再次关闭存储。写入路径 MUST 在已关闭时直接返回而不触碰存储；已提交但尚未建连的写入若因关闭失败，MUST 归类为 `store_error` 且 MUST NOT 逃逸。

#### Scenario: 卸载后不再摄入

- **WHEN** 配置条目被卸载后已配置实体再次变化
- **THEN** 存储写入调用次数为 0，且不出现写入已关闭存储的错误

#### Scenario: 卸载不产生未处理的存储错误

- **WHEN** 卸载瞬间存在至少一个已建连（已在执行器线程内运行）的写入任务
- **THEN** 该写入正常完成且数据保持可用；不出现未处理的 `StoreUnavailable`；尚未建连的写入按 `store_error` 记录

#### Scenario: 重复卸载幂等

- **WHEN** 同一配置条目被卸载两次
- **THEN** 第二次不抛错，监听与存储句柄均未重复关闭

### Requirement: 默认无外部网络投递

摄入链路 MUST 只写入本地存储；默认配置下 MUST NOT 发起外部网络请求。

#### Scenario: 摄入不触发外发

- **WHEN** 一次有效变化被摄入
- **THEN** 除本地数据库写入外没有对外网络连接尝试

### Requirement: 摄入层日志纪律

跳过与失败路径的日志 MUST 只包含源实体 ID、映射 ID、事件 ID 与原因码；MUST NOT 包含状态值、单位数值或原始属性内容。

#### Scenario: 日志不含健康值

- **WHEN** 触发一次跳过与一次写入失败
- **THEN** 日志中出现源实体 ID 与原因码，且不出现体量数值或原始属性内容
