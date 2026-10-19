# OEP-0002：D1–D3 架构决议收敛与本体演化落地

状态：proposed (基于 ADR-0005、ADR-0006、ADR-0007 定案实施)
类型：演化升级 (Object 拆分与特权流转契约落地，全部 experimental)
范围：`ontology/` 目录演化，不触碰生产运行时代码
关联决策：`decisions/0005-d1-out-of-matrix-writers.md` (D1), `decisions/0006-d2-gate-object-decomposition.md` (D2), `decisions/0007-d3-workflow-initial-state.md` (D3)

## 1. Observation

在 OEP-0001 候选本体首轮评审（`OEP-0001-review-r1.md`）与语义冲突登记（`0003-semantic-conflicts.md`）中，标识了三项长期悬而未决的架构与模型冲突：
1. **SC-018 (D1)**：任务生命周期状态机外存在 8 类越权写入与裸写，缺乏统一事务 CAS 网关与权威审计事件；
2. **SC-012 (D2)**：门禁被强行合并为单一 `gate` 对象，混淆了节点级契约规范（`GateSpec`）与任务级执行裁决事实（`tasks.stage_verdict`），导致 fix-loop 重试历史覆盖及数据库与文件双权威冲突；
3. **SC-011 (D3)**：工作流状态机声明以 `pending` 为起点，但工程实际注册直写 `running`、仅占位记录写 `pending`，导致工作流起点定义撕裂。

在 ADR-0005、ADR-0006 与 ADR-0007 明确裁决方案后，本提案负责将决议落地进 `ontology/` 正式资产体系。

## 2. Evidence

- **D1 证据**：
  - `herdr/transitions.py` L19-35：`TASK_TRANSITIONS` 单向矩阵；
  - `herdr/state_db.py` L6734-6738 & `herdr/delivery_rework.py` L102-108：completed→rework 越权修复流；
  - `herdr/kernel.py` L832-957：`bypass_task_gate` 直调 `save_task`；
  - `herdr/kernel.py` L526-575：`rollback_workflow` 的 `force=True`；
  - `console/task_api.py` L266-290：控制台 force-review 裸写 `status`。
- **D2 证据**：
  - `docs/product-specs/workflow-template-schema.md` L89-96：节点级 `GateSpec`；
  - `services/herdr-controller.py` L938-973：`GATE_DEFAULTS` / `resolve_gate_config`；
  - `bin/herdr-task` L877-1019：`set_status` 写入任务行 `stage_verdict`；
  - `herdr/direct_dispatch.py` L135-158：`HERDR_GATE_VERDICT` 协议与文件路径 `gate_verdict_path(task_id)`。
- **D3 证据**：
  - `herdr/transitions.py` L89-97：`pending -> running/in_progress/...`；
  - `herdr/projects.py` L578-603：`register_workflow` 直写 `status="running"`；
  - `herdr/state_db.py` L1617：占位工作流写入 `status="pending"`。

## 3. Current Ontology Gap

- `objects/gate.yaml` 为单一合并对象，无法表达多代任务的门禁裁决历史；
- `states/task_lifecycle.yaml` 仅以注解形式记录 `out_of_matrix_writers`，缺乏正式的特权流转契约；
- `states/workflow_lifecycle.yaml` 虽观测 initial 为 running，但缺少注册动作（`register_workflow`）建模；
- 门禁关系与映射仍指向单一 `gate`，未确立数据库调度权威与不可变证据快照的主从约束。

## 4. Proposed Diff

1. **对象解耦（Objects）**：
   - 拆分新增 `objects/gate_spec.yaml`：主键 `workflow_id:node_id`，表达节点级准入/准出契约规范；
   - 拆分新增 `objects/gate_verdict.yaml`：主键 `task_id`，表达任务执行裁决事实，明确单一权威源为 DB `tasks.stage_verdict`，并由 DB 强引用证据快照哈希；
   - 废弃标记 `objects/gate.yaml`：标记为 `deprecated`，指向解耦后的双对象。
2. **关系重构（Relations）**：
   - 更新 `relations/node_specifies_gate.yaml`：目标指向 `gate_spec`；
   - 新增 `relations/task_produces_gate_verdict.yaml`：任务生成门禁裁决事实；
   - 新增 `relations/gate_verdict_evaluates_gate_spec.yaml`：裁决结论核验契约规范。
3. **状态契约升级（States）**：
   - `states/task_lifecycle.yaml`：正式引入 `privileged_transitions` 契约，声明 `delivery-repair`、`operator-bypass`、`rollback`、`console-force-review` 4 类受管特权流转的 intent、授权 policy 与审计要求；
   - `states/gate_verdict_lifecycle.yaml`：归属对象绑定至 `gate_verdict`；
   - `states/workflow_lifecycle.yaml`：固化 `initial: running`，将 `pending` 保留为占位/草稿态。
4. **动作与策略演化（Actions & Policies）**：
   - 新增 `actions/register_workflow.yaml`：建模工作流创建动作（Effects: status=running, Event: workflow_created）；
   - 更新 `actions/record_gate_verdict.yaml`、`bypass_gate.yaml`、`force_pass_gate.yaml`：target 统一绑定为 `gate_verdict`；
   - 更新 `policies/operator_cli_policy.yaml` 与 `policies/controller_service_policy.yaml`：授予 `register_workflow` 动作。
5. **事实映射对齐（Mappings）**：
   - 更新 `mappings/gate_verdict_file_mapping.yaml`：归属对象为 `gate_verdict`，确立权威说明（DB 任务行为调度唯一权威，文件为不可变快照）。

## 5. Breaking Change

- [x] No
- 说明：全量模型仍处于 `experimental` 候选阶段，无线上运行时读取 `ontology/`；对象拆分与字段规范化属于演化治理升级。

## 6. Impacted Consumers

- 待接线的上下文投影：`coordinator_workflow_projection`、`task_executor_projection`、`console_operator_projection`；
- 后续代码收敛工程：内核消除 `save_task` 裸写，以及控制台 force-review 改造为网关调用。

## 7. Golden Tests

- 黄金用例解析：`gate_blocked_requires_note`、`gate_bypass_audited` 引用对象对齐到 `gate_verdict` 与 `gate_spec`；
- 机械审计脚本 `ontology/tests/audit_ontology.py` 执行 100% 通过（MAJOR=0, MINOR=0）。

## 8. Migration / Compatibility

- 语义无缝衔接：保留 `gate.yaml` 的别名与废弃重定向，确保任何历史引用不产生死链；
- 数据库 schema 与 JSON 投影保持零变动。

## 9. Human Decision

- Status: approved (基于 ADR-0005、ADR-0006、ADR-0007 定案实施)
- Reviewer: User / Independent Architecture Reviewer
- Decision: 批准落地并纳入本体仓库。
