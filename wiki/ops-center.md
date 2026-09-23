# Agent Operations Center (ops-center.md)

> **公司：上海共事智能科技有限公司**  
> **品牌：共事**  
> **产品：HAFlow**  
> **一句话：让人和多个 AI Agent 一起把事情做完**  
> *Human + Agent, in Flow*  
> `herdr-task ops-center` 为多 Agent 并行运行提供分层运维视图。它读取任务注册表并按需探测 Pane 对应 Agent；它是观测聚合层，不负责推进状态机。

## 1. 四层视图

`FACT` 输出按以下层次组织：

- `boss`: 工作中的 Agent 数、运行中的 Workflow 数、阻塞 Workflow 和待处理异常数。
- `workflow_cards`: 每个 Workflow 的节点卡片，包含 active/completed/blocked/failed/superseded 计数、运行时长和待下钻任务。节点与工作流级 `total` 只含存活任务：`status == "superseded"` 或带 `superseded_by` 的任务单独计入 `superseded`，不计入分母（谓词与 `is_node_complete`/`node_status` 一致）。全部任务被取代且无后继的节点标记为 `superseded` 状态，而不是 `empty`；下钻任务在全终态时取最新的权威任务。
- `agent_fleet`: 已知 Agent 与任务中出现的 Agent，包含健康状态、当前任务、负载、运行时长、最后结果和运行时状态。
- `anomalies`: 只列异常，并为每种异常提供建议操作与 `herdr`/`herdr-task` 命令链接。

默认返回卡片与摘要；`--include-tasks` 才返回完整任务摘要和状态轨迹，避免首页展开全部 Pane 细节。

Evidence:
- `bin/herdr-task#_ops_center_payload`
- `bin/herdr-task#_build_boss_summary`
- `bin/herdr-task#_build_workflow_cards`
- `bin/herdr-task#_node_task_status_counts`
- `bin/herdr-task#_build_agent_fleet`
- `bin/herdr-task#_build_anomalies`
- `tests/test_herdr_task_ops_center.py#TestSupersededStats`
- `tests/test_stage_advance_and_supersede.py#TestOpsCardParity`

## 2. 状态、时长与流动

`FACT` 任务摘要同时提供：

- `status`: 任务注册表状态。
- `runtime_status`: Pane 中 Agent 的运行时状态；探测失败时为 `null`。
- `last_activity_at`: 最后活动时间，并派生为 `last_event`。
- `started_at`、`elapsed`、`elapsed_bucket`: 运行时长及 `normal`（不超过 10 分钟）、`slow`（不超过 30 分钟）、`stuck`（超过 30 分钟）分桶。
- `status_trajectory`: 仅在 `--include-tasks` 下输出，由 `status_history` 的状态变更序列归一化得到。

因此 `idle` 不会被单独解释为“完成”：当任务仍在运行、Pane 为 idle/unknown 或运行时探针不可用时，Agent Fleet 会标记为 `STALE`，异常中心也会给出 `BLOCKED` 或 `CONTROLLER_RECOVERY` 信号。

Evidence:
- `bin/herdr-task#_task_summaries_for_ops`
- `bin/herdr-task#_task_status_trajectory`
- `bin/herdr-task#_task_healthy_status`
- `tests/test_herdr_task_ops_center.py#test_ops_center_marks_stale_agent`

## 3. 异常契约

`FACT` 当前识别 `BLOCKED`、`FAILED`、`AUTH_REQUIRED`、`TOKEN_EXHAUSTED`、`TRUST_REQUIRED`、`UPDATE_BLOCKED`、`ANCHOR_MISSING`、`PANE_NOT_FOUND` 和 `CONTROLLER_RECOVERY`。每条异常至少带有 Workflow、Task、Agent、Node、最后事件信息；若有 Pane，还提供打开 Pane 和日志的命令。

`INFERENCE` 这些命令链接是 Dashboard 后续交互按钮的后端契约草案；当前 CLI 只输出建议动作，不会自动执行重试、换 Agent、禁用 Agent 或修复。

Evidence:
- `bin/herdr-task#ANOMALY_ACTIONS`
- `bin/herdr-task#_classify_task_anomalies`
- `bin/herdr-task#_suggested_actions_for_anomaly`
- `tests/test_herdr_task_ops_center.py#test_ops_center_detects_anomalies`

## 4. 前端部署边界

`FACT` Console 前端源代码位于 `console/herdr_factory_console.py`，LaunchAgent 运行的是 `~/.herdr-console/herdr_factory_console.py` 部署副本。两者通过 `scripts/install-herdr-console.sh` 同步；脚本默认同步后重启 `com.user.herdr-factory-console`，`--no-restart` 可用于只更新文件。

Evidence:
- `console/README.md`
- `scripts/install-herdr-console.sh`
- `docs/operations/service-management.md`

## 5. Workflow 启动反馈

`POST /api/run` 使用异步 Job 协议：接口先返回 `202` 和 `job_id`，控制台随后轮询
`GET /api/run/status?id=<job_id>`。这样不会因为深度预检、Agent 路由或 Pane
装配超过 HTTP 超时而误报失败；后台 `herdr-factory run` 的允许时长为 600 秒。

- 成功：关闭“新建需求”窗口，刷新驾驶舱并显示 Workflow 标识。
- 失败：保留窗口，显示后端错误，恢复按钮并允许重试。
- 服务端同步校验失败仍返回 `500`，同时记录请求路径和异常文本，便于定位。

Evidence:
- `console/herdr_factory_console.py#start_workflow_job`
- `console/herdr_factory_console.py#workflow_job_status`
- `tests/test_console_run_job.py`

## 6. 启动状态同步

新 Workflow 的启动状态由 Registry 统一承载：`requirement` 保存需求正文，
`startup_ready=false` 表示仍在 Deep Preflight，完成预检并通过固定 Agent 校验后才写入
`startup_ready=true`。Controller 只对已打开门闩的 Workflow 发送首个节点事件，并把 Registry
中的需求正文附带到总指挥消息中。这样不会出现“阶段已通知、总指挥却没有需求”的半启动状态。

`herdr-factory` 不再直接向总指挥 Pane 注入启动消息，避免它与 Controller 同时写入同一个 Pane。

Evidence:
- `herdr/projects.py#register_workflow`
- `herdr/projects.py#mark_workflow_startup_ready`
- `bin/herdr-factory#start_workflow`
- `services/herdr-controller.py#check_workflow_stage_advance`
- `tests/test_workflow_start_sync.py`

## 7. 任务归档查询 (Task Archive Query)

`FACT` 控制台动作区「任务归档」提供跨项目、跨 Workflow 的历史任务检索列表。
查询核心是纯函数 `herdr/archive.py#query_archived_tasks`（过滤/排序/分页，无 I/O）；
控制台壳层 `archive_query` 从 StateStore（唯一事实源）读取任务。读取失败时请求失败，
不会把可能过时的 `tasks.json` 当作后备来源。

- 过滤：`project_id`（精确选择）、`workflow_id`（工作流下拉选择/项目级联/支持当前工作流默认预选）、`agent`（精确）、`status`（组别名或精确状态）、
  `q`（task_id / goal / 节点 / 项目 / 工作流 关键词）；
- 级联与工作流选择：提供 `GET /api/workflows?project_id=...` 轻量接口；弹窗打开时默认带入当前项目与当前工作流；项目切换时工作流下拉框自动级联更新；归档任务卡片上的 `workflow_id` 支持一键点击快速过滤；
- 状态组：`archived`（cleaned/superseded/failed，默认）、`active`、`all`，或任意精确状态名（如 `completed`）；
- 排序：`updated_at` 倒序（缺失回退 `last_activity_at` → `created_at`），`task_id` 升序兜底；
- 分页：`limit` 默认 50、上限 200，`offset` 越界安全；响应含 `total/count/limit/offset/status/items`；
- 下钻：每条任务复用既有「任务白盒简报」（`/api/task/projection`）。

Evidence:
- `herdr/archive.py#query_archived_tasks`
- `herdr/archive.py#summarize_task`
- `console/herdr_factory_console.py#archive_query`
- `console/herdr_factory_console.py#api_workflows`
- `tests/test_archive_query.py`

## 8. Console task status reads

`FACT` Console 的共享 `tasks()` 通过 `herdr_kernel.load_tasks_data()` 读取 StateStore。
普通 Workflow 详情、执行者负载、工位占用和 Task 详情都复用该入口。JSON `tasks.json` 是兼容投影，不是这些实时视图的查询源。

Evidence:
- `console/herdr_factory_console.py#tasks`
- `console/herdr_factory_console.py#tasks_for_workflow`
- `console/herdr_factory_console.py#agent_loads`
- `console/herdr_factory_console.py#task_detail`
- `tests/test_console_project_creation.py#ConsoleWorkflowStagesTest.test_tasks_for_workflow_reads_state_store_not_json_projection`
