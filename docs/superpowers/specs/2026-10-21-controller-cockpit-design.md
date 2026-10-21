# Workflow Controller 驾驶舱设计（批准版）

日期：2026-10-21  
状态：设计已批准；实现见 §16，合并、部署与生产验收另行授权
路由：`MATCH + Research + Medium + UI/state_machine + unified + controller-free`  
验证：浏览器信息架构与浅色视觉评审  

## 1. 背景与问题

HAFlow Controller 是 Workflow 的控制平面和状态机执行器。它监听 Agent/Pane 事件，读取 Workflow、Task、Run、Candidate 与恢复责任等权威事实，计算 DAG 就绪节点，并执行派发、返工、门禁、恢复和收尾。

当前 Console 已经具备一批 Controller 操作能力，包括任务启动、工作流推进、返工、催办、指导、中断、替换、恢复裁决、Git 交付步骤、门禁放行和关闭工作流。然而这些能力分散在 Workflow Canvas、Task Drawer、Pane/Runtime、恢复待办及交付链中。用户能够点击零散按钮，却无法直接看见 Controller 当前在判断什么、为什么没有推进、下一步准备做什么，以及动作是否真正落实。

本设计建立 Workflow 级的 **Controller 驾驶舱**。它不是浏览器中的第二个 Controller，也不是运维数据大屏，而是当前 Workflow 的唯一完整控制入口和解释界面。

## 2. 已确认的产品原则

1. 默认采用全自动驾驶。Controller 在事实充分、模板允许且不需要领域判断时自动执行，不等待用户点击按钮。
2. Workflow 模板可以把指定节点或动作配置为半自动。人工确认只用于用户能够作出有效判断的领域，不用机械确认代替系统安全校验。
3. “推进工作流”与其他动作遵循同一策略，不设置独立的人工确认特权。`automatic` 表示 Controller 自动执行，而不是按钮免除确认。
4. 用户确认之后，Controller 仍须重新校验 Workflow、Task、Run、Pane、版本、候选与恢复责任。
5. Controller 驾驶舱集中现有全部 Controller 功能。其他页面只保留上下文快捷入口，并跳转到驾驶舱定位对应对象。
6. 前端不建立第二套状态、决策或动作可用性规则。按钮来自后端结构化 Action 投影。
7. 页面统一使用 Console 现有浅色设计令牌，不引入独立设计系统。

## 3. 目标与非目标

### 3.1 目标

- 让用户不打开终端也能看见 Controller 是否运行、正在处理什么、为何停住以及下一步是什么。
- 将 Workflow、Node、Task、Run、Pane、Recovery 和 Delivery 控制能力集中到一个 Workflow 级页面。
- 为每个动作展示触发依据、影响范围、风险、执行状态和真实回执。
- 保留 Canvas、Task Drawer、Pane 列表和恢复提醒的上下文便利，但消除独立控制逻辑。
- 支持模板定义默认全自动或指定节点/动作半自动。

### 3.2 非目标

- 不在前端重新实现 DAG、门禁、路由、恢复或任务状态机。
- 不把 Controller 改造成 Agent，也不允许模型直接写入确定性状态。
- 第一版不建设跨 Workflow 的全局运维大屏。
- 第一版不支持运行中的 Workflow 临时切换自动/半自动。
- 第一版不建设用户能力画像、组织级审批流、长期趋势或预测性调度。

## 4. 信息架构

Controller 驾驶舱属于当前 Workflow，与现有流程工作台共享项目和 Workflow 上下文。

### 4.1 顶部状态

显示：

- Workflow 名称和 ID；
- Controller 服务状态、运行版本及最近一次检查时间；
- 当前候选或执行代次；
- 模板控制策略：全自动或半自动；
- 状态未确认时的明确降级提示。

### 4.2 Controller 当前判断

这是页面的第一信息层，固定回答三个问题：

1. **正在发生什么**：当前节点、活跃任务、等待容量或恢复义务；
2. **为什么没有推进**：正常等待、门禁阻塞、身份未知、恢复责任或系统异常；
3. **下一步是什么**：Controller 准备自动执行的动作、触发条件和预计等待。

这些内容必须来自结构化确定性投影。模型生成的说明只能作为辅助解释，不得成为状态或按钮可用性的依据。

### 4.3 节点、任务与运行现场

按 Workflow 节点组织当前代次的任务，并关联：

- Task ID、状态和 version；
- Run ID、Agent 与执行身份；
- Pane ID 与实时存活状态；
- 依赖、门禁和候选身份；
- 当前可用 Controller Actions。

历史 Task 可以查看，但必须明确标注已替换或属于旧代次，不能提供对当前 Workflow 生效的动作。

### 4.4 Controller 待办

集中显示所有尚未结案的控制责任：

- 等待容量或等待 Agent；
- 等待派发、启动登记或回执；
- 恢复责任；
- 门禁阻塞或返工；
- Git 交付链下一步；
- 需要人工领域判断的半自动动作。

没有读到权威数据时显示“状态未确认”，不能显示“暂无待办”。

### 4.5 Controller 时间线

时间线展示结构化事实和动作回执，包括：

- 节点就绪与派发责任建立；
- Action 请求被接受或拒绝；
- Task intent、登记和 Pane 身份确认；
- 门禁、返工、恢复与交付动作；
- 等待、重试、部分成功和最终结案。

时间线复用现有事件与 operation 事实，不另建第二套历史账本。

## 5. 入口集中规则

Controller 驾驶舱是唯一完整控制中心。其他页面遵循以下规则：

| 现有入口 | 调整后行为 |
| --- | --- |
| Workflow Canvas 阻塞节点 | 打开驾驶舱并定位 Node/Blocker |
| Task Drawer 控制按钮 | 打开驾驶舱并定位 Task/Run |
| Pane/Runtime 操作 | 打开驾驶舱并定位运行现场 |
| 恢复提醒 | 打开驾驶舱并定位 Recovery Operation |
| Git/交付下一步 | 打开驾驶舱并定位 Delivery Action |

可以保留轻量快捷按钮，但快捷按钮必须消费同一个 Action 对象、使用同一个 Action ID、调用同一个后端入口，并展示同一种回执。禁止继续维护独立的动作拼装、可用性判断或确认逻辑。

## 6. 数据架构

### 6.1 单一 Cockpit Projection

新增或扩展一个 Workflow-scoped Controller 投影，聚合现有权威读取结果：

- Workflow 和当前 execution/candidate；
- 当前与历史 Task；
- Task version、Run 和 Pane identity；
- Workflow graph、依赖和 blockers；
- node-dispatch/recovery operations；
- Controller service/runtime fingerprint；
- Controller Actions；
- 最近结构化事件和动作 receipts。

投影只做有界读取、关联和表达，不写状态，不缓存新的业务事实。读取失败必须保留 unknown，不得把缺失数据转换为正常状态。

### 6.2 动作模型

继续复用后端 `ControllerAction` 作为动作权威，并逐步补足驾驶舱所需字段：

| 字段 | 含义 |
| --- | --- |
| `action_id` | 同一事实快照下稳定、可去重的动作身份 |
| `type/category/group` | Workflow、Node/Task、Run/Pane 或 Recovery/Delivery 分类 |
| `title/description/effect` | 用户能理解的动作、适用场景和实际影响 |
| `api_endpoint/api_payload` | 后端生成的执行入口与参数 |
| `target` | Workflow/Node/Task/Run/Pane/Operation 精确归属 |
| `expected_identity` | version、pane、run、candidate、operation 等并发校验信息 |
| `automation` | automatic、confirmation_required 或 blocked |
| `risk/destructive` | 风险与破坏性标记 |
| `reason/evidence_refs` | 为什么现在允许、建议或禁止该动作 |

前端不得根据 Task status 自行重建上述规则。

## 7. 第一版动作范围

### 7.1 Workflow

- 立即重新检查；
- 手动推进；
- 暂停与恢复；
- 关闭工作流。

### 7.2 Node 与 Task

- 启动节点或补派任务；
- 原工位返工；
- 有理由的替换/作废；
- 门禁处理与显式强制放行。

### 7.3 Run 与 Pane

- 查看运行现场；
- 留言指导 `steer`；
- 立即催办 `redrive`；
- 紧急停止 `halt`。

### 7.4 Recovery 与 Delivery

- 恢复责任查看和裁决；
- Git pipeline 下一步；
- 清除 escalation；
- 当前已有的重新派发、部分派发和关闭处理。

实施前必须建立现有功能盘点表。每项功能只能标记为：已集中复用、旧入口改为定位、保留同源快捷入口、只读展示或明确技术债。未列入盘点表的功能视为遗漏，不能宣称集中完成。

## 8. 自动与半自动策略

控制策略由 Workflow 模板声明，默认值为全自动。建议的概念结构如下；最终 Schema 在实施规格中与现有模板规范对齐：

```yaml
control_policy:
  mode: automatic
  confirmations:
    production_release: true

nodes:
  acceptance:
    control_mode: supervised
    confirm_before:
      - advance
```

动作决策顺序：

1. Controller 根据当前权威事实计算动作是否有效；
2. 事实不足、身份未知或恢复责任未结案时，动作 blocked；
3. 模板指定人工确认且用户能作出领域判断时，动作 confirmation_required；
4. 其他确定性、非破坏性的合法动作 automatic，并由 Controller 自动执行；
5. 自动执行或人工确认之后，执行边界再次读取并校验全部相关身份。

人工确认不能绕过安全规则，不能把 unknown 变成 pass，也不能替代证据。破坏性放行、证据绕过、身份 unknown 或恢复责任冲突不得因为默认 automatic 而自动执行；它们保持 blocked，或只向具备领域判断能力的用户提供人工入口。

## 9. 执行与回执语义

按钮不是 CLI 快捷键，而是 Controller 当前允许执行的结构化动作。

### 9.1 执行前

后端重新读取并至少校验：

- Workflow 归属和当前状态；
- Node 与 Task 归属；
- Task version；
- Run/Pane 当前实例；
- execution/candidate；
- 未结恢复责任；
- 动作的风险确认字段。

陈旧页面、跨 Workflow Task、已替换 Task、实例变化和恢复责任冲突必须 fail closed。

### 9.2 执行结果

前端必须区分四类结果：

1. **已落实**：状态已改变，并能读取 Task、Pane、Operation 或 Event 回执；
2. **已接受，仍等待**：责任已持久化，但派发、登记或回执尚未完成；
3. **部分成功**：前一步已经生效、后一步失败，明确禁止重复前一步；
4. **已拒绝**：没有产生副作用，并说明冲突事实及刷新、等待或恢复路径。

HTTP 200、命令退出码 0、成功提示或已发送消息都不能单独证明动作已落实。

## 10. 视觉规范

驾驶舱复用 `console/static/console-tokens.css`：

- 页面背景：`--bg-page: #f8f9fa`；
- 表面：`--bg-surface: #ffffff`；
- 浅层区块：`--bg-subtle: #f4f5f7`；
- 默认边框：`--border-default: #e5e7eb`；
- 主色：`--primary: #5e6ad2`；
- 状态色复用 success/warning/danger 及其浅色背景。

页面保持现有 Console 的圆角、间距、字体、按钮、状态标签和工作台导航风格。不使用大面积深色背景，不建立新的颜色变量体系。

## 11. 异常与降级

- Controller 服务不可达：页面保持最后一次明确标时的快照，同时醒目标记实时状态未知，禁止危险动作。
- Projection 部分读取失败：逐块显示 unknown，保留可验证部分，不把失败降级为“无卡点”。
- 动作超时：显示“结果未确认”，按 operation/action ID 查询状态，禁止直接建议重复执行。
- 部分成功：保留已生效步骤与后续恢复入口。
- 并发变化：后端拒绝陈旧 expected identity，前端刷新并定位变化对象。
- 历史 Task 或 Pane：允许只读查看，不提供对当前代次生效的动作。

## 12. 验收标准

### 12.1 功能集中

- 存在当前 Workflow 的唯一完整 Controller 驾驶舱。
- 现有 Controller 功能盘点无遗漏，每项均有迁移结论。
- Canvas、Task、Pane 和恢复入口不再维护第二套控制规则。
- 同一动作在所有入口共享 Action ID、请求参数、后端校验和回执表达。

### 12.2 权威性与失败边界

- UI 与 StateStore 中的 Workflow、Task、Run、Pane、candidate 和 recovery 状态一致。
- 陈旧 version、跨 Workflow Task、Pane 实例变化、旧 execution/candidate 和未结恢复责任均被拒绝。
- 读取错误显示 unknown，不显示空态或成功态。
- 部分成功不会诱导用户重复已生效步骤。

### 12.3 真实调用链

至少验证一条真实链路：

```text
浏览器按钮
  → Console HTTP API
  → Controller Action 执行边界
  → CLI / Kernel / Store / Pane
  → SQLite 与运行现场读取
  → 驾驶舱回执刷新
```

测试替身可以替换收费模型或外部 Provider，但不能替换本次验收的 Controller、持久化或动作状态变化。

### 12.4 浏览器验收矩阵

- 全自动 Workflow 正常推进，用户无需机械确认；
- 半自动节点展示依据、影响和确认入口；
- Task 指导、催办、中断与返工；
- 等待容量、等待登记和恢复责任；
- 门禁 blocked、force pass 和部分推进失败；
- Git pipeline 与关闭工作流；
- 陈旧页面、双击、跨 Workflow、实例变化和服务不可达；
- Canvas、Task Drawer、Pane 与恢复入口均正确定位驾驶舱；
- 宽屏、窄屏、键盘焦点与浅色视觉一致性。

## 13. 实施分段建议

本设计批准不等于授权实施。后续实施应单独进入 Unified Dev Flow，并按以下顺序拆分：

1. 建立 Controller 功能盘点及 Cockpit Projection，只读展示当前判断与待办；
2. 集中现有 Action 对象和执行回执，不新增动作能力；
3. 将旧入口改为同源快捷入口和上下文定位；
4. 增加模板级 automatic/supervised 策略；
5. 完成真实浏览器到 SQLite/Pane 的自验证 E2E。

每个阶段保持现有 Controller 行为、调用顺序和兼容接口，不以 UI 重构为理由改写状态机。

## 14. 产品规划与技术债

以下事项不阻塞第一版：

- 运行中的 Workflow 临时覆盖模板控制策略；
- 跨 Workflow 的全局 Controller 运维大屏；
- 用户领域能力画像与组织审批授权；
- 长期趋势、容量预测和预测性调度；
- 更丰富的 Controller 判断解释，但不得让模型成为事实源。

## 15. 已拒绝方案

1. **独立全局 Controller 运维后台**：第一版信息量过大，脱离用户当前 Workflow 上下文。
2. **继续把功能分散到现有页面**：无法形成 Controller 心智模型，并继续制造多套按钮和规则。
3. **前端直接拼 CLI 命令或写状态**：绕过权威校验、恢复责任和并发保护。
4. **所有重要动作强制人工确认**：不懂领域的用户只能机械点击，产生虚假审核。

最终选择：Workflow 内的集中式 Controller 驾驶舱；默认全自动，模板可配置有效的半自动确认；其他页面只保留同源快捷入口。

## 16. 当前实现与验证边界

Controller 仍是后端控制平面。`herdr/controller_cockpit.py` 从同一 Workflow
的权威快照生成只读投影；`console/controller_cockpit_api.py` 装配 HTTP 读取和
Action 执行。Canvas、Task Drawer、Pane 和 Dashboard 保留带 Workflow、Node、
Task、Run、Pane 或 Operation 身份的定位入口，操作在驾驶舱集中确认。

`control_policy`、Node `control_mode` 与 `confirm_before` 从 Workflow 的
SQLite 配置快照解析。缺省策略为 automatic；半自动策略生成待确认 Action。
运行时临时覆盖策略尚未实现。人工确认提交服务端 `action_id` 和
`expected_identity`；服务端重新推导 Action，旧版本或 Pane 身份被拒绝。
指导弹窗接受指导正文，指导队列回执绑定当前 Run。

默认安全执行注册表只包含 Workflow `pause`、`resume`，写入前在同一 SQLite
事务中复查身份与策略。暂停属于显式人工意图；存在 `pause_workflow` 或
`step_workflow` 等人工暂停事件时，恢复必须再次确认，自动 sweep 不撤销暂停。
其他 automatic 策略动作若没有注册自动 handler，投影保持策略值，并给出
`execution.mode=manual`；Cockpit 显示“手动执行（后台未注册自动处理）”按钮。
后台只执行符合条件且已注册的动作，不反复为未注册推荐制造拒绝回执。直接请求
不支持的自动执行仍返回 `unsupported_handler`，恢复冲突仍 fail closed。

真实节点派发从固定 Workflow 配置解析 launch 策略。要求确认时，现有
`workflow_recovery_operations` 中保留 `waiting_human` 义务；Cockpit 的
`confirm_dispatch` Action 在核对 Operation version、generation、依赖及旧派发责任后，
把处理人、理由、时间和 identity_key 写入该 Operation 的持久确认记录和决策事件。
reconcile、claim、start、launch intent 与 Task 登记共同消费它。普通 retry 不构成确认；
重启和同一代次重试保留确认，新的 execution/config/candidate 代次不能继承旧确认。
确认本身不宣称已经派发，也不改变 admission → lease → wait → launch → receipt 的顺序。

执行查找按 Action ID 重新推导精确目标，独立于 100 项展示窗口；恢复、事件读取 N+1
行才能判定截断。纯回执分类在 `herdr/controller_receipts.py`，Console 与 Controller 共用。

回执在所选 StateStore 数据库的 `cockpit_action_receipts` 表持久保存，区分：

- `applied`：权威状态、事件或确切指导队列条目已回读确认。
- `accepted_waiting`：责任已受理，或副作用结果尚未确认；终端返回不等于业务完成。
- `partial`：部分步骤已生效，失败步骤单独展示；刷新只回读，不重发已生效步骤。
- `rejected`：身份、策略或能力校验拒绝，页面保留过期上下文并刷新。

派发 Operation 在 Task 尚未登记时保留 `awaiting_result`，只有与该 Operation、
Workflow 执行身份和 launch intent 匹配的 Task/Run 登记后才结案。读取失败显示
未确认状态并禁用 Action，不以旧缓存显示“暂无待办”。

`tests/test_console_controller_cockpit_e2e.py` 使用本地 Console HTTP server、
ego-browser 与临时 SQLite，覆盖指导、暂停恢复、模板确认、身份替换、中断、
催办等待、派发登记、部分成功、读取失败和 Canvas 定位，并检查 1440px/390px
布局。终端传输与服务存活探测受控；真实 Agent 执行、付费模型、已安装服务、
Git 交付全链路和生产 UAT 不在该 E2E 的证明范围。门禁部分失败用例验证既有
兼容执行请求到真实 Kernel 的路径，未把受恢复责任禁止的操作重新加入界面。

## 17. 可见入口盘点与迁移结果

| 原入口 / 操作 | 当前入口与权威执行 | 回执 / 保留边界 |
| --- | --- | --- |
| 页头暂停 / 恢复 | 定位 Cockpit；`pause` / `resume` Action | SQLite 状态流转事件；人工暂停不会自动恢复 |
| 页头单步 | 定位 Cockpit；`step` Action | 复用 Kernel 的就绪节点检查并保持暂停；明确提示尚未派发任务，不声称单步已启动 Agent |
| 页头节点回溯 | 定位 Cockpit；每个节点独立 `rollback` Action | 目标节点、Workflow/Task 快照指纹、理由及确认；回读 rollback 历史 |
| 页头快照中心 | 定位 Cockpit；`checkpoint` 与每个快照的 `restore_checkpoint` Action | 真实 checkpoint_created / checkpoint_restored 事件；恢复需显式确认 |
| 页头进入下一阶段 | 定位 Cockpit；`advance` Action | 复用既有手动推进；没有业务证据时保留等待回执 |
| Task / Drawer 成果会签、Dashboard 会签 | 定位对应 Workflow/Node 的 Cockpit | 原会签是 force-pass / rollback，不是独立业务签署；只提供当前允许的同源动作，恢复冲突不绕过；产物仍从 Task 详情查看 |
| Canvas、Task、Pane 指导 / 催办 / 中断 / 返工 | 上下文定位 Cockpit，使用服务端 action_id | 自定义指导绑定 Run，催办等待，halt 的中断事件；默认 automatic 且无 handler 时可手动操作 |
| Git pipeline / escalation / 关闭 | Cockpit 当前 Action | 既有 CLI 与安全限制；未注册自动 handler 不声称后台执行 |
| 派发确认与恢复 | Cockpit 的 confirm_dispatch Action 和既有恢复裁决卡 | Operation 身份、版本与持久决策；确认不是 Task 登记 |
| 人工业务裁决记录 | Cockpit 业务裁决区，复用共享文档决策账本 | 记录业务结论，不直接启动、force-pass 或改变 Controller 状态 |
| 创建候选分支、指定后续执行者 | 保留既有仓库准备 / 执行者配置入口 | 这些是准备与配置操作，不作为 Controller Action 执行回执；不增加运行时 automatic/supervised 覆盖 |
| 不再被页面渲染的旧全局兼容函数与 HTTP API | 保留历史调用兼容 | 不构成第二个可见 Controller 操作面；新页面通过 Cockpit Action 执行 |

可执行盘点证据：`test_header_and_signoff_entrypoints_only_locate_cockpit` 验证旧入口不发 POST，
`test_header_pause_checkpoint_rollback_and_step_use_cockpit_receipts` 验证真实页面与 SQLite 回执，
`test_default_policy_keeps_steer_redrive_halt_operable` 验证默认策略的手动可用性。
