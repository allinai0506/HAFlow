# 运行架构与进程拓扑 (architecture.md)

> **公司：上海共事智能科技有限公司**  
> **品牌：共事**  
> **产品：HAFlow**  
> **一句话：让人和多个 AI Agent 一起把事情做完**  
> *Human + Agent, in Flow*  
> **进程体系、通信机制与后台守护**  
> 关联索引: [[index]] | [[system-overview]] | [[task-lifecycle]] | [[tab-node-model]]

---

## 1. 进程拓扑与角色分层

HAFlow 系统的运行时由三种生命周期的进程构成：**CLI 工具链**、**常驻 LaunchAgent 守护进程**与**瞬时 Worker 任务进程**。

```mermaid
graph TD
    subgraph User_Space [CLI 交互入口]
        FactoryCLI["herdr-factory (项目装配/体检/模版)"]
        TaskCLI["herdr-task (任务生命周期/基线校验)"]
        PreflightCLI["herdr-preflight / deep-preflight (健康体检)"]
    end

    subgraph Daemon_Space [macOS LaunchAgent 常驻后台]
        Controller["herdr-controller.py (调度引擎 / 事件分发)"]
        Sentinel["herdr-sentinel.py (死锁看门狗 / 崩溃巡检)"]
        Notifier["herdr-notifier.py (原生 macOS 通知广播)"]
    end

    subgraph Ephemeral_Space [执行与工作区隔离]
        Worker["herdr-worker.py (CoW 克隆 / 现场装配)"]
        AgentProcess["Agent CLI (Claude / Codex / OpenCode ...)"]
    end

    subgraph Terminal_Multiplexer [终端多路复用核心服务]
        TerminalDaemon["Terminal Multiplexer (~/.config/herdr/herdr.sock)"]
    end

    User_Space -->|调用| Worker
    Worker -->|启动并重定向| AgentProcess
    Controller -->|Unix Socket 监听| TerminalDaemon
    Controller -->|读取/更新| FileStorage[(~/.herdr-controller/*.json)]
    Sentinel -->|定时巡检| FileStorage
    Sentinel -->|必要时重启| Controller
    Notifier -->|定时扫描| FileStorage
```

Evidence:
- `services/herdr-controller.py`
- `services/herdr-sentinel.py`
- `services/herdr-notifier.py`
- `services/herdr-worker.py`
- `CLAUDE.md:常用开发与运维命令`

---

## 2. 后台常驻守护进程详解

### Workflow Controller 驾驶舱

- `FACT` Controller 保持后端控制权。Cockpit 是 Workflow 级的投影和统一 Action
  界面，不创建浏览器调度器或第二份任务状态。Canvas、Task Drawer、Pane 和
  Dashboard 用包含对象身份的上下文入口跳转并定位；返回恢复原节点或抽屉。
- `FACT` 自动策略来自 StateStore 中的 Workflow 配置快照。缺省为 automatic，
  模板可设置 supervised 或指定动作确认。新增默认自动注册表仅启用在 SQLite
  事务中复查身份的 Workflow 暂停/恢复；显式人工暂停只能确认恢复，未注册自动
  handler 的动作明确显示手动路径。真实派发的模板确认写入既有 Operation 决策，
  claim/start/launch/registration 检查同一代次的持久批准；普通 retry 不能代替确认。
  关闭 coordinator intake 的兼容根节点也受同一策略约束；supervised 节点建立
  Operation 并等待确认，automatic 无 Operation 的兼容路径仍可用。
- `FACT` 人工请求传 `action_id` 和 `expected_identity`，执行边界重新推导动作。
  旧 Task 版本或 Pane 身份拒绝后回读。指导按当前 Run 记录；中断须实际适配器
  成功与状态/事件回读一致。回执使用同一数据库中的 `cockpit_action_receipts`，
  区分 applied、accepted_waiting、partial、rejected。读失败保留 unknown。
- `FACT` 派发通知成功仍可能等待 Task 登记。Operation 与 launch intent、Task、
  Run 的归属匹配后才能结案；页面刷新不重发已经执行的步骤。
- `FACT` 页头暂停、推进、单步检查、回溯、快照及原会签入口集中到 Cockpit。
  展示最多 100 个 Action，Workflow 控制优先于 Task 动作；执行及 Operation
  定位按精确 Action ID 重新查找。恢复深链接只显示匹配的派发确认动作。事件及恢复读 N+1 判定
  截断。回执纯逻辑由 `herdr/controller_receipts.py` 供 Console 和 Controller 共用。
- `FACT` 回溯、暂停/恢复、单步与快照在实际 SQLite 写事务中校验 expected execution
  和持久状态指纹；回溯的 Task 失效与 Workflow 元数据一起提交。快照意图指纹覆盖
  Workflow/Task 全部持久行，包括 agent_override；相同快照意图仍去重。恢复回执先
  按 checkpoint 身份查询再 LIMIT，不依赖最新两个其他快照的事件。
- `UNKNOWN` 以上隔离测试不能证明已安装服务或生产 Agent 的运行结果。运行中
  Workflow 临时改自动策略、跨 Workflow 运维大屏和组织审批链尚未实现。

Evidence:
- `herdr/controller_cockpit.py#build_cockpit_projection`
- `herdr/controller_actions.py#action_automation`
- `console/controller_cockpit_api.py#execute_controller_action`
- `services/controller/cockpit_automation.py#default_handlers`
- `services/controller/cockpit_automation.py#execute_action`
- `herdr/node_dispatch_store.py#reconcile_workflow`
- `herdr/recovery_store.py#decide_operation`
- `herdr/controller_receipts.py#classify_receipt`
- `herdr/controller_workflow_actions.py#workflow_control_actions`
- `herdr/workflow_control_store.py#validate_identity`
- `tests/test_controller_cockpit_bounded_fix2.py`
- `tests/test_controller_cockpit_final_fixes.py`
- `tests/test_console_controller_cockpit_e2e.py`
- `tests/test_controller_cockpit_automation.py`

### 2.1 Herdr Controller (`services/herdr-controller.py`)

- `FACT` **服务装配与模块职责**：入口保留配置、运行资源、兼容绑定和 `main`；实现按职责位于 `services/controller/`。`bindings.py` 将各操作声明的依赖绑定到同一入口实例，保留旧函数名、签名、配置替换和共享对象身份。实现模块可以独立注入其所需依赖，不反向导入服务入口。

  | 职责 | 实现模块 |
  | --- | --- |
  | 权威读取、状态转换与完成样本 | `persistence.py`、`task_state.py` |
  | 阶段、候选和规则化派发 | `stage_state.py`、`candidate.py`、`reverification.py`、`join_gates.py`、`direct_dispatch.py`、`advancement.py` |
  | 返工、作废和选择性补派 | `fix_loop.py`、`fix_invalidation.py`、`merged_fix_loop.py`、`selective_plan.py`、`selective_facts.py`、`fix_delivery.py` |
  | 协调器队列、投递和提示词 | `coordinator_queue.py`、`stage_delivery.py`、`task_events.py`、`coordinator_transport.py`、`coordinator_prompts.py` |
  | 验收和 Git 终化 | `auto_acceptance.py`、`auto_verdict.py`、`verdict_evidence.py`、`git_finalize.py`、`finalize_retry.py`、`finalize_support.py` |
  | 监督与旁路证据 | `supervisor_actions.py`、`supervisor_evidence.py`、`supervisor_execution.py`、`supervisor_checkpoints.py`、`done_events.py` |
  | 协作、监听与恢复 | `collaboration.py`、`handoffs.py`、`task_listener.py`、`runtime_reconciliation.py`、`registry.py`、`workflow_recovery.py` |

  Evidence:
  - `services/controller/README.md`
  - `services/controller/bindings.py#Dependencies`
  - `services/controller/coordinator_queue.py#_handle_coordinator_item`
  - `services/controller/stage_delivery.py#_handle_stage_advance_item`
  - `services/controller/task_events.py#_handle_task_event`
  - `tests/test_controller_modularity.py`

- `FACT` **核心职能**:
  1. **状态流转监听**: 维护与 Herdr Unix Domain Socket (`~/.config/herdr/herdr.sock`) 的持久连接，接收各 Pane 的实时 Agent 状态（如 `agent_done`, `error`）。
  2. **DAG 依赖推进**: 通过 StateStore 周期性读取权威任务状态；`tasks.json` 是兼容投影。当某节点的所有 Task 完成（状态到达 `cleaned` 或 `completed`）时，计算后续就绪节点（[[dag-workflow-engine]]）。
  3. **协调器注入**: 向项目总指挥 Pane (`coordinator_pane_id`) 输入结构化文本提示，指导总指挥 Agent 发起下一阶段 Task 派发。**2026-09-16 起降级为异常路径**：常规推进会优先走规则化直接派发（见下方 Direct Stage Dispatch），仅配置不足/需求缺失/launch 失败才回落总指挥。
  4. **重复防抖**: 利用 `stage-state.json` 记录 `queued` / `notified`，杜绝重复向总指挥发送推进指令。
- `FACT` **终态闸门（2026-09-13 幽灵推进事故后引入）**: 推进扫描只遍历注册表**非终态**条目（`herdr.projects.non_terminal_workflow_ids`，`status=="completed"` 视为终态）；`check_workflow_stage_advance` 对已关闭工作流早退；stage_advance 消费线程在**每次等待迭代**重新校验终态/注销，已入队事件在工作流关闭后被丢弃（`[STAGE ADVANCE DROP]`）。背景：零任务工作流对 `is_node_complete` 真空成立，无此闸门会被逐阶段"真空推进"并向共享协调者 Pane 注入幽灵提示，诱导其派发真实任务（wf-…-111426 事故，见 lessons §12）。
- `FACT` **Liveness Guard 控制面存活护栏（2026-09-16 6.5h 卡死事故后引入，lessons §41）**:
  策略与事件簿收敛在 `herdr/liveness.py`（纯逻辑）。三类机制：
  1. **有界等待 SLA**：总指挥事件投递与 stage advance 均设 SLA（默认 900s / 600s，env 可覆盖）；到期不再无限 `[COORDINATOR BUSY]` 空转，而是记录 attention 并释放 workflow 调度锁；
  2. **attention episode 投递保证**：投递失败/停滞写入 `~/.herdr-controller/attention.json`（attempts / next_retry_at），registry watcher 按指数退避慢速补投；`done` / `blocked` / `interrupted|paused` 事件均受此护栏，送达即清除；
  3. **注册表卫生**：夹具/临时 workflow（pytest-*、/tmp、已删除 workflow_file、无 project_id 空壳）不进入调度 sweep；`[WORKFLOW COMPLETE]` 单次闩；僵尸 pane 订阅指数退避封顶（2s→300s，8 次后慢重试一次告警）。
  启动时执行 `herdr integration status` 健康检查，缺失集成打印 `[INTEGRATION GAP]`（缺集成 → 屏幕探测误判是本次事故直接根因）。
- `FACT` **Direct Stage Dispatch 规则化推进会（2026-09-16 延迟优化，lessons §42）**:
  1. **常规推进（`[STAGE ADVANCED DIRECT]`）**：`herdr/direct_dispatch.py` 纯函数按节点模板（purpose / required_outputs / rules / default_task_type / default_integration_mode）与需求正文生成 Task 规格（节点字段为空时回退 `stage-policies.json`，兼容历史 workflow.json 旧快照），Controller 直接调用 `herdr-task launch`，不再等待总指挥 LLM 回合；fix-loop 回流时只补派"被作废且无替代"的子集任务（`-rN` 命名），`verdict=pass` 且已落定的任务保留（`[FIX LOOP SUBSET KEEP]`）；
  2. **例外回落**：节点无 purpose、需求正文缺失、legacy stages 路径、launch 非零退出才回落总指挥注入（`[DIRECT DISPATCH FALLBACK]`）；节点已有活跃任务时为 `wait` 模式（不注入、直接标记 notified）；`HERDR_DIRECT_STAGE_DISPATCH=0` 整段回退旧路径；
  3. **决策等待校准**：`wait_for_coordinator_decision` 默认预算 30s → 180s（`HERDR_COORDINATOR_DECISION_TIMEOUT`），超时不再立即重试，写入 attention（`decision_timeout`）按 `HERDR_ATTENTION_RETRY_INTERVAL` 退避；
  4. **提交门禁拆分**：Git 集成任务的 commit 由 Controller 下发 `HERDR_DEFER_HEAVY_TESTS=1`（目标仓 hook 识别该显式开关，不做仓库来源猜测，人类/Agent 手工提交仍走全量门禁），全量测试交给 workflow test 节点与 pre-push 门禁；
  5. **唤醒守卫**：存在活跃 workflow 时 Controller 持有 `caffeinate -i -s -w <pid>`（`[AWAKE GUARD]`），workflow 清零或进程退出自动释放，`HERDR_AWAKE_GUARD=0` 关闭。
- `FACT` **验证节点身份的权威判据（2026-10-22 引入，lessons §187）**:
  `herdr/node_roles.py` 是「该节点是否独立验证冻结候选」的唯一权威来源，判据来自模板
  **声明的语义**（`default_task_type` 经 `resolve_task_type` 解析后为 `test`）而非节点 id
  字面量；legacy id `test`/`review` 继续按 id 命中，因此 `software-development-v1` 与
  `spec-development-v1` 行为完全不变。提供三个入口对应三种数据形状：`is_verifier_node(node)`
  （模板原始节点或 `normalize_node` 后的派发节点）、`is_verifier_task(task)`（只有持久化 Task）、
  `is_verifier_node_id(config, node_id)`（只有 id，config 不可得时仍保住 legacy fail-closed）。
  `candidate_identity_applies(workflow)` 用权威 `herdr/workflow.py#execution_mode` 划定适用范围：
  候选 SHA 是 Git 事实，context 模式工作流没有候选可证明，fail-closed 门禁不得对其生效。
  背景：`code-craft-v1` 把门禁声明为 `cleanroom_test`/`cleanroom_review`，此前 12 个站点以
  `{"test","review"}` 字面量为条件，对这套模板全部静默失效（放行而非报错），导致
  `wf-haflow-1021-01` 两个洁净室门禁在 baseline 上跑、结论对候选无效。
  注意与 `herdr/task_cli_verification.py#_is_gate_task` 区分：后者回答「是否产出门禁结论」
  （含 `wrapup`/`acceptance`），语义更宽，两者不是同一概念。
- `FACT` **规则化验收 auto-accept（2026-09-17 引入，lessons §61）**:
  非门禁节点（requirements / plan / implementation 等无 gate 配置的节点）的
  `done` 事件不再必须排队等总指挥 LLM 回合：`herdr-task verify-baseline` 报告
  `TASK_CHANGED`（至少一个受控文件变更）即直接置 `completed` 并走既有
  finalize 链路（`[AUTO ACCEPT]`）。门禁节点（test/review/wrapup）与证据不足
  （`BASELINE_MATCH`）、配置不可判定的场景一律回落总指挥，绝不自动翻案；
  `HERDR_AUTO_ACCEPT=0` 整体关闭。背景：agent_done→completed 的总指挥验收
  等待实测占用 2.6h/8h，且其长回合会阻塞排在后面的门禁事件投递。
- `FACT` **被作废子集补派按谱系去重（2026-09-17 引入，lessons §61）**:
  `herdr/direct_dispatch.py#lineage_redispatch_candidates` 保证同一替换谱系
  （`x` / `x-r2` / …）最多只补派一发；旧逻辑会把历史作废任务反复补派，
  fix-loop 每轮 2→4→8 放大并发重复任务（[[dag-workflow-engine]] §4.3）。
- `FACT` **门禁规则化裁决 auto-verdict（2026-09-17 引入，lessons §62）**:
  门禁节点（test/review/wrapup）派发时注入结论契约
  （`herdr/direct_dispatch.py#gate_verdict_contract`：写 clone 外状态目录
  `~/.herdr-controller/gate-verdicts/<task_id>.json`（`HERDR_GATE_VERDICT_DIR` 可覆盖；
  权限受限时可退回 `<clone>/.herdr/gate-verdict.json`，该目录已被 `herdr-task` 内部过滤、
  不会进入交付）+ 终端输出 `HERDR_GATE_VERDICT: pass|blocked`）；`try_auto_verdict`
  合并文件（状态目录优先、clone 兜底）与屏幕两路信号，结论唯一一致时直接调用既有 CLI 契约
  `herdr-task set <task> completed --verdict ... --note ...` 落盘，blocked 自动进入既有
  fix-loop 回流。信号缺失/冲突、非门禁节点、`HERDR_AUTO_VERDICT=0` 一律回落总指挥。
  背景：门禁 verdict 过去必须由总指挥 LLM 从自然语言报告"转写"，566K tokens 上下文下单回合 10-20min。
- `FACT` **终化重试护栏全覆盖（2026-09-17 引入，lessons §63）**:
  `should_retry_finalize` 把 `committed` 与 `completed + integration_mode=git`
  统一纳入终化重试：episode 退避窗口外自动重跑幂等的
  `finalize_completed_task`（commit → rebase → integrate → cleanup），
  `HERDR_FINALIZE_RETRY_MAX`（默认 5）封顶，耗尽打印 `[FINALIZE RETRY EXHAUSTED]`
  并升级人工。背景：commit 门禁瞬时失败（flaky gate）曾让 `completed` 任务
  成为无重试死区，总指挥在一个 577K tokens 回合里手工重试 5 次、阻塞 65 分钟。
- `FACT` **close 等待 git 终化（2026-09-17 引入，lessons §65）**:
  `git_finalize_pending_tasks` 检查同 workflow 的 `completed`/`committed` + git 任务；
  命中则 `maybe_close_completed_workflow` 打印一次 `[CLOSE DEFERRED]` 并推迟，
  `close_workflow` CLI 同样 `[CLOSE ABORT]`。背景：wrapup 收官时后台 close 线程
  把 `completed` 任务抢先推进 `cleaned`，在跑的 `herdr-task commit` 子进程撞
  `Illegal transition: cleaned -> committed`，交付分支落不进集成链路。
- `FACT` **总指挥回合成本治理（2026-09-17 引入，lessons §66）**:
  1. **上下文卫生**：阶段边界（`[STAGE ADVANCED DIRECT]`/`[STAGE ADVANCED]`）与
     fix-loop 边界（`[FIX LOOP NOTIFIED]`）调用 `maybe_compact_coordinator` 向总指挥
     Pane 注入 `/compact`（仅 `opencode`/`claude` kind；忙则跳过；有界等待；
     `HERDR_COORDINATOR_COMPACT=0` 关闭）；2. **效率纪律**
     (`COORDINATOR_DISCIPLINE`) 注入全部事件模板：决策落盘即结束回合、
     禁止 commit/integrate/全量测试、只读核验优先。背景：`wf-nexusarchive-0917-01`
     总指挥上下文 94K→684K，584K 时单回合 LLM 生成 58.3min，事件串行等待
     累计 2.35h（`[COORDINATOR BUSY]` 187 次）。
- `FACT` **总指挥接单与模板切换（2026-09-18 引入，lessons §67）**:
  1. **接单机制**：新工作流首个节点（`stage=start`）默认路由协调者
     （`coordinator_intake_enabled`，`HERDR_COORDINATOR_INTAKE=0` 关闭），
     经 `HERDR_WORKFLOW_INTAKE_EVENT` 要求总指挥先理解需求再派发第一个 Task；
     非首节点保持 Direct Stage Dispatch 直派；2. **模板切换**：
     `ensure_project` 收到显式模板且与当前 `workflow.json` 不同时，若有活跃
     工作流则拒绝，否则保留 Workspace/协调者 Pane 并重建节点 Tab/Anchor
     （`reprovision_project_template`）；`run --template` 缺省 `None` 表示沿用。
     背景：`wf-nexusarchive-0918-01` 选 general-task-v1 实际按
     software-development-v1 运行，且启动未经总指挥。
- `FACT` **基础设施失败自动补派（2026-09-17 引入，lessons §60）**:
  registry watcher 对 `failed` 任务调用纯选择器 `herdr/liveness.py#select_infra_failures_for_recovery`
  （仅 `dispatch_delivery_fuse` / `agent_process_crash`，节点内无活跃任务，谱系失败次数 <
  `HERDR_AUTO_RECOVER_MAX` 默认 2，未 superseded）；命中后 `herdr-task supersede` 并清除该节点
  stage-advance 闩，由既有 sweep 的 direct dispatch 补派 `-rN` 替代任务（`[AUTO RECOVER]`）。
  质量类失败（test FAIL、总指挥判 failed）绝不自动翻案；此前 failed 任务只能人工
  `launch --supersedes`，实测造成 28 分钟级空等。
- `FACT` **创建闸门（herdr-factory 侧）**: `herdr-factory run` 在注册前持 per-project flock（`~/.herdr-controller/locks/<project_id>.workflow-create.lock`）原子执行「同项目活跃工作流检查 + 注册」；同项目已有非终态工作流时拒绝创建（exit 2，列出活跃工作流与处置指引），`--force` 显式 bypass（e2e 自动 bypass）。同项目工作流共享协调者 Pane 与阶段拓扑，默认必须串行。

### 2.2 Herdr Sentinel (`services/herdr-sentinel.py`)
- `FACT` **核心职能**:
  1. **崩溃模式拦截**: 每 3 秒巡检处于 `ACTIVE` 状态（`dispatched`, `working`, `blocked`, `rework`）的任务。
  2. **终端可见内容探测**: 通过 `herdr pane read <pane_id> --source visible` 捕获终端异常特征。
  3. **特征匹配**: 匹配 `"Bun has crashed"`, `"segmentation fault"`, `"panic(main thread)"` 等底层崩溃，并在 `tasks.json` 中标记 `sentinel_reason`，更新任务状态。
  4. **假死自动破冰 (Nudge Enter)**: 针对因按键卡顿处于假死状态的窗格，在超过 15 秒无响应时自动向 Pane 发送 `enter` 触发恢复。
  5. **停滞检测 (Stall Detector, 2026-09-16 引入)**: 对处于非终态且长时间（默认 1800s，env `HERDR_TASK_STALL_AFTER`）无任何状态推进的任务，打印 `[SENTINEL STALL]` 并推送 macOS 通知（补上此前"控制面停滞"完全失明的盲区）。
  6. **投递熔断 (Dispatch Delivery Fuse, 2026-09-17 引入, lessons §60)**: 对停留在 `dispatched` 超过 `HERDR_DISPATCH_DELIVERY_SLA`（默认 600s）的任务，先取证 Pane 屏幕（`_pane_delivery_evidence`：是否有 `HERDR_ORCH_TASK:<task_id>` 标记 + Agent 状态）；无标记且 Agent 非 `working` → 置 `failed`（reason `dispatch_delivery_fuse`，`[SENTINEL FUSE]`）并通知，有标记/已 working 只通知不处置；`HERDR_DISPATCH_FUSE=0` 关闭。与 Nudge 互补：Nudge 覆盖"有标记但假死"，熔断覆盖"投递完全失败无标记"（旧逻辑的最大盲区）。
  7. **自愈救援**: 发现 Controller 进程僵死时，主动执行 `launchctl kickstart -k` 重启 Controller。

### 2.3 Herdr Notifier (`services/herdr-notifier.py`)
- `FACT` **核心职能**:
  1. 异步轮询 `tasks.json`。
  2. 触发条件：当任务进入关注状态（`blocked`, `failed`, `human_review`, `needs_action`）或整个 Workflow 全部完成时。
  3. 通过 macOS 原生系统通知派发：优先使用 `terminal-notifier` 附带 `-open` 直达控制台 Deep-Link URL（`http://127.0.0.1:8765/?workflow_id=...&task_id=...`），未安装时安全降级为 `osascript`。

Evidence:
- `services/herdr-controller.py#main`
- `services/herdr-sentinel.py:CRASH_PATTERNS`
- `services/herdr-sentinel.py#nudge_enter`
- `services/herdr-notifier.py#notify`

---

## 3. 进程间通信与持久化契约

### 3.1 共享状态目录 (`~/.herdr-controller/`)
所有组件通过本地标准 JSON 文件通信与同步状态：

| 文件名 | 职责与归属 | 核心结构 |
| :--- | :--- | :--- |
| `tasks.json` | 全局工单状态机，所有组件的核心数据流 | `{"tasks": [Task, ...]}` |
| `projects.json` | 本地 Git 仓库到 Herdr Workspace/Coordinator 的映射中心 | `{"projects": {<root>: Project}}` |
| `workflows.json` | 运行中的工作流实例元数据 | `{"workflows": {<wf_id>: Workflow}}` |
| `stage-state.json` | Controller 内部阶段推进防抖状态 | `{<wf_id>:<stage>: "queued"\|"notified"}` |
| `attention.json` | Controller 的投递失败/总指挥停滞事件簿（Liveness Guard） | `{"episodes": {<task>:<event>: {attempts, next_retry_at, reason}}}` |
| `sentinel-state.json` | Sentinel 巡检状态（seen / nudged / stalls 停滞事件簿 / dispatch_fuse 投递熔断事件簿） | `{"seen": {}, "nudged": {}, "stalls": {<task_id>: {status, idle_seconds}}, "dispatch_fuse": {<task_id>: {waited_seconds, requeues, action}}}` |
| `agent-pools.json` | 各项目 Agent 白名单与偏好矩阵 | `{"projects": {<proj_id>: Pool}}` |
| `agent-reservations.json` | 动态预占锁中心（带 300s TTL） | `{"reservations": {<task_id>: Reservation}}` |
| `agent-router.lock` | 文件排他锁，保证并发分人安全 | `fcntl.flock` 目标文件 |

### 3.2 并发与原子写入保障
`FACT` 为防止多进程并发读写导致 JSON 损坏，代码中严格执行两种保护：
1. **原子替换**: 写入时先写 `<file>.tmp`，调用 `os.replace` 进行原子重命名。
2. **文件锁排他**: 对关键资源（如 `agent-reservations.json`）在读写前通过 `fcntl.flock(lock.fileno(), fcntl.LOCK_EX)` 获得独占锁。

Evidence:
- `herdr/agent_router.py#release_agent_reservation`
- `herdr/projects.py#_save`
- `services/herdr-sentinel.py#save_json_atomic`

---

## 4. 关键运维约束：LaunchAgent 进程生命周期

> [!CAUTION]
> **LaunchAgent 代码热更新陷阱**  
> 修改了 `services/herdr-controller.py` 或 `herdr/` 中的代码后，正在运行的后台 LaunchAgent **不会**自动热重载新代码，内存中仍旧跑着旧字节码！  
> **必须执行**: `launchctl kickstart -k gui/$(id -u)/com.user.herdr-controller`  
> **严禁操作**: 在终端直接执行 `python3 services/herdr-controller.py`（会导致多个 Controller 抢占同一个 Unix Socket 和状态文件）。

Evidence:
- `RULES.md:守护进程运维红线`
- `CLAUDE.md:坑点 1：LaunchAgent 进程更新陷阱`
