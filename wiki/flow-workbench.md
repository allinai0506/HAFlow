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

- 任务行 → `openTaskDrawer(taskId)`（既有 Task Drawer，含概览/活动/产物/运行时）
- 阻塞节点 → `executeControllerAction(actionId)` + 既有 `/api/controller/execute-action`
- 视图切换到「任务列表」→ 既有 `renderTasks()`，不重写任务列表

## 8. 明确不做

Workflow Builder / 拖拽持久化 / Stencil / 版本化 UI / 发布 / MiniMap / ELK / React / React Flow / BPMN / 全局 IA 重构，均不属于本层。

Evidence:
- `herdr/workflow_graph.py` — `workflow_graph_projection` / `aggregate_node_status` / `pick_default_node`
- `herdr/workflow.py` — `normalize_workflow` / `context_contract_ids` / `validate_workflow_dag`
- `herdr/projects.py` — `workflow_config_for`
- `console/herdr_factory_console.py` — `workflow_definition_for` / `workflow_graph_for` / `setWorkspaceMode` / `renderFlowGraph` / `renderNodeInspector` / `send_static`
- `tests/test_workflow_graph_projection.py` — DAG 边精确性、并行拓扑、状态聚合、legacy fail-soft、自定义扇出、context 不伪造
- `tests/test_console_flow_workbench.py` — 视图切换、容器归属、画布生命周期、依赖缺失 fail-soft、无 CDN
- PR: https://github.com/allinai0506/HAFlow/pull/114
