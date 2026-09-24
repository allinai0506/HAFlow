# HAFlow Context Compiler V1 设计

> 状态：已获用户确认，进入实现计划阶段
> 基线：`437b33504804f0e210c2caa39e33e2c12f4a13ba`
> 范围：确定性、状态感知、角色感知、可追溯的 WorkingContext 投影

## 1. 目标与边界

WorkingContext 是一次 Agent 执行边界看到的最小可信视图。它从现有 Task/Workflow 状态、Trajectory、Observation、Finding、CollaborationEvent、Eval 事实中投影，不成为新的事实源，也不修改任何源记录。

V1 必须满足：

- 同一个 Task 对不同角色生成不同的信息视图。
- Task/Workflow 状态、当前节点、依赖、角色和来源事实共同影响选择。
- 每个内容项都有 `source_ref`；没有来源的模型总结不会进入快照。
- 旧 Finding 被明确 supersede 后，当前快照只保留有效版本，历史记录仍留在原表。
- 严格禁止跨 Run；同一 Workflow 执行范围可以读取其依赖任务的已验证事实。
- 快照不可变；输入变化产生新的 `context_id`。
- 相同语义输入重复编译可复用快照；指纹覆盖所有影响输出的稳定输入。
- 预算在候选筛选后、持久化前执行；不读取完整历史或 Observation 正文。
- 不实现 Vector DB、Embedding、RAG、LLM summarizer、长期记忆、跨 Run 检索、Graph DB、UI 或新 Agent Runtime。

## 2. 权威来源与调用链

| 语义 | 权威来源 | WorkingContext 用法 |
| --- | --- | --- |
| Task 当前状态/目标/阻塞 | `state_db.tasks` + `StateStore` | 读取当前快照，生成 `current_state`、goal、blocker |
| Workflow/DAG 状态 | `state_db.workflows` + `herdr.workflow` | 读取 status、current_stage、node `depends_on`、purpose/rules |
| RuntimeState | Task payload 中的 `runtime` | 只作为当前运行事实；不把 pane/tab 当作业务身份 |
| 历史事实 | `TrajectoryLedger` / `events(source=trajectory)` | 只读取相关、有界事件；不复制完整 transcript |
| 证据 | `ObservationStore` / `observations` | 只携带 `observation_id/source_ref/source_type/sha256/excerpt` 等元数据 |
| 分析 | `TrajectoryFinding` / `trajectory_findings` | 携带 finding 摘要、状态、严重性和 evidence refs；不当作机器事实 |
| 协作 | `CollaborationEvent` / `collaboration_events` | Handoff 只携带 `context_id`、summary、artifact/evidence refs |
| Eval | `eval_results` | 仅携带最新明确 verification 字段及其来源，不复制评估全文 |
| 历史工作记忆 | `context_packs` | 不作为 WorkingContext source，不复制旧 ContextPack |

真实执行链：

```text
Task launch / retry / handoff / review request / verification request
  -> 读取同一 SQLite 一致性快照
  -> Context Compiler 选择与投影
  -> working_contexts 不可变写入
  -> prompt / CollaborationEvent 只携带 context_id
  -> Agent 按 context_id 读取快照
```

## 3. 数据模型

新增 `herdr/context_compiler.py`，提供 `WorkingContext` 与 `ContextItem`。

### 3.1 WorkingContext 字段

```json
{
  "context_id": "wc_<uuid>",
  "run_scope": "workflow execution scope",
  "run_id": "current task run id",
  "workflow_id": "wf-1",
  "task_id": "task-1",
  "node_id": "implementation",
  "agent_role": "developer",
  "goal": "bounded task goal",
  "goal_source_ref": "task:task-1",
  "current_state": {
    "task_status": "working",
    "workflow_status": "running",
    "current_node": "implementation"
  },
  "current_state_refs": {
    "task_status": "task:task-1",
    "workflow_status": "workflow:wf-1",
    "current_node": "task:task-1"
  },
  "completed": [],
  "artifacts": [],
  "evidence": [],
  "findings": [],
  "decisions": [],
  "blockers": [],
  "open_questions": [],
  "verification": [],
  "handoffs": [],
  "next_action": "deterministic policy action",
  "next_action_source_ref": "policy:context_compiler_v1:developer",
  "source_refs": [],
  "context_fingerprint": "sha256",
  "source_version": "sha256",
  "metrics": {},
  "compiled_at": 0.0
}
```

### 3.2 ContextItem 字段

每个列表项统一为：

```json
{
  "kind": "finding",
  "value": "small bounded fact or reference metadata",
  "source_ref": "finding:fnd_123",
  "source_task": "task-1",
  "source_run": "run-1",
  "created_at": 1710000000.0,
  "evidence_refs": ["observation:obs_123"]
}
```

`source_ref` 允许的 V1 前缀：`task:`、`workflow:`、`trajectory:`、`finding:`、`observation:`、`collaboration:`、`eval:`、`policy:`。编译器拒绝没有 `source_ref` 的内容项。

## 4. 范围与身份

- `run_scope` 使用现有 `collab_scope_for_task`：`workflow_run_id` > `execution_id` > `workflow_id`。
- `run_id` 保存当前 Task 的单次执行身份；它不是跨任务共享范围。
- 编译时先读取目标 Task，再解析其 workflow execution scope。
- 只允许同一 scope 下的任务事实；每个来源的 `run_id` 必须属于该 scope 的任务集合。
- Finding/Observation 若带 `task_id`，必须能对应到 scope 内任务；若不带 task，则其 `run_id` 必须属于 scope。
- CollaborationEvent 必须同时满足 `run_id == run_scope`、`workflow_id == workflow_id`，且 from/to Task 在 scope 内。
- 信息不足时丢弃受影响来源或 fail closed，不猜测、不跨 Run 拼接。

## 5. 选择规则

### 5.1 通用字段

始终选择：goal、current state、当前 node、Task status、Workflow status、直接依赖状态、最新 incoming handoff、当前 blocker、明确 open question、当前 next action。

### 5.2 角色字段

固定角色及别名归一化：

- `developer`：`dev`、`implementer`、`implementation`、`engineer`
- `reviewer`：`review`、`code_reviewer`
- `tester`：`test`、`qa`、`verifier`
- `coordinator`：`coord`、`orchestrator`

角色规则：

- Developer：需求/验收、当前实现 artifact、依赖完成项、实现相关 Finding、阻塞和待办验证。
- Reviewer：变更 artifact 范围、实现阶段 Finding、测试/验证 evidence、实现风险和 review scope。
- Tester：acceptance criteria、verification targets、artifact refs、已知失败、最近 verification evidence、未验证声明。
- Coordinator：整体 Workflow 状态、依赖图摘要、blockers、open questions、失败 handoff 和待调度节点。

未知角色抛出 `ValueError`，不静默降级为其他角色。

### 5.3 纯函数 relevance

公开 `context_relevance(...)`，按以下因素生成确定性分数：

1. state relevance（当前状态/当前节点/失败状态）；
2. role relevance（字段和来源对角色的用途）；
3. dependency relevance（直接依赖和当前节点）；
4. validity（scope、存在性、未 supersede）；
5. recency（仅作为最后 tie-breaker）。

排序键为 `(validity, state_relevance, role_relevance, dependency_relevance, recency, source_ref)`，不采用单纯 `created_at DESC LIMIT N`。

## 6. Supersession

V1 支持两种确定性关系：

1. 同一 `finding_key` 的更新：沿用现有 Finding 身份，数据库只保留当前版本；历史分析由原存储契约负责。
2. 显式关系：Finding `metadata.supersedes` 或 `metadata.superseded_by` 指向同一 scope 内的 Finding ID。编译器构建 supersession 图，选择没有有效后继且未被标记 `superseded_by` 的最新 Finding。

若引用不存在、跨 scope 或形成环，来源被标记无效并不进入当前上下文。历史 Finding 不删除。

## 7. Provenance、Evidence 与安全

- Finding/事件/Observation 只投影小型字段；不调用 `ObservationStore.read()`。
- 文本先复用 Observation 的递归脱敏，再做长度/数量裁剪。
- `verification.passed` 只有严格布尔值才可出现；缺失不推断为失败或成功。
- Finding 的 `evidence` 仅保留合法引用并解析到 Observation/Event；引用存在不升级为业务结论。
- 旧 verification 不覆盖同来源较新的失败 verification。
- `context_fingerprint` 不包含随机 `context_id` 或 `compiled_at`，只包含影响选择结果的规范输入、source ref/version、角色和预算配置。

## 8. 预算与排序

默认配置：

```python
{
    "max_items": 40,
    "max_chars": 12000,
    "max_items_per_kind": {
        "completed": 8,
        "artifacts": 10,
        "evidence": 10,
        "findings": 10,
        "decisions": 5,
        "blockers": 5,
        "open_questions": 5,
        "verification": 10,
        "handoffs": 3,
    },
}
```

选择优先级：

```text
BLOCKER > OPEN QUESTION > CURRENT GOAL > VERIFICATION
       > FINDING > ARTIFACT > HANDOFF > HISTORY
```

超预算时先删除低优先级候选，再缩短单行文本，最后保留身份、当前状态和最新验证；不得静默丢失 goal/current state/next action。序列化结果必须不超过 `max_chars`。

## 9. 持久化与不可变性

在 `state_db` 增加 `working_contexts`：

- `context_id` 主键；
- `run_scope`、`run_id`、`workflow_id`、`task_id`、`node_id`、`agent_role`；
- `context_fingerprint`、`source_version`；
- `payload_json`、`metrics_json`、`compiled_at`；
- 按 task/role/compiled_at 和 task/compiled_at 建索引；
- 不设置指向 workflow/task 的外键，保留审计快照。

写入规则：

- 相同 task/role 的最新 fingerprint 相同则返回已有 canonical snapshot；
- fingerprint 改变则追加新 snapshot；A→B→A 也允许生成新的不可变 snapshot；
- `BEGIN IMMEDIATE` 下检查最新行，竞争请求返回数据库 canonical row；
- 不提供 update/delete API；
- `get_working_context(context_id)`、`get_latest_working_context(task_id)`、`list_working_contexts(task_id)` 为公开读取入口；
- 保存前完成身份、引用、脱敏和预算检查。

## 10. Fingerprint、Diff 与指标

### 10.1 Fingerprint

`context_fingerprint = sha256(canonical_json({role, scope, selected source refs/versions, selected normalized values, budget policy}))`。

### 10.2 Diff

`diff_working_context(old, new)` 返回：

```json
{
  "added": [],
  "removed": [],
  "superseded": [],
  "changed": []
}
```

- 以 `(kind, source_ref)` 作为项身份；
- `superseded` 只由显式 supersession 关系产生；
- `changed` 只报告同一 source_ref 下规范 value 变化；
- 不生成自然语言总结。

### 10.3 指标

每个快照记录：

- `raw_candidate_items`
- `selected_items`
- `context_chars`
- `compile_latency_ms`
- `context_reuse`
- `context_changed`

不记录质量评分。Context Compiler 的指标读取保持无副作用；现有 Harness metrics 可在后续同一 PR 中增加聚合计数，但不改变 Task 状态。

## 11. Collaboration 与执行边界

- `CollaborationEvent.context_refs` 继续只保存引用；Handoff 的 `context_id` 不展开到事件 payload。
- `build_handoff_prompt` 增加 `WORKING_CONTEXT_REF` 行和加载提示，不嵌入完整 WorkingContext。
- dispatch 前校验 context ref 存在、目标 Task 和 run scope 匹配；旧事件没有 ref 时保持兼容。
- Task launch、retry（复用 dispatch）、handoff、verification dispatch 触发编译。
- review request 由现有 handoff/Controller 路径携带目标 Agent 的 context ref。
- Context 编译失败不得改变 Task/Workflow 状态；dispatch 记录 warning 并按既有路径继续，除非显式 ref 校验失败。

## 12. 验收矩阵

- A：同一 Task 的 developer/reviewer 快照字段和候选不同。
- B：implementation→review 状态/节点/依赖变化导致 fingerprint 与内容变化。
- C：显式 supersession 只保留有效 Finding，历史仍可查。
- D：Finding evidence_ref 保留并能回指 Observation/Event。
- E：Run A 的 Task/Finding/Observation/Event 不进入 Run B。
- F：100+ Finding 仍满足每类数量和总字符预算。
- G：不含完整 trajectory、terminal transcript、chat history、CoT。
- H：V1 保存后源数据改变，V1 不变；再次编译生成 V2。
- I：V1→V2 准确返回 added/removed/superseded/changed。
- J：Handoff→CollaborationEvent→目标 Agent 可由 context_id 读取快照。
- 额外：source_ref 缺失、跨 scope、未知角色、存储竞争、编译失败均有确定行为。

## 13. 不做事项与已知限制

- V1 不从 ContextPack 继承语义文本，避免两个投影层形成事实源。
- V1 不理解自由文本 supersession；没有显式关系时只使用现有 Finding identity。
- Artifact 原文、Observation 内容和终端 transcript 不进入快照。
- 没有 `workflow_run_id` 的旧数据按现有协作兼容规则以 `workflow_id` 作为 scope；不提供跨 workflow experience retrieval。
- WorkingContext 编译是执行边界快照，不承诺实时自动重写。
