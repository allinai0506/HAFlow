# Flow Workbench v1 — Spec (unified light adapter)

Route: resume=fresh, intent=feature, complexity=large, risk=[], domain=[ui,api],
delivery_target=working_tree, spec_backend=unified, controller=none,
proof_mode=acceptance-test + visual, assurance=deterministic.

## 1. Goal / Non-goal

Goal: Workflow 主区从 Stage Stepper+Task List 升级为 Header+Attention+Flow Canvas+Inspector+Drawer+Controller，默认 Flow First，准确表达 DAG（含 test/review 并行、wrapup 汇聚）。
Non-goal: 不改 Scheduler/Router/执行语义；不做 Builder/拖拽持久化/Stencil；不做全局 IA 重构；不做 MiniMap/ELK/React；不断网 CDN。

## 2. Truth sources

- nodes/edges label/node_type/depends_on/purpose: `herdr.workflow.normalize_workflow` + `herdr.projects.workflow_config_for(workflow_id)`（优先 workflow_file 快照，回退模板）。
- status/counts/agents/task_ids: 真实 tasks（`herdr.kernel.load_tasks_data` 经 console `tasks_for_workflow`），聚合复用 `stage_summary` 思想，Graph 优先级 blocked>failed>rework>working>completed>waiting。
- context: `normalize_workflow` 的 execution/context contract（required/optional），无 binding 时只显示 Contract，不伪造已加载。
- controller: `herdr.controller_actions.resolve_workflow_blockers + generate_controller_actions`，执行走已有 `/api/controller/execute-action` + `executeControllerAction`。
- runtime: task pane/agent/status/timestamps/candidate_sha 等已有字段，有则显示无则 `—`。

## 3. API contract

`GET /api/workflow?id=` 新增 `graph: {nodes[], edges[]}` + `context: {required[], optional[]}` + `workflow_template`。纯函数 `herdr.workflow_graph.workflow_graph_projection(workflow_dict, tasks, blockers=None)` 可独立测试。Fail-soft：legacy/缺定义时返回有限 nodes、edges=[]，不崩溃不伪造。

Node: `{id,label,node_type,depends_on,purpose,status,task_count,active_task_count,completed_task_count,failed_task_count,blocked_task_count,agents,task_ids,has_attention,downstream[]}`。Edge: `{from,to}` 仅由 depends_on 生成。

## 4. Frontend contract

- `[流程图][任务列表]` toggle，默认流程图；List 复用 `renderTasks()`。
- `#flowCanvas` + `initFlowGraph/renderFlowGraph/destroyFlowGraph/resizeFlowGraph/fitFlowGraph`，X6 只做渲染/zoom/pan/fit，Dagre(TB) 算坐标，禁止自研 layout。
- Inspector 340px，Tabs 概览/任务/上下文/运行；默认选中 blocked>failed>working>rework>waiting-next>first，确定性。
- Task 点击走 `openTaskDrawer(taskId)`；Controller 走 `executeControllerAction`；Attention Hub 保留压缩；Flow 下 Stage Stepper 弱化为摘要。
- vendor: `console/static/vendor/x6-3.1.8.min.js` + `dagre-3.1.1.min.js` + LICENSE，`/static/vendor/*` 本地服务，缺失时显式错误不白屏，无 unpkg/jsdelivr/cdnjs。

## 5. Acceptance

- software-development-v1 edges 精确 6 条，无 test->review。
- test/review 并列拓扑（depends_on 均为 implementation）。
- 聚合：completed+working+blocked → blocked。
- 自定义 A->B->{C,D,E}->F 无硬编码。
- Context 有则返回无则不造；legacy fail-soft；Drawer/Controller/ViewToggle/WorkflowSwitch/无 CDN/JS syntax/DOM hooks 均有测试。
- 手工：启动 console 肉眼 diamond 拓扑，点 implementation 看 Inspector，点 Task 进 Drawer，构造 blocked 看解卡，Zoom/Pan/Fit，切换 Workflow。

## 6. Slices / Checkpoints

S1 backend 纯函数+单测 → S2 console 接线+vendor+静态路由 → S3 Canvas+Inspector+Toggle+生命周期 → S4 verify/review artifacts。每个 slice 后跑专项测试。
