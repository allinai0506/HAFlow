# Workflow Progress Recovery Implementation Plan

> For agentic workers: 单一 root 主控，controller-free 路由，独立文件 worker 与 reviewer；working_tree 不提交，不执行完整 SDD 的 commit 循环。

Goal: blocked 与 finalize escalation 有持久恢复义务，独立于正向 DAG。
Architecture: workflow_progress 纯评估；recovery_store 在原 SQLite 内事务义务、CAS、租约与人工裁决；workflow_recovery 编排，实际 Controller 调既有修复原语。
Tech Stack: Python 标准库、SQLite、现有 StateStore、pytest。无新增依赖。
Global constraints: docs/product-specs/workflow-progress-recovery-spec.md 九项不变量；测试临时存储，不启动真实 Agent、模型、不写生产。root 独占已有文件。

## Task 1 统一评估
Files: NEW herdr/workflow_progress.py、NEW tests/test_workflow_progress.py。
Interfaces: active_tasks(workflow,tasks)->list；assess_workflow(workflow,config,tasks)->dict(blockers,obligations,can_advance)；recovery_identity(workflow,facts)->str。
- [x] RED `assert assess_workflow(wf,cfg,[impl_committed,test_cleaned_blocked])['obligations']`。pytest -q tests/test_workflow_progress.py。
- [x] 实现活跃过滤、门禁 retry_node 合并、candidate/run/generation 稳定身份；未知转 waiting_human。foreign/superseded、A→B、无 review、escalated、cleaned blocked 回归。

## Task 2 同库持久恢复
Files: NEW herdr/recovery_store.py、NEW tests/test_recovery_store.py；root 修改 state_db 初始化与写入钩子。
Interfaces: ensure_obligations(conn,workflow,config,tasks,now=None)->list；list_operations(db_path,workflow_id)->list；claim_operation(db_path,id,owner,now,lease_seconds=60)->dict|None；finish_operation(db_path,id,owner,status,detail,now)->dict；decide_operation(db_path,id,expected_version,operator,action,reason,now,until=None)->dict。
- [x] RED 独立进程 Barrier 竞争唯一建义务/单 claim；原事务异常 `raise RuntimeError('injected before commit')` 后任务与义务都回滚。pytest -q tests/test_recovery_store.py。
- [x] schema 收敛原数据库初始化，ensure 使用 caller conn、不嵌套事务；unique identity、canonical events、version/owner CAS。租约过期 unknown 转人工而非重发，read-only status 不迁移。
- [x] 人工 stale version/candidate 与 expired hold 回归；候选换代作废旧义务；不可把日志时间当进展。

## Task 3 真实恢复入口
Files: NEW herdr/workflow_recovery.py、NEW tests/test_workflow_recovery.py、NEW tests/test_recovery_entrypoints.py；root 修改 services/herdr-controller.py、bin/herdr-task、console/herdr_factory_console.py、herdr/controller_actions.py、herdr/kernel.py。
Interfaces: reconcile_workflow(store,workflow,config,tasks,now=None)->list；Controller claim 前重查 assessment，执行后确认事实或 waiting_human；CLI recovery-status/recovery-decide 使用 expected version/operator/reason。
- [x] RED Controller 原卡点快照必须 `assert recovery_store.list_operations(store.db_path,'wf')`，不等 review-ready。无 coordinator 有 waiting_human；中断无盲目重复。
- [x] committed repair 保留原候选，正式 successor 注册后才作废旧 gate；不强制 rework。复用既有 launch intent 与谱系，失败/未知仍持久义务。
- [x] Console 展示义务与统一 blocker；manual step 拒绝同一事实。真实 CLI→核心→SQLite→读链，外部传输替换。
- [x] pytest -q 新增专项、selective replan、reverification、Controller actions、Console、transition 相邻测试。

## Task 4 收口
- [x] ai-slop-cleaner Mode B 删除重复判断、无用包装与 import，保留安全边界。
- [x] 清理后 pytest -q、python3 -m compileall -q herdr services bin tests、无扩展 bin/herdr-task py_compile、git diff --check；.omc/verify-1005fixbug.md 记录真实 exit/count。
- [x] 独立审查最新 spec/diff/evidence；修复后重测复审；.omc/review-1005fixbug.md 不伪造 MERGE_READY。
- [x] 知识归档 docs/lessons/lessons-learned.md、wiki/index.md、wiki/log.md；决策 TSV 独立审查。仅交付本地 diff。

## Rejected alternatives
只补 ready-node handle_fix_loop 无法解决 escalation/队列丢失；改 pass 破坏验收；新数据库破坏事实事务；超时自动重发可能重复副作用；committed 强制返工破坏候选身份。

## Plan review resolution 2026-10-05
Review by gpt-6-astra identified launch registration before delivery and committed replacement rejection. Root resolves before implementation:
- Execution slot is workflow generation + candidate SHA + retry root + kind. Immutable gate facts/task runs/affected lineage are versioned payload; later sibling gate merges into same slot, cannot concurrently dispatch second repair.
- Successor uses distinct branch based on verified predecessor SHA. No onto predecessor owned branch, no integration prerequisite.
- New kernel gateway validates predecessor version/SHA, same workflow/node, successor run/launch identity and confirmed delivery then atomically writes predecessor superseded_by and successor supersedes. Predecessor remains committed. Do not bulk overwrite stale tasks after this narrow transaction.
- Persist steps before external effects: claimed→launch_intent→successor_registered→delivery_confirmed→lineage_linked→gates_invalidated→awaiting_new_candidate. Registration/notification alone never resolve. Unknown delivery becomes human/inventory check; failed successor remains obligation.
- Route automatic Controller fix-loop calls through persistent claim. Keep legacy callable only as executor primitive and for old compatibility tests. Recovery gate disposition happens only after rework/successor confirmed; no blind queue-as-completion.
- save_task owns BEGIN IMMEDIATE if it owns connection; caller-owned transaction never commits. All task writes, including metadata and initial saves, register obligations and canonical facts same transaction; failures propagate.
- finalize retry only after human resolves WIP or other explicit required precondition; operation revalidates candidate/version and clears escalation through existing kernel metadata gateway. Record resulting fact; uncorrected refusal returns waiting_human. No permanent suppression presented as success.
- Add real launch-path tests registration-before-dispatch crash and delivery-before-link crash; independent connection owner/version CAS; closed/paused never deliver.

## Human escalation and resumed scope

Three NEEDS_FIXES rounds were escalated per RULES. User explicitly replied “继续修复”; resumed repairs preserve original authorization: working_tree only. Full affected coverage mapping and nonblocking existing Console modal implemented. Subsequent SPEC counterexample demonstrated old same-Run rework request reuse; durable request ID, current completion epoch/path and actual delivery events now bind the operation. Independent resumed SPEC and Standards reviews both pass code/specialists, final full verification pending.

## 最终候选源码修复

真实 Worker 回归证明项目目录不含本地失败提交时 pin SHA 失败。补充 `choose_recovery_source`，选择包含精确候选的项目目录或登记的干净前驱 clone，仍保持独立后继 Git 和原项目集成目标。候选不可读、超时或 WIP 不产生派发副作用。全部实现与知识项已完成；最终全量及复审结果以 `.omc/verify-1005fixbug.md` 为准，未部署。
