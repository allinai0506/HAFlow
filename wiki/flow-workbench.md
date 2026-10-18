# Flow Workbench (flow-workbench.md)

> **公司：上海共事智能科技有限公司**
> **品牌：共事**
> **产品：HAFlow**
> **一句话：让人和多个 AI Agent 一起把事情做完**
> *Human + Agent, in Flow*

Console 的 Workflow 主区是**运行态 DAG 工作台**：Header + Attention Hub + Flow Canvas + Node Inspector + Task Drawer + Controller Actions。它是只读投影层，不参与工作流定义与执行语义。

## 1. 分层职责

```
Workflow Definition (herdr/workflow.py normalize_workflow
                     + herdr/projects.py workflow_config_for)
        ↓
herdr/workflow_graph.py :: workflow_graph_projection()      [纯函数]
        ↓  {nodes, edges, context}
Dagre (rankdir=TB) 计算节点坐标   ← 只算坐标，不算业务
        ↓
AntV X6 渲染 / zoom / pan / fit   ← 只渲染，不持有真相
        ↓
Flow Canvas  →  Node Inspector（概览 / 任务 / 上下文 / 运行）
```

`workflow_graph_projection(workflow, tasks, blockers)` 无 I/O、无 DOM、无模型调用，可被 Web Console / API / Desktop 复用。

## 2. 真相来源

| 输出 | 唯一权威 | 备注 |
| :--- | :--- | :--- |
| `nodes` / `edges` / `label` / `node_type` / `depends_on` / `purpose` | 真实 Workflow Definition | 解析顺序：workflow_file 快照（`workflow_config_for`）→ 原始 `workflow_file` → 模板名回退。严禁按模板名或节点名硬编码 |
| `edges` | 节点 `depends_on` | 依赖方向决定箭头方向；不存在数组顺序推导的隐含串行关系 |
| `status` / 计数 / `agents` / `task_ids` | 真实 Task 记录 | 见 §3 聚合优先级 |
| `context.required` / `context.optional` | Workflow Definition 的 execution context contract | 无 contract 返回空数组；不伪造「已加载」 |
| `has_attention` | `controller_actions.resolve_workflow_blockers` 的真实 blocker | 与 Controller 建议同源 |
| Inspector 任务 / Runtime | `workflow_detail().tasks` | 有则显示，无则 `—` |

**Fail-soft**：legacy `stages`-only 或空定义返回有限 nodes + `edges: []`，不崩溃、不伪造连线。

## 3. 节点状态聚合

优先级严格确定，同一 Task 集合必得同一结果：

```
blocked > failed > rework > working > completed > waiting
```

- `blocked`：存在未 superseded 的 blocked Task，或 `stage_verdict == "blocked"`
- `failed` / `rework`：存在对应状态的存活 Task
- `working`：存在 `working` / `dispatched` / `pending` / `agent_done`
- `completed`：全部存活 Task 处于 `completed` / `committed` / `integrated` / `cleanup_ready` / `cleaned`
- `waiting`：无存活 Task
- `superseded` / 带 `superseded_by` 的 Task 不计入任何状态

语义与既有 `stage_summary` 一致，未引入第三套状态模型。

## 4. 依赖与离线约束

- `@antv/x6@3.1.8` → `console/static/vendor/x6-3.1.8.min.js`（全局 `X6`）
- `@dagrejs/dagre@3.1.1` → `console/static/vendor/dagre-3.1.1.min.js`（全局 `dagre`）
- 版本固定，vendored 入仓，保留上游 LICENSE；经 `<script src="/static/vendor/...">` 本地加载
- Console `Handler.send_static` 提供 `/static/`，带路径穿越防护（目标必须位于 `console/static` 内）
- 运行时**零 CDN**（无 unpkg / jsdelivr / cdnjs）；断网可用
- `scripts/install-herdr-console.sh` 负责 `rsync console/static/` 到 `~/.herdr-console/static/`，否则部署后 vendor 丢失

## 5. Read-mostly 契约

X6 的编辑能力全部显式关闭：`nodeMovable` / `edgeMovable` / `edgeLabelMovable` / `arrowheadMovable` / `vertexMovable` / `vertexAddable` / `vertexDeletable` / `edgeAddable` / `connecting` 全部置否。节点可点击，图可 zoom / pan / fit，但**不存在任何保存路径**，`depends_on` 与节点定义无法被 UI 改写。

生命周期：`initFlowGraph` / `renderFlowGraph` / `destroyFlowGraph` / `resizeFlowGraph` / `fitFlowGraph`。工作流切换必须销毁旧图并清空选中，避免重复 Graph 实例与陈旧监听。

## 6. 容器归属（Console 单页面唯一所有权）

`#tasks` 是被多方复用的共享容器：Workflow 任务列表、运维驾驶舱、我的仪表板、历史/未注册空间面板都写它。因此显示归属必须由单一入口裁决：

```
setWorkspaceMode('flow')   → 显示 #flowWrap + #flowSummary，隐藏 #tasks
setWorkspaceMode('list')   → 显示 #tasks + #stages
setWorkspaceMode('aux')    → 显示 #tasks（运维/仪表板/空间面板），隐藏 Flow 与视图切换
```

任何写 `#tasks` 的代码路径都必须显式 claim 容器。漏掉这一步的症状是「页面切换后看到的还是上一个视图」——内容已渲染，但被默认视图的 `display:none` 隐藏。

Inspector 默认选中同样必须确定性：blocked > failed > working > rework > waiting-next > first。

## 7. 复用既有链路

Inspector 不新建第二套详情或解卡系统：

- 任务行 → `openTaskDrawer(taskId)`（既有 Task Drawer，含概览/执行轨迹/产物/运行时）
- 阻塞节点 → `executeControllerAction(actionId)` + 既有 `/api/controller/execute-action`
- 视图切换到「任务列表」→ 既有 `renderTasks()`，不重写任务列表

## 8. 明确不做

Workflow Builder / 拖拽持久化 / Stencil / 版本化 UI / 发布 / MiniMap / ELK / React / React Flow / BPMN / 全局 IA 重构，均不属于本层。交付成果区也不改节点自动完成条件。

## 工作流主体加载

`FACT` Console 使用 `GET /api/workflow?id=…&lightweight=1` 读取目标 workflow 的既有 SQLite 快照，先渲染工作流、任务和图；不扫描 Git 工作区，也不探测实时 coordinator。必要的跨 workflow required_task_ids 按 ID 读取以保留归属错误诊断。默认详情接口仍保留完整响应。

`FACT` 交付成果、恢复操作、决策和实时状态随后通过各自接口补全。请求按类型串行、同地址 in-flight 去重；sequence 与 workflow_id 不符的旧响应被丢弃。等待阶段明确显示加载中，失败保留未知/读取失败。交付物扫描耗时不等于主体首次显示时间。

Evidence: `console/herdr_factory_console.py::workflow_detail/loadWorkflow/loadWorkflowExtras`；`tests/test_console_workflow_latency.py`。

## 9. 交付成果

`FACT` 工作流页在 `flow` / `list` 模式显示 `#deliverablesPanel`。Workflow、Node、Task 三处读同一份目录：Workflow 汇总各 Node，Node 汇总所属 Task，Task 抽屉「产物」只显示该 Task 的行。身份是 `sha256(task_id + kind + key)` 的前 20 位。

`FACT` 状态只使用已有事实。正式成果 `present`（已产生）只来自三种已有依据：节点输出契约里的文件名、任务上已经登记的产物或 Observation 路径、以及这次任务自己的 Git 变更。工作区里对不上这些依据的文件是 `workspace`（工作区文件），可以查看或下载，但不是正式交付。`required_outputs` 里带允许后缀的相对文件名，要到节点汇总时才判断；某个 Task 没写出的文件不会在其他 Task 上显示为未产生。当前任务都能读取、且都没有该文件时，节点才出现一条 `missing`（未产生）。已替代任务仍能校验和读取的文件标记为 `historical`（历史产物），可查看、下载，但不计入当前成果数量，也不能满足节点预期。提交仍是记录，不能当文件下载；历史 PR 链接可以打开。没有当前任务能够确认时，节点显示 `unconfirmed`（不可确认），而不是已产生或未产生。契约句子（如「测试结论（PASS / FAIL）」「招标基本信息表」）不是文件，不会生成未产生行。工作区不受管或文件不可读是 `unavailable`。目录读取失败写入 `error`。Workflow 与 Node 以中性、默认折叠的「部分文件暂时无法展示」提示保留未知状态，展开可查看原始原因；已发现成果继续展示。扫描达到 40 个文件就停止，并写入 `notice`「成果列表可能未完整。」页面不能把失败或截断显示成没有成果。文件存在不是验收通过。这一层不写入 `verified`，也不因成果推进节点。

`FACT` Context 模式只扫描该 Task 工作区里允许的业务文件，再按上面的依据区分正式成果和工作区文件。Git 模式不整库扫描，也不把派发时的 `baseline_commit` 或与它相同的 `candidate_sha` 当成这次成果。它列出相对该基线到当前 HEAD 的文件，加上 `git status --untracked-files=all` 里后缀允许的变更，并另列这次自己的提交、可靠的 http(s) PR 地址和变更清单。没有基线时，才使用任务记录里的交付 SHA。提交和变更清单不能当文件下载。 PR 链接补充读取同一 workflow/task 的 `herdr-task` 正式 `pull_request_created` 事件回执（按时间与事件 ID 取最新一条），兼容任务已有 PR 字段；创建意图及备注不是正式 PR 依据。工作区清理不影响 PR 链接展示。

`FACT` 查看和下载走 `GET /api/workflow/deliverable`。服务端按工作流和任务重建相对路径，拒绝工作区之外、路径穿越、符号链接越界、敏感文件名和超限文件。Markdown 预览先转义再渲染少量标记。下载响应使用附件、`nosniff` 和 `sandbox`。

Evidence:
- `herdr/deliverables.py` — `project_for_tasks` / `read_deliverable` / `resolve_workspace_file`
- `herdr/projection.py` — `project_workflow` 附带 `deliverables`
- `console/herdr_factory_console.py` — `api_workflow_deliverables` / `renderDeliverables` / `renderTaskArtifacts`
- `tests/test_deliverables.py`

Evidence:
- `herdr/workflow_graph.py` — `workflow_graph_projection` / `aggregate_node_status` / `pick_default_node`
- `herdr/workflow.py` — `normalize_workflow` / `context_contract_ids` / `validate_workflow_dag`
- `herdr/projects.py` — `workflow_config_for`
- `console/herdr_factory_console.py` — `workflow_definition_for` / `workflow_graph_for` / `setWorkspaceMode` / `renderFlowGraph` / `renderNodeInspector` / `send_static`
- `tests/test_workflow_graph_projection.py` — DAG 边精确性、并行拓扑、状态聚合、legacy fail-soft、自定义扇出、context 不伪造
- `tests/test_console_flow_workbench.py` — 视图切换、容器归属、画布生命周期、依赖缺失 fail-soft、无 CDN
- PR: https://github.com/allinai0506/HAFlow/pull/114

普通 `/` 首页恢复已记住的工作流也应先走轻量详情，不能先等待空间巡检和完整项目现场。系统页面及首次无历史选择的默认入口保留；后台空间与成果完成时间需单独测量。

Console 成果与 Controller 动作读取指定工作流的权威快照。成果投影共享进程内4个只读探测线程，每请求最多4项未完成、完成即补位，并按任务顺序汇总；同任务 HEAD 在一次投影内复用。上限指活跃线程，不代表跨进程或排队请求上限。整个首页验收须同时覆盖侧栏、PR成果、动作、决策与运行现场。

## 读取性能与隔离（2026-10-15）

`FACT` 项目列表通过 `/api/project?lightweight=1` 读取工作流元数据与执行者负载；`/api/project/runtime` 单独加载工位现场，pending/error 不表示工位为空。工作流直达入口绘制后不再因项目刷新重复读取同一主体。旧项目或空间的运行现场响应不能写入新对象。

成果目录的持久化补充使用 `herdr.deliverable_store.read_enrichment`，同一只读连接按 task 索引限量读取 artifact 与正式 `herdr-task` PR 事件。文件/Git 核心的所有权、可读性和错误语义不变。白盒详情定向读取任务，工作流白盒复用一次快照和全进程 4 个并发工位探测预算。

归档使用 SQLite 筛选、计数与分页，仅解码当前页；保留 Unicode 子串及状态组查询，非有限时间按无效时间 0 排序。日志读取最多 256 KiB 文件尾部，丢弃开头残行；不足请求行数时明确提示截断。

Evidence:
- `console/herdr_factory_console.py#project_detail`
- `console/herdr_factory_console.py#loadProjectRuntime`
- `herdr/deliverable_store.py#read_enrichment`
- `herdr/projection.py#project_workflow`
- `herdr/archive.py#query_archived_tasks_sqlite`
- `tests/test_console_page_latency.py`
- `tests/test_projection_archive_latency.py`
- `tests/test_deliverable_read_latency.py`

## 模板隔离与运行冻结定义（2026-10-17）

`FACT` 模板库选中态按空间隔离：`state.templateSelections[spaceId]`；`effectiveTemplateId()` 依次取 空间选择 → 项目实际模板（`project_detail` 新增 `workflow_template`，读项目 workflow.json）→ 默认模板。只有用户点选模板卡片、或保存模板成功后才会记账（`selectTemplateInLibrary(id,remember)` 传 `remember=1`）；程序化显示（模板库初始渲染、切空间刷新）不写选择，唯一例外是项目模板已删除/不可用时的回落对齐（把回落项同步为空间选择，见下）。切换空间时若当前在模板库视图，选中态随空间重绘（`rer&&state.shellView==='templates'` 时刷新，避免与 `paintShellPage` 重复渲染）。冷启动直开或恢复模板库视图（`?view=templates` / 恢复 `shellView==='templates'`）时先渲染库、再 `refreshAll()` 水合空间/项目后按有效模板重绘（overview 后台拉取，不阻塞首屏），避免选中回落默认模板、"选用并启动" 暂不可用。

`FACT` 阶段卡片与图投影同源"运行冻结定义"：`workflow_stages(wid,p,w,definition)` 解析顺序 definition → `w.config`（运行记录冻结定义）→ 项目 workflow.json → 内置 STAGES；`workflow_detail` 先用 `_referenced_task_inventory` 解析 definition 再渲染阶段，阶段、手动推进与回滚选项不再被项目后续换模板（reprovision）改写。

`FACT` "选用并启动需求"弹窗模板透传：`launchWithCurrentTemplate()` → `showNewWorkflow(effectiveTemplateId())` → `populateTemplateSelect('newTemplate',preferredId)`；preferred 不存在时回落 `software-development-v1`，异步重建下拉后显式 `sel.value=wanted`，不再静默重置用户选择。

`FACT` 迟到继续帧不得穿越空间/弹窗上下文：切空间或连续点选后，迟到的 `/api/template` 详情按 `state.templateDetailSequence` 序号丢弃；弹窗重开时旧弹窗迟到的下拉填充按 `state.templateSelectSeq[selId]` 丢弃；保存模板的记账固定写回发起空间（请求前捕获 `sid`，已切换空间时只记账不改写当前界面）；模板库回落（项目模板已删除/不可用）将回落项同步为空间选择，保证选中卡片、详情、DAG/编辑按钮与"选用并启动"一致。

Evidence:
- `console/herdr_factory_console.py` — `workflow_stages` / `_project_workflow_template` / `project_detail` / `effectiveTemplateId` / `rememberSpaceTemplate` / `selectTemplateInLibrary` / `populateTemplateSelect` / `launchWithCurrentTemplate` / `saveTemplate` / `selectSpace`
- `tests/test_console_project_creation.py` — 冻结定义优先级（直接调用 + `workflow_detail` 级）、`workflow_template` 暴露
- `tests/test_console_templates.py` — `TestTemplateLibraryBehavior`（空间隔离、仅用户点选记账、preferred 透传、保存模板记账、过期详情/填充丢弃、保存按发起空间记账、回落对齐、直开视图水合重绘）
- `tests/test_console_workflow_latency.py` — 启动调度性能回归（`__templates__` 先调度模板库、overview 后台水合不阻塞启动）

## 任务内容一致性

`FACT` 列表与流程图 Inspector 的任务页共用 `renderTaskRow()`，显示相同任务名称、ID、执行者、状态与操作菜单；侧栏仅改变布局。`flowTasksForNode()` 只按节点筛选同一份 `workflow.tasks`，保留已替代任务供查看历史；节点状态聚合仍排除已替代任务。概览的当前任务区也复用同一任务行。

Evidence: `console/herdr_factory_console.py::renderTaskRow/renderTasks/renderNodeInspector/flowTasksForNode`；`tests/test_console_execution_trace.py::test_flow_and_list_render_identical_task_content_and_history`。

## 同一工作流的数据刷新

`FACT` 画布复用同时检查 workflow ID 与完整 graph projection 的序列化键。节点状态、执行者、任务计数或边发生变化时重建节点和连线；投影未变时只更新选中态，避免每次读取都重建。销毁画布同时清理投影键。

Evidence: `renderFlowGraph/destroyFlowGraph`；`tests/test_console_execution_trace.py::test_flow_graph_refreshes_changed_projection_but_reuses_unchanged_graph`。隔离浏览器使用真实 X6/Dagre 验证同 workflow 的 working/claude/1 → completed/qodercli/2 变化，不修改生产任务状态。

## 统一单任务详情入口

`FACT` 点击已有任务的画布节点与点击列表任务，均调用 `openTaskDrawer(task_id)`。共用同一份任务/API 投影、详情抽屉和「概览 / 执行轨迹 / 产物 / 运行时」四页签，不再把节点模板概览当成该任务详情。节点存在多个执行记录时，默认选最后一条未替代任务；全部已替代时选最后一条历史任务。历史记录仍可通过节点任务列表选择，尚无任务的节点继续显示节点信息。

`FACT` X6 的 `node:click` 是唯一画布点击装配；删除 HTML 包装层重复点击监听，防止一次点击重复加载详情。

Evidence: `selectFlowNode/openTaskDrawer/onTaskRowClick`；`tests/test_console_execution_trace.py::test_canvas_node_and_list_row_open_the_same_task_detail`。

## 10. 交付管道可见性：commit → 集成 → 归档（issue #253）

`FACT` 一个 `integration_mode=git` 的任务进入 `completed` / `committed` 后，控制器 registry watcher 会自己跑完整条交付链（`_check_finalize_retry` → `finalize_completed_task`：commit → integrate → `cleanup_ready` → `cleaned`）。这段窗口里任务不是终态，界面却曾把它渲染成终态语感的「已提交」，同时给出人工「并入目标分支」按钮——用户看不出系统在等什么，按钮还与守护进程争抢同一分支。

`FACT` 现在由 `herdr/delivery_pipeline.py`（纯核心，无 I/O）给出唯一投影，工作台只渲染：

| 字段 | 权威来源 |
| :--- | :--- |
| `stage` / `stage_label`（等待保存版本 / 等待集成 / 等待收尾 / 等待归档 / 已归档） | `task.status` 与既有的交付链顺序 |
| `scheduler_owned` / `owner` / `owner_note` | 与守护进程同一守卫：运行中的 workflow + git 任务 + 非 context + 未 `finalize_escalated` + 状态仍在重试范围内（`completed` / `committed`）；未知状态一律不宣称接管 |
| `gates` | 该节点**直接下游**且未 `pass` 的门禁节点（门禁定义复用 `workflow_progress` 的唯一定义，含默认 `test`/`review`/`wrapup` 回退）；有效的手工放行覆盖同样算已结算 |
| `commit` / `integration_branch` / `candidate_sha` | Task Store 既有字段 |
| `candidate_published_at` | `events.candidate_published`（`source=herdr-task`）按 task 取最新一条；缺失写「时间未记录」，不猜测 |

`FACT` 渲染契约：徽章只在调度器接管时改为阶段标签（`等待集成` + 彩色在途样式），并在同排显示可见的等待原因「等待门禁: …」；未被接管的任务继续显示原有状态标签。调度控制的「继续推进」区块对这类任务不再产出任何人工动作，改列调度器归属说明。节点卡片（Inspector 运行标签）与任务抽屉各增一行交付管道：`commit=<sha> → integration_branch → 候选发布 <时间>`。

`FACT` 只隐藏显示，不删除能力：`bin/herdr-task integrate` 等 CLI 兜底路径不变，`finalize_escalated` 的任务继续显示既有三条人工恢复动作。`task_git_step` 执行接口仍按 `GIT_PIPELINE_FORWARD` 表解析 argv，不依赖调用方传入命令。

`FACT` Inspector 任务页与列表共用 `renderTaskRow()`（见上文「任务内容一致性」），因此徽章与等待原因在两处一致；Inspector 运行标签仍各自渲染交付管道行。

Evidence:
- `herdr/delivery_pipeline.py` — `delivery_projection` / `scheduler_owns_next_step` / `pending_gate_nodes` / `stage_of`
- `herdr/workflow_progress.py` — `gate_retry_node`（门禁定义的唯一公开入口）
- `herdr/controller_actions.py` — `_git_pipeline_actions` 按归属跳过守护进程已接管的步骤
- `console/herdr_factory_console.py` — `attach_delivery` / `candidate_publication_times` / `api_task_projection` / `taskBadge` / `deliveryWaitHtml` / `deliveryPipelineLine` / `renderSchedulerAttribution`（列表与 Inspector 共用的 `renderTaskRow` 内渲染徽章与等待原因）
- `tests/test_delivery_pipeline.py` — 纯投影与归属反例（paused/非 git/context/已升级/未知状态）
- `tests/test_console_delivery_pipeline.py` — 服务端真实 SQLite + 事件账本读链、`workflow_detail` / `api_workflow_controller_actions`、Node 渲染断言
- `tests/test_controller_progress_actions.py` — 调度器接管不产出人工 integrate，其余链路动作保留
- PR: https://github.com/allinai0506/HAFlow/pull/264
