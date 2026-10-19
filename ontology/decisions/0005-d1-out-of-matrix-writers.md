# ADR-0005：状态机外写入的特权网关收敛

## 背景

在 HAFlow 任务生命周期规范（`wiki/task-lifecycle.md` L15）中宣称：“任何越权状态变更将被 CLI 直接拦截”。然而在 brownfield 实际代码考古中发现，存在 8 类绕过 `TASK_TRANSITIONS` 状态转移矩阵的直接写入路径（语义冲突 SC-018，详见 `decisions/0003-semantic-conflicts.md`）。

这些越权写入主要包括：
1. `completed -> rework`（`herdr/delivery_rework.py` 的交付物修复路径）；
2. `blocked/paused -> completed`（`herdr/kernel.py:bypass_task_gate` 门禁旁路放行直写 `save_task`）；
3. `任意非 superseded 状态 -> superseded`（`herdr/kernel.py:rollback_workflow` 以 `force=True` 批量作废）；
4. `任意状态 -> agent_done`（控制台 `console/task_api.py:api_task_force_review` 裸写数据库状态）；
5. `save_task` 的 upsert 覆盖写与 `restore_checkpoint` 的全量删除重建。

这些直接写入缺乏统一的事务 CAS 守卫与权威 `task_transition` 审计事件，存在并发数据覆盖、历史轨迹断裂及事实源撕裂的严重安全风险。

## Evidence

- `herdr/transitions.py` L19-35：`TASK_TRANSITIONS` 状态矩阵定义。
- `herdr/state_db.py` L6734-6738 & `herdr/delivery_rework.py` L102-108：`transition_task(..., status='rework', source='delivery-repair')` 越权边。
- `herdr/kernel.py` L832-957：`bypass_task_gate` 直接调用 `store.save_task(task)` 绕过转移网关。
- `herdr/kernel.py` L526-575 & `herdr/transitions.py` L73-74：`rollback_workflow` 的 `force=True` 绕过边合法性检查。
- `console/task_api.py` L266-290 & `console/http_server.py` L279：控制台 force-review 裸写 `tasks.status`，无守卫、无事件。
- `herdr/state_db.py` L1714-1731：`save_task` upsert 覆盖写状态。
- `herdr/state_db.py` L7719-7765：`restore_checkpoint` 清空重建。

## 决策

1. **确立单一入口铁律（Single Gateway Enforcement）**：
   - 严禁任何业务代码、CLI 或控制台通过裸写数据库 `status` 字段或调用 `save_task(task)` 改变任务生命周期状态。
   - 所有任务状态流转**必须且只能**经由具象状态存储网关（`state_db.transition_task` / `SQLiteStateStore.compare_and_set_task_transition`）执行原子 CAS 推进，并由同一事务持久化权威的 `task_transition` 审计事件。

2. **双轨制状态流转模型（Canonical vs Privileged Transitions）**：
   - **常规前向流转（Canonical Transitions）**：`TASK_TRANSITIONS` 矩阵保持严格单向生命周期约束，禁止向常规矩阵中盲目塞入逆向或越权边，确保常规 Worker 与普通 CLI 无法越权变更状态。
   - **特权管理流转（Privileged Transitions with Intent & Audit）**：废弃无语义的 `force=True` 暴力开关。所有管理例外与旁路操作必须在网关层显式声明受管 `intent` 并绑定权限策略：
     - `intent='delivery-repair'`：仅在交付物修复且授权通过时允许 `completed -> rework`，原子清空 `stage_verdict` 并记录 `task_transition(reason='delivery-repair')`；
     - `intent='operator-bypass'`：门禁旁路放行必须走网关完成 `blocked -> completed`，在 `clone_lock` 下原子核验 `HeadGuard` 并记录 `review_closure_skipped` 与流转审计事件；
     - `intent='rollback'`：工作流回滚走显式的受控批量流转 `* -> superseded`，附带操作员与原因凭据；
     - `intent='console-force-review'`：控制台强制转入人工审查必须重构为网关调用，校验 Origin、CSRF 头及操作员凭据后推进至 `agent_done`。

3. **本体模型规范落地**：
   - 在 `ontology/states/task_lifecycle.yaml` 中，将当前的 `out_of_matrix_writers` 声明升级为正式的 `privileged_transitions` 契约，逐一绑定目标状态、允许的触发源、必需的 `intent` 声明、对应的 `policy` 以及强制审计事件要求。

## 被拒绝方案

- **方案 1：将越权写入全部并入常规 `TASK_TRANSITIONS` 矩阵**。
  - 被拒绝理由：若常规矩阵放开 `completed -> rework` 或 `* -> agent_done`，普通 Agent 或 CLI 即可随时逆转已完成任务或跳过门禁，彻底破坏了交付确信度与流水线单向推进的系统不变量。
- **方案 2：继续保留直接 `save_task` 与裸写 status 现状**。
  - 被拒绝理由：直接写 DB 无法保证并发 CAS 安全，无法触发 `task_transition` 事件与 downstream hook，是导致状态撕裂的根因。

## 影响

- 控制台 `console/task_api.py` 的 `api_task_force_review` 必须重构为调用 `transition_task`，禁止直写 SQL。
- 内核 `bypass_task_gate` 必须彻底消除底部的 `store.save_task(task)`，改为调用转移网关。
- 全系统所有任务状态变更实现 100% 审计事件全覆盖。

## 兼容性

- 本体层属于规范升级，不构成破坏性变更。
- 代码层收敛采用向后兼容步骤：先在 `transition_task` 中支持 `intent` 参数与对应特权流转，逐步替换各业务处的裸写点，最后关闭 `save_task` 的覆盖写漏洞。
