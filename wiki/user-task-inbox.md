# User Task Inbox: 用户统一任务收件箱与自动调度 (user-task-inbox.md)

> **公司：上海共事智能科技有限公司**  
> **品牌：共事**  
> **产品：HAFlow**  
> **一句话：让人和多个 AI Agent 一起把事情做完**  
> *Human + Agent, in Flow*  

---

## 1. 核心定位与设计原则

User Task Inbox V1 是 HAFlow 的用户级任务接入、排队缓冲与自动化调度中枢。它解决了用户在派发任务时必须等待当前任务执行完成才能手动派发下一条指令的瓶颈，实现**任务接入与任务执行彻底解耦**。

### 核心架构原则
1. **Inbox 属于用户**：
   - 统一收件箱全局挂载于用户身份，汇聚人工自然语言指令、未来 GitHub Issues 及自动化事件。
2. **Task 必须明确项目与工作区**：
   - 新建草稿和排队任务均需提供合法的 `project_id` 与其绑定的 `workspace_id`。历史草稿可通过确认接口补齐归属。草稿在用户确认入队前保持 `DRAFT`。
3. **工作区单任务互斥与跨空间并行**：
   - 同一工作区同一时刻严格只允许执行一个任务，保障代码基线与分支状态不发生碰撞。
   - 当任务进入 `WAITING` 状态（底层工作流暂停等待用户输入/审批）时，该任务继续持有当前工作区互斥锁，后续排队任务保持 `PENDING`，绝不发生协调者 Pane 抢占与代码分支双写。
   - 不同工作区互不阻塞，独立并行调度。
4. **优先级与自动接续**：
   - 待执行队列按优先级（`URGENT` > `HIGH` > `NORMAL` > `LOW`）结合提交时间 FIFO 排序。
   - 当前任务完成后，调度器（`InboxDispatcher`）原子收敛终态并释放工作区锁，自动触发下一任务启动，全过程无需人工二次干预。

---

## 2. 状态机模型

Inbox 任务具备严格受控的 8 状态转换模型（`herdr/inbox/transitions.py`）：

```
[ 创建 (未确认) ] ──> DRAFT ──(confirm_scope)──> PENDING
[ 创建 (已确认) ] ─────────────────────────────> PENDING
                                                   │
                ┌──────────────────────────────────┴────────────────────────────────┐
                │                                                                   │ (cancel)
                ▼                                                                   ▼
             CLAIMED ──(start)──> RUNNING ──(need_human)──> WAITING              CANCELLED
                │                    │                        │
                │ (fail)             │ (complete)             │ (resume)
                ▼                    ▼                        ▼
              FAILED             COMPLETED                 PENDING
                │
                └──(retry)──> PENDING
```

- **合法跃迁约束**：`validate_inbox_transition(from_state, to_state)` 强制校验。
- **终态保护**：`COMPLETED`、`CANCELLED` 为不可变终态；`FAILED` 仅允许经由显式 `retry` 跃迁回 `PENDING`。

---

## 3. 持久化存储 (`user_inbox_tasks`)

数据库表结构定位于 `~/.herdr-controller/state.db`，由 `herdr/inbox/schema.py` 统一定义并接入 `herdr/state/schema.py` 初始化流水线：

- **主键**: `id` (`TEXT PRIMARY KEY`, 形如 `inbox_xxxxxxxxxxxx`)
- **关联字段**: `project_id`, `workspace_id`, `workflow_id`, `worker_id`
- **核心数据**: `title`, `instruction`, `priority`, `status`, `source_type`, `source_id`
- **并发与恢复**: `(user_id, idempotency_key)` 用户范围唯一防重索引, `lease_expires_at`, `heartbeat_at`, `attempt_count`, 预挂载 `workflow_id` 防奔溃重跑, `execution_history_json` 独立审计历史
- **审计时标**: `created_at`, `updated_at`, `started_at`, `completed_at`
- **索引体系**: 
  - `idx_inbox_dispatch`: `(status, priority, created_at)`
  - `idx_inbox_workspace`: `(workspace_id, status)`
  - `idx_inbox_project`: `(project_id, status)`
  - `uq_inbox_user_idempotency`: `(user_id, idempotency_key)`
- **Schema 迁移保障**: `ensure_inbox_schema` 自动检测并迁移旧表全局 `idempotency_key UNIQUE` 约束，确保多用户同名幂等键兼容。

---

## 4. 调度与收敛机制 (`InboxDispatcher`)

- **工作区繁忙检测 (`is_workspace_busy`)**：
  - 检查收件箱内是否存在处于 `CLAIMED`、`RUNNING` 或 `WAITING` 状态的任务（`WAITING` 状态持续持有工作区锁保护执行现场与协调者 Pane）。
  - 核对当前工作区及其项目是否存在尚未完成的 Workflow。`failed` 与 `closing` 持续占用工作区，关闭协议完成并写入 `completed` 后才允许领取后续任务。
  - 对已分配 Pane 的未登记启动，关闭流程持有 Workflow 启动锁。完整盘点确认 Pane 和 clone 均不存在，且 Launch Intent 证明原 Worker 尚未请求启动 Agent 时，可以释放工作区。一旦请求过启动，仍需原运行实例的进程退出证据。
  - 只有启动记录绑定了原终端及本机启动会话时，确认同一物理机或虚拟机已重启且原终端消失，可以在没有停止回执的情况下补记退出证据。同一次启动会话内，缺少可验证的进程退出证据时仍保持 `closing`。
  - 底层 `bin/herdr-factory` 与 `herdr/inbox/dispatcher` 严格对齐互斥语义：Factory 拒绝在存在活跃（含 `paused`）工作流时重复创建；Dispatcher 识别工作区占用并保持后续任务在队列排队等待，从根源杜绝协调者 Pane 抢占、代码分支冲突与重复启动失败。
- **原子领取任务 (`claim_next_task`)**：
  - 在 SQLite `EXCLUSIVE` 事务保护下，基于条件原子执行 CAS 跃迁，彻底杜绝多 Worker 重复领取。
  - 草稿确认归属（`confirm_task_scope`）在原子事务内计算目标队列末尾 `sort_order`，绝不发生插队。
- **预先绑定与崩溃自愈 (`launch_task` / `recover_stale_tasks`)**：
  - 启动前在 DB 中原子预分配并持久化目标 `workflow_id`。
  - 进程崩溃后，超时自愈逻辑检查底层工作流是否已创建。若已存在活跃工作流，恢复追踪，绝不重复生成新工作流。
  - 缺失或游离工作流经 60 秒宽限期后自动置为 `FAILED`，避免永久占用工作区。
  - Factory CLI 的 `run --db-path` 为注册、预检、状态更新、配置读取和失败收敛统一选择 SQLite。切换到与继承环境不同的数据库时，投影和运行定义使用该数据库独立的文件路径。首次初始化该数据库时跳过旧版 JSON 自动导入。
  - 调度过滤 Context 契约：Git 模式绝不注入 `--context` 参数，执行历史独立留存于 `execution_history_json`。
- **真实终态收敛 (`reconcile_running_tasks`)**：
  - 严格依据底层真实的 Workflow 状态进行收敛。底层 Workflow `completed` 才将收件箱任务置为 `COMPLETED`，底层 `failed` 则收敛为 `FAILED`，绝不凭借自述伪造成功。
  - `WAITING` 任务恢复为 `RUNNING` 时重新校验工作区是否空闲，避免并发冲突。
- **调度轮询集成**：
  - 集成在 `services/controller/registry.py` 控制器主循环中，并在每次任务提交、确认、重试、完成时触发即时扫描。

### 4.1 架构裁决：WAITING 状态下的工作区互斥与资源释放演进 (ADR-INBOX-001)

- **问题背景**：
  - 需求期望用户任务进入 `WAITING`（等待人工审批/回复）时不长期阻塞同工作区的后续排队任务。
- **现状与底层技术约束**：
  - 在 HAFlow 当前底层架构中，工作区（Workspace / Worktree）与协调者终端（Coordinator Pane / tmux session）强绑定。
  - 处于 `paused` / `WAITING` 状态的工作流，其协调者终端内常驻有未提交的分支改动、未退出的进程和 Agent 上下文。
  - 若在尚未实现现场快照挂起（Checkpoint & State Suspension）前释放工作区并派发任务 B，任务 B 将被绑定至同一协调者 Pane，导致 terminal 污染、git working tree 脏写、甚至双写冲突。因此，**严禁简单放宽 Factory 门禁或绕过 `paused` 繁忙检查**。
- **V1 裁决方案 (V1 Safety Contract)**：
  - **安全底线优先**：V1 坚持以工作区互斥（Workspace Exclusivity）为第一不变律。
  - WAITING 任务持有工作区互斥锁，同工作区任务 B 在队列保持 `PENDING` 排队；不同工作区的任务（如空间 C、D）并行执行，不受任何阻塞。
  - 控制台明确展示工作区忙碌提示（`（工作区当前执行中：...）`），用户可随时调整优先级或取消。
- **V2 演进路径 (Workspace Checkpoint & Pane Suspension Roadmap)**：
  - 在 V2 中引入协调者挂起与现场快照协议（Suspend-to-Disk Checkpoint）：
    1. 工作流进入 `WAITING` 时，触发完整 Checkpoint 导出（Git stash/分支隔离 + Agent memory snapshot）。
    2. 优雅停机并回收 Coordinator Pane，释放 Workspace Mutex。
    3. 允许排队任务 B 接入并创建全新干净的 Coordinator Pane。
    4. 当 A 收到用户批示/唤醒时，重新进入工作区请求排队；待工作区空闲时，重新分配 Coordinator Pane 并从 Checkpoint 反序列化恢复执行。

---

## 5. 控制台前端集成

- **左侧导航 Rail**：增加收件箱入口（`#railNavInbox`），支持未完成任务数量红点（Badging）。
- **顶层工作流页签**：集成虚拟页签 `__inbox__`，实现与其他工作流画布的原生切换。
- **指标与列表视图**：指标卡片快速筛选（全部、待执行、执行中、等待拍板、已完成、失败、草稿），展示工作区忙碌提示。
- **智能推荐与交互**：
  - 粘贴或输入需求指令后，自动调用 `/api/inbox/detect` 进行项目与空间探测与置信度打分。
  - 用户可修改推荐的项目及其联动工作区。点击「确认归属并提交」后才创建任务。请求发送期间禁用提交按钮，本页面内重试相同内容会复用原幂等键，包括关闭后重开提交窗口。
  - 任务排序上移/下移（`moveTaskOrder`）、调整优先级、撤销与重新排队，全部对齐 4px 栅格规范并使用非阻塞对话框。

---

## 6. Evidence 证据契约

- DDL 与架构注册: `herdr/inbox/schema.py:USER_INBOX_TASKS_DDL`, `herdr/state/schema.py:INIT_STATEMENTS`
- 状态机合法性验证: `herdr/inbox/transitions.py:validate_inbox_transition`
- 持久化存储与幂等防重: `herdr/inbox/store.py:InboxStore`
- 智能项目探测: `herdr/inbox/detector.py:detect_project_and_workspace`
- 调度器与真实收敛: `herdr/inbox/dispatcher.py:InboxDispatcher`
- 控制台 API 路由: `console/inbox_api.py`, `console/http_server.py`
- 控制台前端控制器: `console/static/console/scripts/inbox.js`, `console/static/console/scripts/intake.js`, `console/static/console/styles/inbox.css`
- 10 任务自动接续端到端验证: `tests/test_inbox_e2e_pipeline.py:test_ten_tasks_continuous_auto_dispatch`

## Active Intake 草稿

已启用的 Active Intake 连接可为当前实例的认证操作员生成草稿。待分配事项从同一个 Inbox 查看。项目和工作区可空，界面允许分配给自己及撤销归属。外部任务的确认只保存回执，持久状态保持 `DRAFT`。普通人工草稿继续通过原确认路径入列。 [配置与验收](../docs/engineering/reviews/active-intake-task-materialization-v1.md)
