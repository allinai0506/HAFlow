# Semantic Context Compact V1

Semantic Context Compact 是 HAFlow 的 Working Memory 层。它把 Facts、Evidence 和 Analysis 压缩成下一 Agent 可以直接接手的 ContextPack；它不是删除历史，也不是把完整日志重新塞回模型。

## 数据边界

- Trajectory Ledger 是 Facts，Context Compact 只读取 bounded 事件窗口。
- Observation 是 Evidence，Compact 只读取 observation metadata、excerpt 和 `observation_id`，不调用 `read_observation()`。
- Trajectory Observer 的 Finding 是 Analysis，Compact 只保留少量 finding 引用和摘要。
- `verified_facts` 由程序根据 verification event、Task 和 Runtime 生成；reducer 不能写入它。
- `completed`、`open_issues`、`next_focus` 属于语义分析，所有引用在落库前必须存在。

## 持久化与预算

`context_packs` 是 SQLite append-only 表，保留 `ctx_<uuid>`、run/task/workflow、goal、current_state、语义数组、引用、`source_event_sequence`、metadata 和 created_at。相同 run 的相同 sequence 重复 Compact 返回最新快照，不新增行；有新 sequence 时创建完整新快照，旧快照保留。

默认输入预算为 12,000 字符、最近 100 个事件、10 个 Finding、20 个 Observation metadata 和 20 个 Artifact ref。裁剪优先移除旧事件、低 severity Finding、Observation excerpt 和多余 Artifact ref；goal、current_state、最新 verification 和 critical Finding 优先保留。

## 入口与失败隔离

手动入口：

```bash
herdr-task compact --run-id <run_id>
herdr-task compact --run-id <run_id> --json --no-model
```

`--no-model` 仍会生成 goal、current_state、verified_facts、finding/evidence/artifact refs；语义数组可为空。`agent_done` Done Gateway 以 daemon best-effort 方式触发 Compact，provider、JSON、读取或存储失败都不能改变 Task、Workflow、Runtime、Coordinator 状态。

## Context Compiler V1（WorkingContext）

`herdr/context_compiler.py` 是 ContextPack 之后的执行边界投影，不是新的事实源。实现按职责拆为 `context_models.py`（值对象）、`context_sources.py`（有界 SQLite source snapshot）、`context_candidates.py`（候选构造）、`context_selection.py`（role relevance）、`context_projection.py`（预算/指纹）、`context_diff.py`（结构化 diff）；入口模块负责编排、存储 facade 和公共 API。它从当前 Task/Workflow 状态、同一 workflow execution scope 内的 Trajectory/Observation/Finding/Collaboration/Eval 记录，按 `developer`、`reviewer`、`tester`、`coordinator` 角色选择最小上下文。

- 每个内容项都有 `source_ref`；Observation 只投影 metadata/excerpt，不读取正文。
- Finding 的 `metadata.supersedes` / `superseded_by` 参与当前版本选择；历史 Finding 保留在 `trajectory_findings`。
- `working_contexts` 是不可变快照表；`working_context_source_heads` 为每个 `(run_scope, workflow_id)` 维护单调 source revision，`working_context_source_clock(run_scope, workflow_id, revision)` 由源表触发器按 execution scope 维护，最新相同 fingerprint 复用旧 `context_id`，事实变化或 A→B→A 追加新快照；迟到旧 revision 不能成为 latest，同一 scope 的保存竞态会重试，其他 Workflow 的写入不会误触发 stale；dispatch 重新读取权威 Task 身份，task-bound legacy source 缺 workflow 时仍必须由 Task/run 证明 scope；critical Finding、超限 source 与目标 incoming Handoff 使用显式保留/截断标记。
- `context_fingerprint` 覆盖编译器版本、角色、范围、稳定的 source projection/revision、选中的事实版本和预算策略；`diff_working_context` 只返回结构化的 added/removed/superseded/changed，并包含 scalar provenance 变化。
- Task launch/retry、Handoff、verification dispatch 只传 `context_id`，不把完整 WorkingContext 塞入 CollaborationEvent 或 prompt；首次 Handoff 先建立 CollaborationEvent，再 attach 快照 ref。
- V1 禁止跨 Run；没有显式 workflow execution id 的旧任务只读取自身 Run，只有目标直接参与的 planned/direct Handoff 才扩展 sibling Run；仅有不同 per-task `run_id` 且无 execution 证据的自动 Handoff 会跳过。

FACT：实现与测试见 `herdr/context_compiler.py`、`herdr/context_models.py`、`herdr/context_sources.py`、`herdr/context_candidates.py`、`herdr/context_selection.py`、`herdr/context_projection.py`、`herdr/context_diff.py`、`herdr/state_db.py:working_contexts`、`tests/test_context_compiler.py`、`tests/test_collaboration_wiring.py`。
UNKNOWN：Context Diff 尚未接入 Dependency Wakeup；V1 不提供实时重写或跨 Workflow experience retrieval。
