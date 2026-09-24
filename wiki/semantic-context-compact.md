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
- `working_contexts` 是不可变快照表；最新相同 fingerprint 复用旧 `context_id`，事实变化追加新快照。
- `context_fingerprint` 覆盖角色、范围、选中的事实版本和预算策略；`diff_working_context` 只返回结构化的 added/removed/superseded/changed。
- Task launch/retry、Handoff、verification dispatch 只传 `context_id`，不把完整 WorkingContext 塞入 CollaborationEvent 或 prompt。
- V1 禁止跨 Run；没有显式 workflow execution id 的旧任务只读取自身 Run，除非已有 CollaborationEvent 证明 handoff 链接。

FACT：实现与测试见 `herdr/context_compiler.py`、`herdr/state_db.py:working_contexts`、`tests/test_context_compiler.py`。
UNKNOWN：Context Diff 尚未接入 Dependency Wakeup；V1 不提供实时重写或跨 Workflow experience retrieval。
