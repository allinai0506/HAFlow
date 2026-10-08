# HAFlow Review Dataset V1 候选集与收录台账

## 一、数据集概况

- **Dataset ID**: `haflow-review-dataset-v1`
- **总 Case 数**: 10 个历史 PR
- **Golden Defects 数量**: 10 个确凿缺陷
- **基线覆盖范围**: PR #85 ～ PR #155
- **语言栈**: Python
- **工程约束验证**: 100% 黄金问题所在文件均位于对应 PR 的 `git diff base...head` 改动范围内，且基础/目标 Commit 均在本地 Git 树完整可解析。

---

## 二、收录 Case 清单与 5 项证据全答复

### 1. PR #107: `allinai0506_HAFlow_107-3477d064`
- **缺陷位置**: `herdr/scheduler.py:152-165`
- **缺陷原因**: `extract_task_candidate_sha` 仅从任务属性中提取 candidate_sha 或 baseline_commit，未将派发声明（claim）与克隆现场客观证据（evidence/baseline_commit）分离并验证一致性。
- **触发路径**: 派发时声明验证候选 A 但克隆实际基于 B。
- **后果**: 未校验的声明仍会被当作有效覆盖证据，导致汇合门禁在 claim/evidence 不一致时产生错误判定。
- **工程证据**: 强修复证据。随后的审查与修复提交 `e6408e7df2b3313b01dd8c3f7fb4caf5567d9f1b`，明确拆分 `extract_task_candidate_claim` 与 `extract_task_verified_sha`，新增 `claim_evidence_mismatch` 校验拦截。
- **分类**: `evidence_claim` (Evidence / Claim)

### 2. PR #110: `allinai0506_HAFlow_110-2b9f2177`
- **缺陷位置**: `herdr/fix_loop.py:48-68`
- **缺陷原因**: `verdict_fingerprint` 盲目使用 `task_id`（代际任务 ID），导致后续轮次（-r2, -r3）对于同一阻塞原因产生不同的指纹。
- **触发路径**: 同一缺陷在后续轮次中重复发生。
- **后果**: 重复判定机制失效，错误地重复进入修复循环，透支循环预算。
- **工程证据**: 强修复证据。修复提交 `03fbf1434642953ddfda7bc868f80f693440b713` 闭环 3 个 P1 问题，重构指纹去除了易失 `task_id`，改为基于 affected 谱系统一计算。
- **分类**: `workflow_state` (Workflow / State)

### 3. PR #108: `allinai0506_HAFlow_108-7709da70`
- **缺陷位置**: `herdr/reverification.py:657-672`
- **缺陷原因**: `decision_identity` 和 `plan_identity` 在生成复验决策凭证时，未将具体的 `candidate_episode_id` 纳入身份构造。
- **触发路径**: 候选版本发生变更或多代候选并存。
- **后果**: 允许跨代际复用过期的通过结论，产生伪造的通过证据。
- **工程证据**: 强代码评审与修复提交 `2879a1f5926ec37e4c9f13661b09b91e92d733ec`，要求将复用事实严格绑定到 candidate episode，并在 `tests/test_selective_reverification.py` 中增加断言。
- **分类**: `identity` (Identity)

### 4. PR #155: `allinai0506_HAFlow_155-034cf32b`
- **缺陷位置**: `herdr/state_db.py:5450-5460`
- **缺陷原因**: 启动上下文校验逻辑未将 DAG 依赖闭包阶段纳入合法引用上下文，误将闭包内合法的上下文引用作为未知引用拦截。
- **触发路径**: 工作流多代际/多前驱依赖节点汇合执行。
- **后果**: 拒绝合法的工作流依赖闭包执行请求，导致复杂 DAG 工作流启动阶段即假死中断。
- **工程证据**: 强修复证据。修复提交 `89088523c91ea28dc5a0a3a789420067cbffcba0`，支持完整的 DAG 闭包验证。
- **分类**: `contract` (Contract)

### 5. PR #118: `allinai0506_HAFlow_118-6c7b4a96`
- **缺陷位置**: `services/herdr-controller.py:2860-2885`
- **缺陷原因**: Sentinel 巡检在处理 blocked 观测记录时，无前置快照校验直接发起 CAS 写入，造成无谓的高并发数据库争抢。
- **触发路径**: 多个观察项并发写入或大规模集群巡检。
- **后果**: 数据库写锁严重颠簸，引发 CAS storm 并阻断正常状态持久化。
- **工程证据**: 强修复证据。修复提交 `12a02b1f1ac797a7a14ee56d1f057863cbef1729`，引入乐观前置快照校验。
- **分类**: `concurrency` (Concurrency)

### 6. PR #102: `allinai0506_HAFlow_102-5cd1e0d7`
- **缺陷位置**: `herdr/state_db.py:1172-1187`
- **缺陷原因**: SQLite 以只读模式 (`mode=ro`) 打开 WAL 模式数据库时，若未先做头部检查，会尝试在只读文件系统上创建 sidecar shm/wal 文件导致报错。
- **触发路径**: 在只读挂载文件系统或只读沙盒中只读查询 SQLite。
- **后果**: 只读查询抛出 sqlite3.OperationalError (unable to open database file) 异常，导致只读审计与监控失败。
- **工程证据**: 强修复证据。修复提交 `816157fae98f02931dc1e35fa8448ec8ba23d242`，增加前置安全预检与无 sidecar 连接参数。
- **分类**: `contract` (Contract)

### 7. PR #103: `allinai0506_HAFlow_103-fb9ea2f3`
- **缺陷位置**: `herdr/agent_router.py:645-665`
- **缺陷原因**: 金丝雀路由决策在临界区内生成后，未在事务临界区内直接原子持久化，而是逃逸到临界区外异步写入。
- **触发路径**: 控制器在路由决策生成后、持久化完成前发生崩溃或重启。
- **后果**: 路由决策丢失，任务重启后被重新分派到非金丝雀节点，破坏分流契约。
- **工程证据**: 强修复证据。修复提交 `7ebce3da1e9389204bc49386d7fa0218fa4112e4`，将持久化收敛至临界区内保证强原子性。
- **分类**: `workflow_state` (Workflow / State)

### 8. PR #100: `allinai0506_HAFlow_100-e76d92cb`
- **缺陷位置**: `herdr/execution_outcome.py:224-237`
- **缺陷原因**: 任务执行结果文件命名格式使用单下划线分隔符 `outcome_{task_id}_{run_id}`，但 task_id 本身允许包含下划线。
- **触发路径**: task_id 包含下划线（如 `task_step_1`）。
- **后果**: 分割解析时发生歧义冲突，导致反序列化时将 task_id 与 run_id 提取错位，找不到对应运行结果。
- **工程证据**: 强修复证据。修复提交 `6f0821d3f44482b81923ec08d17ca022137aa59e`，将分隔符重构为无冲突的双冒号标识并使用结构化元数据头。
- **分类**: `identity` (Identity)

### 9. PR #106: `allinai0506_HAFlow_106-b747fbd4`
- **缺陷位置**: `herdr/rollout_policy.py:387-423`
- **缺陷原因**: 安全熔断评估函数在遭遇底层异常（例如网络超时或状态读取异常）时返回 `triggered=False`。
- **触发路径**: 监控或状态子系统短时异常。
- **后果**: 策略在发生故障时未 Fail-Closed 熔断，反而放行了不安全的高风险灰度放量。
- **工程证据**: 强修复证据。修复提交 `09fa5d37452d3a9484930129bcad0938b2512a88`，将异常默认安全语义重构为 Fail-Closed (安全阻断)。
- **分类**: `workflow_state` (Workflow / State)

### 10. PR #85: `allinai0506_HAFlow_85-6f76cd6f`
- **缺陷位置**: `herdr/fix_loop.py:101-126`
- **缺陷原因**: `redelivery_handled` 在判断修复任务是否已被有效承接时，仅检查是否存在 `updated_at > first_seen_at` 的任务；对于已被作废（`status == "superseded"` 或包含 `"superseded_by"`）的历史陈旧任务，其时间戳变更依然会被误判为有效工作已承接。
- **触发路径**: 任务在重试前被标记为作废但发生了后置时间戳刷新。
- **后果**: 无效的作废任务使重试机制误以为已由有效 worker 承接，导致流水线永久挂起死锁。
- **工程证据**: 强修复证据。修复提交 `3be436239ae86990f4e29943a2693e1625e21fb9`，过滤作废任务并在 `tests/test_fix_loop_recovery.py` 中覆盖。
- **分类**: `workflow_state` (Workflow / State)
