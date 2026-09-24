# HAFlow Context Compiler V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从现有 Task/Workflow/Trajectory/Observation/Finding/Collaboration/Eval 事实中，确定性编译出按状态和角色选择、可追溯、不可变且有预算的 WorkingContext。

**Architecture:** `herdr/context_compiler.py` 负责公共编译入口与存储 facade；`herdr/context_models.py` 负责不可变值对象与基础规范化；`herdr/context_sources.py` 负责同一 SQLite source snapshot 与 scope 校验；`herdr/context_candidates.py` 负责纯候选构造；`herdr/context_selection.py` 负责 role relevance/filter；`herdr/context_projection.py` 负责预算/指纹；`herdr/context_diff.py` 负责结构化 diff；`herdr/state_db.py` 负责不可变投影写入与读取。CLI/Controller 只在执行边界调用编译器并传递 `context_id`。WorkingContext 不写回 Task、Trajectory、Finding 或 Observation。

**Tech Stack:** Python 3 标准库、SQLite（现有 `state_db` WAL/事务）、pytest、现有 `redact_text`/`Observation` 脱敏能力。

## Global Constraints

- 禁止 Vector DB、Embedding、RAG、LLM summarizer、长期记忆、跨 Run 检索、Graph DB、UI 和新 Agent Runtime。
- WorkingContext 只能是 Projection，不得成为事实源或修改任何源记录。
- 所有上下文项必须有合法 `source_ref`；来源必须属于同一 workflow execution scope。
- V1 角色仅支持 `developer`、`reviewer`、`tester`、`coordinator` 及设计文档列出的别名。
- 快照不可变；重复相同 fingerprint 复用最新快照，语义输入变化追加新 `context_id`。
- 所有候选和输出都必须有界；不得读取 Observation 正文、完整 transcript、chat history 或 chain-of-thought。
- 不添加第三方依赖；核心选择逻辑保持纯函数，I/O 只在 `state_db`/装配层。
- 测试使用临时 SQLite/目录，不启动真实 Agent、不调用收费模型、不重启生产服务。
- 当前授权目标为隔离分支 `feat/context-compiler-v1` 的 working tree；不 push、不合并、不部署。

---

### Task 1: 建立 WorkingContext 验收测试骨架

**Files:**
- Create: `tests/test_context_compiler.py`
- Reference: `docs/superpowers/specs/2026-09-24-context-compiler-v1-design.md`

**Interfaces:**
- Tests import `WorkingContext`, `ContextItem`, `compile_working_context`, `context_relevance`, `diff_working_context`, `get_working_context`, `get_latest_working_context`, `list_working_contexts` from `herdr.context_compiler`.
- Tests use `state_db.save_workflow`, `state_db.save_task`, `state_db.upsert_trajectory_finding`, `ObservationStore`, `TrajectoryLedger`, and `state_db.create_collaboration_event` against `tmp_path / "state.db"`.

- [ ] **Step 1: Write the failing acceptance tests**

Add fixtures/helpers that seed a workflow with `implementation -> review -> test`, tasks with distinct `run_id` values but the same `workflow_id`, and source records. Add tests named:

```python
def test_role_aware_contexts_are_distinct(...)
def test_state_aware_context_changes_after_node_transition(...)
def test_superseded_finding_is_excluded(...)
def test_finding_preserves_evidence_ref(...)
def test_run_isolation_rejects_other_run_sources(...)
def test_budget_limits_hundreds_of_findings(...)
def test_context_contains_no_raw_history(...)
def test_snapshot_is_immutable_and_recompiles_after_source_change(...)
def test_diff_reports_added_removed_superseded_changed(...)
def test_handoff_event_can_load_target_working_context(...)
```

Each test must assert observable output/storage behavior, not only that a private helper was called.

- [ ] **Step 2: Run the focused tests to verify RED**

Run:

```bash
pytest -q tests/test_context_compiler.py
```

Expected: FAIL because `herdr.context_compiler` and its public API do not yet exist.

- [ ] **Step 3: Commit the test skeleton**

```bash
git add tests/test_context_compiler.py
git commit -m "test: define context compiler acceptance matrix"
```

---

### Task 2: Add immutable WorkingContext SQLite storage

**Files:**
- Modify: `herdr/state_db.py` near the existing `context_packs` DDL and accessors
- Modify: `herdr/state_store.py` `StateStore` and `SQLiteStateStore`
- Test: `tests/test_context_compiler.py`

**Interfaces:**
- Add table `working_contexts(context_id, run_scope, run_id, workflow_id, task_id, node_id, agent_role, context_fingerprint, source_version, source_watermark, payload_json, metrics_json, compiled_at)` and `working_context_source_heads(run_scope, workflow_id, source_version, revision, updated_at)`.
- Add state_db functions:

```python
def save_working_context(context: Dict[str, Any], *, db_path: Optional[Path] = None) -> Dict[str, Any]: ...
def get_working_context(context_id: str, *, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]: ...
def get_latest_working_context(task_id: str, *, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]: ...
def list_working_contexts(task_id: str, *, db_path: Optional[Path] = None) -> List[Dict[str, Any]]: ...
```

- Add matching concrete methods on `SQLiteStateStore`; do not add a new abstract requirement that breaks existing test doubles.

- [ ] **Step 1: Write storage RED tests**

Cover: schema creation, round-trip JSON, same latest fingerprint returns the original `context_id`, changed fingerprint appends a new row, A→B→A appends again, no update/delete API, and concurrent `spawn` writers produce a canonical row without overwriting a newer source version.

- [ ] **Step 2: Run the storage tests and verify RED**

```bash
pytest -q tests/test_context_compiler.py -k 'storage or immutable or concurrent'
```

Expected: FAIL because the table/accessors do not exist.

- [ ] **Step 3: Add schema and bounded accessors**

Use `BEGIN IMMEDIATE` for writes. Read the latest row for `(task_id, agent_role, run_scope, workflow_id)` ordered by source revision/compiled time; if its fingerprint equals the candidate, return it. Otherwise insert a new `context_id`. A stale source revision is retained as history but cannot become latest. Validate identity, source-ref shape/existence, and role before writing. Never issue UPDATE/DELETE for snapshot rows. Keep JSON decoding defensive and return no fake row on absence.

- [ ] **Step 4: Add indexes and StateStore delegation**

Add indexes for `(task_id, agent_role, compiled_at)` and `(task_id, compiled_at)`. `SQLiteStateStore` delegates to the state_db functions using `self.db_path`.

- [ ] **Step 5: Run the storage tests and verify GREEN**

```bash
pytest -q tests/test_context_compiler.py -k 'storage or immutable or concurrent'
```

Expected: PASS.

- [ ] **Step 6: Commit storage**

```bash
git add herdr/state_db.py herdr/state_store.py tests/test_context_compiler.py
git commit -m "feat: persist immutable working context snapshots"
```

---

### Task 3: Implement deterministic source snapshot and compiler core

**Files:**
- Create: `herdr/context_compiler.py`
- Modify: `herdr/state_db.py` for a bounded same-connection source snapshot helper if needed
- Test: `tests/test_context_compiler.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class ContextItem: ...

@dataclass(frozen=True)
class WorkingContext: ...

def normalize_agent_role(value: str) -> str: ...
def context_relevance(item: Mapping[str, Any], *, agent_role: str,
                      current_state: Mapping[str, Any],
                      dependency_ids: Sequence[str] = (),
                      current_node_id: str = "",
                      now: Optional[float] = None) -> float: ...
def compile_working_context(*, workflow_id: str, task_id: str,
                            agent_role: str, store: Any = None,
                            task: Optional[Mapping[str, Any]] = None,
                            workflow: Optional[Mapping[str, Any]] = None,
                            boundary: str = "execution",
                            config: Optional[Mapping[str, Any]] = None,
                            now: Optional[float] = None) -> WorkingContext: ...
def diff_working_context(old: WorkingContext | Mapping[str, Any],
                         new: WorkingContext | Mapping[str, Any]) -> Dict[str, List[Dict[str, Any]]]: ...
```

- [ ] **Step 1: Add compiler RED tests for the public contract**

Assert exact invariants: required identity, valid `source_ref` on every item, role aliases, unknown-role failure, bounded field lengths, strict boolean verification, latest valid verification wins, and `context_relevance` responds to state/role/dependency more than recency.

- [ ] **Step 2: Implement identity and redaction helpers**

Use `collab_scope_for_task`, `_redact_value`, canonical JSON, SHA-256, and the existing source prefixes. Normalize aliases without silently accepting unknown roles.

- [ ] **Step 3: Implement source snapshot reads**

Within one SQLite read transaction, load target task/workflow, same-scope tasks, bounded relevant trajectory events plus an independent latest-verification window, findings, observation metadata, collaboration events, and latest eval facts. Filter by scope/task/run at the query or immediately after decode, enforce payload byte bounds, and never call `ObservationStore.read()`.

- [ ] **Step 4: Implement candidate construction**

Create only program-produced candidates for goal, current state, dependencies, completed milestones, artifacts, evidence, findings, decisions, blockers, explicit questions, verification, handoffs, and next action. Every candidate receives its canonical source ref and a bounded value.

- [ ] **Step 5: Implement supersession and latest-fact selection**

Normalize `metadata.supersedes`/`superseded_by`, reject cross-scope/cyclic references, retain only valid current findings, and retain explicit evidence refs. For verification, choose the newest strict fact per source and never let an older pass override a newer failure.

- [ ] **Step 6: Implement relevance, role profiles, and budgets**

Use the pure relevance function and role-specific profiles from the design. Select by priority/relevance/validity/recency, apply per-kind caps, then enforce serialized `max_chars` without deleting identity/current state/next action.

- [ ] **Step 7: Implement fingerprint, snapshot write, and public getters**

Fingerprint excludes random IDs and compile time but includes compiler version, stable source projection/revision, role, selected refs/values, and budget config. Persist through `state_db.save_working_context`; return the canonical row on reuse. Record the six requested metrics in `metrics_json`/snapshot and append a separate metric event per invocation.

- [ ] **Step 8: Implement deterministic diff**

Index items by `(kind, source_ref)`, return only `added`, `removed`, `superseded`, `changed`, and derive `superseded` from explicit metadata/relations. Do not produce prose.

- [ ] **Step 9: Run the compiler tests and verify GREEN**

```bash
pytest -q tests/test_context_compiler.py
```

Expected: PASS for A–I and the additional source/role/budget cases.

- [ ] **Step 10: Commit compiler core**

```bash
git add herdr/context_compiler.py herdr/state_db.py tests/test_context_compiler.py
git commit -m "feat: compile state and role aware working context"
```

---

### Task 4: Integrate Handoff and execution boundaries

**Files:**
- Modify: `herdr/collaboration.py`
- Modify: `services/herdr-controller.py`
- Modify: `bin/herdr-task`
- Test: `tests/test_context_compiler.py`, existing collaboration tests

**Interfaces:**
- `build_handoff_prompt(event, next_action="", working_context=None)` adds a bounded `WORKING_CONTEXT_REF` line only.
- `dispatch_collaboration_event` validates an existing context ref against the target Task and `run_scope`.
- `dispatch_task` compiles a context at launch/retry and appends its ref to the prompt without changing task state.
- `maybe_dispatch_node_handoffs` creates the Handoff fact first, compiles the target context with the planned direct link, then attaches only its ID to the still-`created` event before dispatch.

- [ ] **Step 1: Add integration RED tests**

Cover: launch prompt contains a context ref but not the full payload; handoff event has `context_refs == [wc_id]`; target can call `get_working_context`; wrong target/scope ref fails closed; old events without refs remain dispatchable.

- [ ] **Step 2: Run integration tests and verify RED**

```bash
pytest -q tests/test_context_compiler.py tests/test_collaboration.py tests/test_collaboration_dispatch.py
```

Expected: new context-ref assertions fail.

- [ ] **Step 3: Add collaboration prompt/reference validation**

Keep `SUMMARY_MAX`, artifact/evidence limits, and existing event identity unchanged. Add only a short context ref line and a load instruction. For a present ref, load the snapshot and verify `task_id`, `run_scope`, and `agent_role`/target compatibility; missing ref on legacy events is allowed.

- [ ] **Step 4: Wire target compilation into handoff creation**

Use deterministic role inference from target node/agent (`review`, `test`, `coord`, otherwise `developer`). Create the event with an empty ref list, compile using the direct planned link, and call `attach_working_context_ref` only while the event is `created`; compilation failure remains best-effort, but a supplied invalid ref is a dispatch failure.

- [ ] **Step 5: Wire launch/retry/verification boundaries**

At the existing prompt dispatch boundary, compile using the current Task and append `WORKING_CONTEXT_REF`. Do not call a model, start a new runtime, or change task state. Add the same bounded ref to supervisor verification prompts through the existing dispatch path.

- [ ] **Step 6: Run integration tests and verify GREEN**

```bash
pytest -q tests/test_context_compiler.py tests/test_collaboration.py tests/test_collaboration_dispatch.py
```

Expected: PASS with no full WorkingContext text in CollaborationEvent or prompt.

- [ ] **Step 7: Commit collaboration integration**

```bash
git add herdr/collaboration.py services/herdr-controller.py bin/herdr-task tests/test_context_compiler.py
 git commit -m "feat: pass working context refs through collaboration"
```

---

### Task 5: Metrics, compatibility, and documentation

**Files:**
- Modify: `herdr/metrics.py` if aggregation can be added without changing existing semantics
- Modify: `wiki/semantic-context-compact.md`
- Modify: `wiki/index.md`
- Append: `wiki/log.md`
- Test: `tests/test_metrics.py`, `tests/test_context_compiler.py`

- [ ] **Step 1: Add metrics tests**

Assert each snapshot records the six factual metrics and that an unchanged compile reports `context_reuse=True`, while a changed source reports `context_changed=True`. No quality score field is present.

- [ ] **Step 2: Add minimal read-only metrics aggregation**

If exposing counts through `HarnessRunMetrics`, add optional fields rather than changing existing required constructor arguments. Query only `working_contexts`; do not inspect source content.

- [ ] **Step 3: Update Wiki as FACT/INFERENCE/UNKNOWN**

Document the new projection boundary, storage/getter API, source rules, role profiles, supersession, budget, collaboration ref, and the explicit limitation that it is execution-boundary rather than continuously rewritten.

- [ ] **Step 4: Run focused documentation/metrics tests**

```bash
pytest -q tests/test_metrics.py tests/test_context_compiler.py
```

Expected: PASS.

- [ ] **Step 5: Commit docs/metrics**

```bash
git add herdr/metrics.py wiki/semantic-context-compact.md wiki/index.md wiki/log.md tests/test_metrics.py tests/test_context_compiler.py
git commit -m "docs: record working context compiler semantics"
```

---

### Task 6: Full verification and independent review

**Files:**
- No new source files unless a review finding requires a targeted fix.

- [ ] **Step 1: Run the complete regression suite**

```bash
pytest -q
python3 -m compileall -q herdr services bin tests
git diff --check
```

Expected: all commands exit 0. Record exact output and current HEAD in `.omc/verify-<session>.md`.

- [ ] **Step 2: Run the real persisted read-chain smoke test**

Use a temporary DB and the public path:

```text
state_db.save_task/workflow
→ TrajectoryLedger/ObservationStore/upsert_trajectory_finding
→ compile_working_context
→ state_db.get_working_context
→ CollaborationEvent(context_refs=[context_id])
→ get_working_context(context_id)
```

Assert the returned row is byte-equivalent to the immutable snapshot and no source tables are modified by compilation.

- [ ] **Step 3: Run independent review**

Provide the reviewer the design, full diff, tests, fresh verification output, and acceptance matrix. Use an independent reviewer/subagent; record findings with location, trigger, impact, and reproduction. Do not call self-review independent.

- [ ] **Step 4: Fix and re-verify any findings**

For each valid finding, follow `S6 → S4 → S5 → S6`; after any source/test change rerun focused tests, full pytest, compileall, and diff check. Stop and escalate after three non-converging review rounds.

- [ ] **Step 5: Write delivery evidence**

Create `.omc/verify-<session>.md` and `.omc/review-<session>.md` with commands, exit codes, source version, reviewer provenance, known limitations, and `MERGE_READY` only if no blocking defect remains. Do not push or create a PR without separate authorization.

## Plan Self-Review

- Spec coverage: A–J map to Tasks 1–4; provenance, immutability, budget, run isolation, fingerprint, diff, collaboration, and metrics map to Tasks 2–5.
- No placeholders: every task names concrete files, interfaces, commands, and expected behavior.
- Type consistency: compiler returns `WorkingContext`; state_db accepts/returns mappings; StateStore delegates mappings; collaboration stores string context refs; diff consumes either dataclass or mapping.
- Scope discipline: no runtime/UI/RAG/model/provider changes; only snapshot storage, compiler, and boundary wiring are in scope.
