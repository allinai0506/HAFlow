# ADR-0006：门禁对象拆分为门禁规格与门禁裁决及权威源确立

## 背景

在 HAFlow 候选本体 v0.1（`ontology/objects/gate.yaml`）中，门禁被合并为单一的 `gate` 对象，主键被赋予节点级标识 `workflow_id:node_id`。然而在 brownfield 实际代码考古中发现，门禁在规范层与执行层存在根本性的身份断层与权威冲突（语义冲突 SC-012，详见 `decisions/0003-semantic-conflicts.md`）：

1. **规格与事实混淆**：门禁在工作流模板中是节点级规范（`GateSpec`，定义 command、retry_node、max_loops 等）；而在运行执行时，每次任务执行 Attempt 都会产生独立的裁决结论（`tasks.stage_verdict`），同一个节点经过 fix-loop 重试会派生出多个不同的任务实例及各自的 verdict。若将二者合并并以节点标识，会导致重试历史覆盖、任务裁决无法精准溯源。
2. **双重权威冲突**：系统同时存在数据库任务行 `tasks.stage_verdict` 与磁盘文件 `~/.herdr-controller/gate-verdicts/<task_id>.json`。控制器推进、CLI 查询与 Watchdog 校验在不同场景下读取不同路径，缺乏排他性权威裁决。

## Evidence

- `docs/product-specs/workflow-template-schema.md` L89-96：`GateSpec` 在模板中定义在节点层（`type`, `command`, `retry_node`, `max_loops`）。
- `services/herdr-controller.py` L938-973：`GATE_DEFAULTS` 与 `resolve_gate_config` 解析节点级门禁规则。
- `bin/herdr-task` L877-1019 (`set_status`)：校验并向任务行写入 `stage_verdict`。
- `herdr/direct_dispatch.py` L135-158：`HERDR_GATE_VERDICT` 协议与按 `task_id` 生成文件。
- `herdr/kernel.py` L832-957 (`bypass_task_gate`)：同时修改 `tasks.stage_verdict` 并落盘 `gate_verdict_path(task_id)`。

## 决策

1. **实体解耦为两个独立业务对象（Decomposition into Two Objects）**：
   - **`gate_spec` (门禁规格 / 节点契约规范)**：
     - 主键：`gate_spec_id = {workflow_id}:{node_id}`
     - 业务含义：工作流节点上的准入与准出质量/安全契约。
     - 关键属性：`node_id`、`gate_type` (auto/human/hybrid)、`command`、`retry_node`、`max_loops`、`auto_criteria`、`requires_human_approval`。
     - 关系：`node` has_one `gate_spec`。
   - **`gate_verdict` (门禁裁决 / 任务评测事实)**：
     - 主键：`task_id`（与特定任务执行 Attempt 强绑定）。
     - 业务含义：针对特定任务执行交付物所产生的权威评测结论。
     - 关键属性：`task_id`、`workflow_id`、`node_id`、`verdict` (`pass` / `blocked`)、`verdict_note`、`evaluator`、`evaluated_at`、`evidence_snapshot_hash`。
     - 关系：`task` produces `gate_verdict`；`gate_verdict` evaluates `gate_spec`。

2. **确立单一调度权威源与不可变证据快照（Single Source of Truth & Evidence Snapshot）**：
   - **调度权威源（Scheduling Authority）**：**数据库 `tasks.stage_verdict`（及随事务落盘的 `WorkflowEvent`）为全系统唯一调度事实源**。控制器推进 DAG、判断节点是否通过，必须且只能以此列为准。
   - **证据佐证源（Immutable Evidence Snapshot）**：磁盘文件 `~/.herdr-controller/gate-verdicts/<task_id>.json` 是评测过程细节、测试堆栈、Finding 详情的只读工件，必须在数据库 `tasks.payload_json` 中记录其 SHA256 校验和。
   - **单向断言防篡改**：外部文件只能作为被引用的凭证，不能反向作为调度事实；若文件损坏或哈希不匹配，系统判定证据失效并 fail-closed，严禁无事务地反向覆盖数据库。

## 被拒绝方案

- **方案 1：维持单一 `gate` 对象**。
  - 被拒绝理由：无法表达 fix-loop 场景下一个门禁节点历经多次打回产生的多份历史裁决，破坏了审计与可追溯性。
- **方案 2：以文件系统 `gate-verdicts/<task_id>.json` 为第一事实源**。
  - 被拒绝理由：文件系统缺乏 ACID 事务支持，无法与任务状态迁移原子提交，在高并发或工位崩溃时容易造成状态与文件脱节。

## 影响

- 候选本体中拆分 `ontology/objects/gate.yaml` 为 `ontology/objects/gate_spec.yaml` 与 `ontology/objects/gate_verdict.yaml`。
- 关联动作 `record_gate_verdict` 与 `bypass_gate` 的 target 确立为 `gate_verdict`，输入引用 `gate_spec`。
- 修复 fix-loop 历史追溯中门禁结论相互覆盖的语义缺陷。

## 兼容性

- 当前本体处于 experimental 阶段，平滑拆分不涉及线上停机或破坏性数据迁移。
