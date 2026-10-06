# Workflow恢复契约 Implementation Plan

> 执行所有者：本轮/root，controller-free，按用户批准的五步顺序实施。working_tree上限使需要自动提交的full SDD不适用；独立Stage Reviewer按统一流程进行。每步先RED，再实现，再专项验证；不自动commit/push/merge/deploy。

**Goal:** 完整修复恢复发现、动作交付、集成、候选发布、业务验收之间的事故契约。
**Architecture:** 复用StateStore的SQLite事务、现有recovery operations与delivery receipts。纯判定保留在herdr；CLI与Controller只装配外部动作，不增加事实源。
**Tech Stack:** Python标准库、SQLite、Git、现有pytest。

## Global Constraints
历史blocked与Run不改写；缺身份unknown；受管副作用前验证；不得启动测试真实Agent或收费模型；不得写生产DB；源码WIP/跨Agent隔离/累计预算不得绕过。新候选以完整SHA和独立门禁验收。

### Task 1：权威快照、严格代次与迁移
Files: herdr/recovery_store.py, workflow_progress.py, state_store.py, projects.py, kernel.py, controller_actions.py, services/herdr-controller.py；NEW herdr/workflow_repair_migration.py, tests/test_workflow_repair_contracts.py；bin/herdr-task迁移入口。
接口：read_snapshot(db_path, workflow_id)返回同事务(workflow, config, tasks)；migration_plan为可审核的现有事件证据与CAS指纹，apply/rollback只使用既有事务。
- [ ] RED：missing_workflow_generation_never_plans_effect；frozen_candidate_is_identical_for_all_readers；legacy_generation_migration_requires_matching_initial_receipts。
- [ ] 运行pytest -q tests/test_workflow_repair_contracts.py并记录目标断言失败；不可用字段不得伪造。
- [ ] 实现共享snapshot、注册代次、严格前置校验、可逆迁移；新字段与公开CLI先--help和临时DB验证。
- [ ] GREEN及并发CAS、无生产写入、旧代次冲突、迁移前进/回滚验证。

### Task 2：绑定已有后继和本轮回执
Files: herdr/workflow_recovery.py, recovery_store.py, recovery_successor.py, workflow_progress.py, services/herdr-controller.py；已有恢复专项。
接口：已有repair_map/source_runs/target_runs/request保持原结构；existing delivery消费生成awaiting_result，不再创建rework副作用。
- [ ] RED：delivered_rework_is_verified_without_resend；existing_successor_waits_without_rework；rework_is_inflight_not_new_gate_failure。
- [ ] 完整真实CLI/SQLite回执路径测试，外部prompt仅受控替換。
- [ ] 实现验证身份、范围与实际initial/rework receipt后绑定；遗漏目标保留人工核验。
- [ ] GREEN；发送后崩溃/回执后崩溃/两个连接争用等反例。

### Task 3：任务专属集成与最终SHA
Files: bin/herdr-task, services/herdr-worker.py, herdr/git_coordination.py；已有integration/pinned-onto测试。
- [ ] RED：集成source checkout同名借用分支时失败的真实Git测试；rebase变SHA后task.commit与integration ref必须一致。
- [ ] 实现任务ref与独立integration branch，无源branch -f；新的Task从onto基线创建自身分支。
- [ ] 真实CLI→Git→SQLite验证源HEAD保持、最终SHA、fetch/receipt崩溃窗口与幂等重试。

### Task 4：明确候选发布、当前失败与升级/latch
Files: herdr/scheduler_facts.py, direct_dispatch.py, workflow_progress.py, services/herdr-controller.py；候选与latch测试。
- [ ] RED：旧任务cleanup不改变candidate；迟到候选发布CAS拒绝；superseded blocked不进入新prompt；旧integrated升级不永久阻塞新成功。
- [ ] 以既有候选episode读取，集成回执或明确CLI发布新episode；过滤operation facts而非全历史重扫。
- [ ] 成功集成受管清升级，latch维护先于阻断return，业务派发仍受门禁。
- [ ] GREEN；A→B→A显式发布、不同gate完成顺序、#152/#153真实入口组合。

### Task 5：验收证据、预算与独立gate
Files: herdr/evaluator.py, checkpoints.py, completion.py, node_capacity.py, bin/herdr-task；业务验收/预算测试。
- [ ] RED：frontend_green_does_not_claim_java_acceptance；checkpoint_report_visible_without_tracked_diff；test_budget_exhaustion_is_pre_effect；budget_extension_CAS_is_bounded。
- [ ] 通用metrics声明仅所选命令；gate结果绑定现有checkpoint和Run/epoch/SHA。增加显式有界预算入口与预检，保持历史计数。
- [ ] 新SHA在临时工作流通过独立test/review验收，再允许业务恢复结案，wrapup不混入业务gate。

## Checkpoints与退出
- [ ] S3：计划独立审查，无未决正确性假设。
- [ ] S4：五步完成，ModeB删除优先清理；不做无关重构。
- [ ] S5：最新专项/全量pytest、compileall、CLI AST/--help、diff-check；关键修复撤销重新RED；迁移shadow对照。
- [ ] S6：独立Standards+Spec+Google正确性评审，最多三轮NEEDS_FIXES；源码变化后重新验证。
- [ ] S8：相关Wiki与通用教训同步，旧历史只追加。
- [ ] S7：本地可审核修改与具体生产迁移/激活清单；超出working_tree的动作不自动执行。

## S3 Round 1 修订
已按独立reviewer四项意见补齐：read_snapshot不读外部配置；迁移旧文件有SHA+DB CAS；代次接受集合与当前epoch回执明确；迁移事务保留并改绑started operation身份，禁止新effects副本；消费后继续幂等gate失效，不错误直接等待；rollback进度CAS。规格末节为具体契约。
新增RED集合：迁移started operation后verify不重发；仅旧epoch拒绝；多代次拒绝；外部配置在apply前改变拒绝；迁移后任务进展阻止rollback；gate失效三崩溃点恢复。
