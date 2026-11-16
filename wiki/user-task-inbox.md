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
   - 每条任务必须具备合法且经后端校验的 `project_id` 与 `workspace_id`。未确认归属的任务保留为 `DRAFT` 状态，严禁进入可执行队列，绝不自动执行。
3. **工作区单任务互斥与跨空间并行**：
   - 同一工作区同一时刻严格只允许执行一个任务，保障代码基线与分支状态不发生碰撞。
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
- **并发与恢复**: `idempotency_key` (唯一索引防重提交), `lease_expires_at`, `heartbeat_at`, `attempt_count`
- **审计时标**: `created_at`, `updated_at`, `started_at`, `completed_at`
- **索引体系**: 
  - `idx_inbox_dispatch`: `(status, priority, created_at)`
  - `idx_inbox_workspace`: `(workspace_id, status)`
  - `idx_inbox_project`: `(project_id, status)`

---

## 4. 调度与收敛机制 (`InboxDispatcher`)

- **工作区繁忙检测 (`is_workspace_busy`)**：
  - 检查收件箱内是否存在处于 `CLAIMED`、`RUNNING` 或 `WAITING` 状态的任务。
  - 交叉核对当前项目是否存在运行中的 Workflow。
- **原子领取任务 (`claim_next_task`)**：
  - 在 SQLite `EXCLUSIVE` 事务保护下，基于条件原子执行 CAS 跃迁，彻底杜绝多 Worker 重复领取。
- **真实终态收敛 (`reconcile_running_tasks`)**：
  - 严格依据底层真实的 Workflow 状态进行收敛。底层 Workflow `completed` 才将收件箱任务置为 `COMPLETED`，底层 `failed` 则收敛为 `FAILED`，绝不凭借自述伪造成功。
- **调度轮询集成**：
  - 集成在 `services/controller/registry.py` 控制器主循环中，并在每次任务提交、确认、重试、完成时触发即时扫描。

---

## 5. 控制台前端集成

- **左侧导航 Rail**：增加收件箱入口（`#railNavInbox`），支持未完成任务数量红点（Badging）。
- **顶层工作流页签**：集成虚拟页签 `__inbox__`，实现与其他工作流画布的原生切换。
- **指标与列表视图**：指标卡片快速筛选（全部、待执行、执行中、等待拍板、已完成、失败、草稿），展示工作区忙碌提示。
- **智能推荐与交互**：
  - 粘贴或输入需求指令后，自动调用 `/api/inbox/detect` 进行项目与空间探测与置信度打分。
  - 任务排序上移/下移（`moveTaskOrder`）、调整优先级、撤销与重新排队，全部对齐 4px 栅格规范并使用非阻塞对话框。

---

## 6. Evidence 证据契约

- DDL 与架构注册: `herdr/inbox/schema.py:USER_INBOX_TASKS_DDL`, `herdr/state/schema.py:INIT_STATEMENTS`
- 状态机合法性验证: `herdr/inbox/transitions.py:validate_inbox_transition`
- 持久化存储与幂等防重: `herdr/inbox/store.py:InboxStore`
- 智能项目探测: `herdr/inbox/detector.py:detect_project_and_workspace`
- 调度器与真实收敛: `herdr/inbox/dispatcher.py:InboxDispatcher`
- 控制台 API 路由: `console/inbox_api.py`, `console/http_server.py`
- 控制台前端控制器: `console/static/console/scripts/inbox.js`, `console/static/console/styles/inbox.css`
- 10 任务自动接续端到端验证: `tests/test_inbox_e2e_pipeline.py:test_ten_tasks_continuous_auto_dispatch`
