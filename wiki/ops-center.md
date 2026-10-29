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


## Required task 配置诊断

`FACT` 节点卡片 JSON 的 `completion_issues` 区分 required task 缺失、跨 workflow、跨节点、谱系循环和配置无效。配置加载失败时节点不会按默认规则误显示 completed。诊断不会将外部 workflow 的成果纳入本节点验收，`JOIN_MISSING_CANDIDATE` 判决保留。

`FACT` 项目与 CLI 文件配置入口拒绝已知跨域 required ID；未派发 ID 可以保留为未来义务，实际完成仍要求本节点能解析其谱系。配置结构要求唯一非空字符串列表，最多64个。

`UNKNOWN` 本次未验证生产 Console 是否展示新增 JSON 字段；API 诊断不等于浏览器呈现已更新。

Evidence:
- `herdr/scheduler.py#required_task_issues`
- `herdr/projects.py#validate_required_task_scope`
- `bin/herdr-task#load_workflow`
- `bin/herdr-task#_build_workflow_cards`
- `tests/test_fix_bug1002.py#test_ops_config_error_cannot_look_completed`

## Workflow定向配置与DAG前沿

`FACT` `node-config <workflow> <node>`返回当前节点和config_sha；`node-config-set`要求expected-sha、reason及required-task-id列表或clear-required-tasks。归属/结构校验后发布不可变workflow-local配置快照，权威workflow指针和node_config_updated审计同事务提交，不改共享项目原件。并发更新只接受匹配当前SHA的一方，另一方需重新读取。

`FACT` Console图投影和checklist显示completion_issues的具体原因；坏配置不会落入缺省completed。current_nodes和ready_nodes是DAG集合；仅当前/就绪frontier恰为一个节点时派生current_stage，并标current_stage_source=derived_nodes。并行节点不压成一个伪当前阶段。

`UNKNOWN` 新字段和checklist本地源码/测试已覆盖，生产浏览器显示尚未验收；未修改生产配置。全量结果待最终交付记录填写。

Evidence:
- `herdr/node_config.py#read_configuration` / `#update_required_tasks`
- `herdr/workflow_graph.py#workflow_graph_projection`
- `console/herdr_factory_console.py#workflow_definition_for` / `#workflow_graph_for`
- `console/herdr_factory_console.py` 的 `flowChecklist`
- `tests/test_fix_bug1002_config.py`

## 7. 运维读取与刷新预算（2026-10-15）

`FACT` Console 与 CLI 共用 `herdr.ops_center.build_ops_center_payload`。任务、工作流与当前周期候选回执取自同一个只读 SQLite 快照；查询不启动 CLI 子进程、不初始化 schema，也不逐工作流创建连接。保留已取代任务与 required task 归属校验。

运行现场按 Pane 去重；本进程共享最多 8 个在途探针，每次最多采样 8 个。探针失败、预算耗尽及未采样返回 unknown，`runtime_sampling` 标明 sampled/omitted；不得把未采样解释为健康。既有外部探针超时仍为 3 秒。多个浏览器共享的是单服务进程预算，不宣称跨进程全局限制。

Console 的运维、仪表板、归档、日志与经验查询通过 `pageRead` 合并同类在途请求；切换范围只保留最新等待项。GET 在途合并按路径与请求头隔离，完成后即释放，不是 TTL 缓存；POST 不合并。

Evidence:
- `herdr/console_queries.py#read_inventory`
- `herdr/ops_center.py#build_ops_center_payload`
- `tests/test_ops_center_read_path.py`
- `tests/test_console_page_latency.py`

## 8. Workflow 启动未完成记录识别与回收（2026-10-29）

`FACT` Factory 注册先于就绪：`register_workflow` 写入 `status=running` 与 `startup_ready=false`，Deep Preflight 成功后才置 `startup_ready=true`。注册后到就绪前的失败由 Factory 收敛；进程被强杀等无法执行清理的路径由 Controller 的启动看门狗回收。

`FACT` 自动终态固定为 `status=completed` + `outcome=startup_failed`，并保留 `startup_ready=false` 与有界脱敏原因（`failed_stage` 为 `preflight` 或 `startup`，`startup_failure_reason` ≤2048 UTF-8 bytes）。不新增 Workflow 状态，不修改 `TERMINAL_WORKFLOW_STATUSES`，人工 `close-workflow --abandon` 仍产生 `outcome=abandoned`。

识别启动未完成的记录（SQLite `state.db`；只把 JSON 显式 `false` 视为未就绪，缺失/null/数字 0/字符串 "false" 与损坏 JSON 均保守排除）：

```sql
SELECT workflow_id, created_at,
       json_extract(metadata_json, '$.failed_stage') AS failed_stage,
       json_extract(metadata_json, '$.startup_failure_reason') AS reason
FROM workflows AS w
WHERE status = 'running'
  AND CASE WHEN json_valid(metadata_json)
           THEN json_type(metadata_json, '$.startup_ready') END = 'false'
  AND NOT EXISTS (SELECT 1 FROM tasks AS t WHERE t.workflow_id = w.workflow_id);
```

`FACT` 自动回收条件（全部满足才动作）：`status=running`、`startup_ready` 为 JSON `false`、该 Workflow 任务数为 0（任何状态的任务都算“有任务”）、`created_at` 严格早于 `now - HERDR_WORKFLOW_STARTUP_DEADLINE_SECONDS`、且非 foreign 记录。`paused/in_progress/closing/failed`、已就绪记录、损坏 JSON 一律不动作。

`FACT` 期限与节流：

- `HERDR_WORKFLOW_STARTUP_DEADLINE_SECONDS` 默认 `1800`（必须大于 Deep Preflight 的 210s 子进程超时）；非数字、≤0、≤210、`inf`/溢出均回退 1800，并只打印一次 `[STARTUP WATCHDOG CONFIG] invalid ...`。
- Controller sweep 每 60s 最多扫描一次；单次最多取 100 个候选、最多成功回收 20 条，按 `created_at` 升序。
- 候选查询在 SQLite 层过滤（`status`、`json_type(startup_ready)`、`created_at`、`NOT EXISTS(tasks)`、已知夹具路径），并用有界 keyset 游标推进；最老的 foreign 记录不会永久饿死其后出现的有效孤儿。
- 单条回收在单个 `BEGIN IMMEDIATE` 事务内重读并复核全部条件，再迁移到终态；重复扫描不产生第二条迁移事件。
- 提交后同步 `workflows.json` 投影并清理该 Workflow 的 stage-state 键；投影/清理失败只告警，不回滚已提交的 DB 终态，投影在下一次扫描时重试。

`FACT` 延迟边界：Controller 存活时，单条超期孤儿的回收延迟上界约为“期限 + 60s 扫描间隔 + 单轮批量排队”；积压 N 条有效候选时，每轮最多 20 条、轮间隔 60s。Controller 停机期间不承诺墙钟回收，重启后由 sweep 继续。若真实孤儿的 `workflow_file` 缺失/已删除或位于 `/tmp/`、`pytest-` 路径，会被判为 foreign 而不自动回收，保留人工清理。

人工清理（唯一人工兜底，不会自动执行）：

```bash
herdr-task close-workflow <workflow_id> --abandon
```

日志标记：Factory 失败收敛输出 `[WORKFLOW STARTUP FAILED] workflow_id=... stage=... converged=completed outcome=startup_failed` 与 `next:` 指引；已由他方终态时输出 `[WORKFLOW STARTUP CONVERGED] ... already_terminal=completed`；收敛失败输出 `[WORKFLOW STARTUP CONVERGE FAILED]` 与 `manual: herdr-task close-workflow <id> --abandon`。看门狗输出 `[STARTUP WATCHDOG RECLAIMED]`、`[STARTUP WATCHDOG ERROR]`、`[STARTUP WATCHDOG PROJECTION WARN]`、`[STARTUP WATCHDOG STAGE CLEANUP WARN]`。

`FACT` 启动等待日志限频：`[STARTUP WAIT]` 每 Workflow 首次 1 行、之后每 600s 最多 1 行，两个等待入口（推进 sweep 与队列投递）共用同一进程内闩；就绪时最多一条 `[STARTUP READY]`，关闭/回收后最多一条 `[STARTUP WAIT ENDED]`。限频状态不落库，Controller 重启后最多重新打印一次；缓存有界（默认 1024 个 Workflow）。

消费者影响（已用测试锁定）：`completed + outcome=startup_failed` 会被 active guard 排除，同项目可重新创建；不触发交付清单校验（仅 `outcome=delivered` 触发）、不产生完成通知（Notifier 按任务分组，零任务不发“Workflow 完成”）、不触发自动物理收尾；Controller 不再推进该 Workflow。看板“完成数”统计口径本轮未改，`startup_failed` 可能计入完成数，需按 `outcome` 区分。

`UNKNOWN` 本节点未执行浏览器级 Console 视觉回归；Console 的启动状态字段与标签由 Console 可见性任务负责，本页只描述已通过测试的持久化字段与运维行为。

Evidence:
- `herdr/workflow_startup.py#startup_deadline_seconds` / `#is_startup_orphan` / `#find_startup_orphan_candidates` / `#converge_startup_failure` / `#reclaim_startup_orphan`
- `herdr/state/transition_gate.py#update_workflow_metadata`
- `bin/herdr-factory#_converge_startup_failure`
- `services/controller/startup_watchdog.py#scan_startup_orphans`
- `services/controller/stage_state.py#clear_workflow_stage_state`
- `herdr/log_throttle.py#StartupWaitLog`
- `tests/test_workflow_startup_core.py` / `tests/test_workflow_startup_convergence.py` / `tests/test_startup_watchdog.py` / `tests/test_controller_startup_log_throttle.py`
