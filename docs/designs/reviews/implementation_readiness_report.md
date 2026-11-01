# Implementation Readiness Audit Report (Stage 9)

**Target Document**: [`docs/designs/workflow-autonomy-and-blocker-remediation.md`](workflow-autonomy-and-blocker-remediation.md#L253-L367)  
**Target Scope**: Detailed Implementation Plan (Lines 253..367)  
**Reviewers**: `egm-implementation-readiness` (Pragmatic Tech Lead), `egm-codebase-details` (Codebase Details Auditor), `egm-style` (Technical Copy Editor)  
**Standards Applied**: `resources/document_standards/implementation-readiness-standard.md`, Codebase Verification, Technical Style Rules  
**Overall Verdict**: **FAIL (Requires Remediation before Implementation)**

---

## Executive Summary

The antagonistic review swarm evaluated the Detailed Implementation Plan against the **Zero-Decision Rule**, concrete **Codebase Reality**, and **Engineering Style Standards**.

While the plan identifies the correct subsystem targets, it contains several critical defects that make direct execution impossible without guessing:
1. **Missing Milestone Phasing Structure**: Promised 4 ordered milestones in line 255, but presented a flat list of 10 files without execution phases or intermediate gates.
2. **Non-Existent APIs and Ghost Functions**:
   - `_reconcile_workflow_advancement` does not exist in `services/controller/advancement.py` (the actual completion check is `check_workflow_stage_advance`).
   - `ports.attention_post` does not exist in `services/controller/attention.py` (the actual method is `ports.attention_note`).
   - `_compute_used_agents` does not exist in `herdr/agent_router.py` (it is inlined in `_choose_agent_locked`).
   - Public entry point `choose_agent` was omitted from signature updates.
3. **Data Model Integrity Hazards**:
   - Mutating `operation['payload']` for `replan_count` breaks `node_dispatch`'s SHA-256 `identity_key` hash.
   - The "15-second debounce window" does not exist in the controller loop (which ticks every 2 seconds).
4. **Reinventing Existing Code**:
   - `candidate_responsible_source` already checks `integrated_commit` and `integrated_sha`; the real defect is hardcoded `'implementation'` node filtering and missing fallback matching in `assess_workflow`.

---

## Detailed Findings by Reviewer

### 1. Pragmatic Tech Lead (`egm-implementation-readiness`)

#### Finding IR-1: Missing Milestone Phasing Structure
* **Severity**: 🔴 **Blocker**
* **Affected Scope**: Line 255
* **Description**: The document states that the plan *"分为四个有序里程碑实施"*, but lines 257–364 list 10 files in a flat sequence without milestone boundaries, delivery order, or checkpoint gates.
* **Remedy**: Reorganize the plan into 4 explicit sequential phases with corresponding test gates:
  - Milestone 1: Teardown Hygiene Bypass & Whitelist Expansion
  - Milestone 2: Round-Scoped Router Isolation & Verifier Scope
  - Milestone 3: Defect Attribution & Dynamic Node Recovery
  - Milestone 4: Closeout Visibility, Diagnostic Alerts & E2E Validation

#### Finding IR-2: Ghost Function & Underspecified Public Router Entry Point
* **Severity**: 🔴 **Blocker**
* **Affected Scope**: Item 2 (`herdr/agent_router.py#L268-L274`)
* **Description**:
  1. Directs the engineer to modify non-existent `_compute_used_agents` (the calculation is inlined in `_choose_agent_locked#L588-L601`).
  2. Updates private methods `_choose_agent_impl` and `_choose_agent_locked`, but omits the public wrapper `choose_agent(...)` called by `bin/herdr-task launch` and external modules.
* **Remedy**:
  - Update `choose_agent(...)` signature to accept `candidate_episode_id: Optional[int] = None` and `dispatch_round: Optional[int] = None`.
  - Specify modifying lines 588–601 in `_choose_agent_locked` directly or extracting it into a helper.

#### Finding IR-3: Pure Engine / Store Responsibility Gap for Replan Counter
* **Severity**: 🔴 **Blocker**
* **Affected Scope**: Item 4 (`herdr/node_dispatch.py#L284-L291`)
* **Description**: `herdr/node_dispatch.py` is a pure evaluation engine whose `result()` returns status tuples; it does not mutate database records. The plan fails to specify which module updates `replan_count`.
* **Remedy**: Detail that `herdr/node_dispatch_store.py` increments `replan_count` in operation `detail` metadata upon transitioning to `superseded`, and `node_dispatch.py:result` inspects `operation.get('detail', {}).get('replan_count', 0) >= 2` to return `('waiting_human', {'reason': 'dispatch_replan_exhausted'})`.

#### Finding IR-4: Undefined Status Mutation API & Re-entrancy Hazard
* **Severity**: 🟡 **Major**
* **Affected Scope**: Item 5 (`services/controller/advancement.py#L293-L298`)
* **Description**: Fails to name the exact API to update workflow status, and fails to handle re-entrancy in `advancement.py` where the sweep loop ticks every 2 seconds and could redundantly trigger closeout.
* **Remedy**: Use `ports.transition_workflow(workflow_id, to_status=...)` or query `ports._workflow_close_inflight`.

#### Finding IR-5: Non-Existent API `ports.attention_post` & Missing Error Parameter
* **Severity**: 🟡 **Major**
* **Affected Scope**: Item 6 (`services/controller/workflow_close.py#L300-L305`)
* **Description**: `ports.attention_post` does not exist; the actual API is `ports.attention_note(...)`. Furthermore, `_note_close_backoff` does not receive process stdout/stderr to parse blocking files.
* **Remedy**: Update signature to `_note_close_backoff(ports, workflow_id, close_key, error_output="")` and invoke `ports.attention_note(...)`.

#### Finding IR-6: Undefined Fallback Query in `workflow_progress.py`
* **Severity**: 🟡 **Major**
* **Affected Scope**: Item 3 (`herdr/workflow_progress.py#L276-L282`)
* **Description**: The fallback attribution rule (*"安全回退至当前节点的全部活动实现任务集合"*) lacks concrete comprehension code.
* **Remedy**: Provide the exact list comprehension: `[t['task_id'] for t in current if (t.get('node') or t.get('stage')) == retry and t.get('status') not in ('superseded', 'failed')]`.

---

### 2. Codebase Details Auditor (`egm-codebase-details`)

#### Finding CD-1: Non-Existent Method `_reconcile_workflow_advancement`
* **Severity**: 🔴 **Blocker**
* **Affected Scope**: `services/controller/advancement.py#L293-L297`
* **Description**: `_reconcile_workflow_advancement` does not exist in `services/controller/advancement.py`. The real DAG completion evaluation is inside `check_workflow_stage_advance(workflow_id, ports, now)`.
* **Remedy**: Target `check_workflow_stage_advance#L194-L237`.

#### Finding CD-2: Non-Existent Method `ports.attention_post` in Controller
* **Severity**: 🔴 **Blocker**
* **Affected Scope**: `services/controller/attention.py`, `services/controller/workflow_close.py`
* **Description**: Calling `ports.attention_post` will trigger an unhandled `AttributeError` at runtime.
* **Remedy**: Use `ports.attention_note(close_key, task, event_type="auto_close_failed", reason=..., detail=...)`.

#### Finding CD-3: Missing Stderr/Stdout Parameter in `_note_close_backoff`
* **Severity**: 🟡 **Major**
* **Affected Scope**: `services/controller/workflow_close.py#L61-L82, L161-L167`
* **Description**: Call sites pass only `(workflow_id, close_key)` without passing process exit output.
* **Remedy**: Update signature and call sites to pass `result.stderr.strip() or result.stdout.strip()`.

#### Finding CD-4: Non-Existent Function `_compute_used_agents` in `agent_router.py`
* **Severity**: 🟡 **Major**
* **Affected Scope**: `herdr/agent_router.py#L588-L601`
* **Description**: Used-agents calculation is inlined in `_choose_agent_locked`.
* **Remedy**: Specify modifying lines 588–601 directly or extracting a helper.

#### Finding CD-5: Factual Code Inaccuracy in `workflow_progress.py`
* **Severity**: 🟡 **Major**
* **Affected Scope**: `herdr/workflow_progress.py#L120-L151`
* **Description**: Claims to add `integrated_commit` to `candidate_responsible_source`, but it is already there. Real missing logic is in `assess_workflow#L249`.
* **Remedy**: Acknowledge existing checks and target `assess_workflow#L249` and `_identity_reason#L195`.

#### Finding CD-6: Hashing Hazard in `operation['payload']` & Sweep Timing Reality
* **Severity**: 🟡 **Major**
* **Affected Scope**: `herdr/node_dispatch.py#L29-L49`, `services/controller/registry.py#L106`
* **Description**: Mutating `operation['payload']` mutates the cryptographic SHA-256 `identity_key`. Controller sweep ticks every 2 seconds, not 15 seconds.
* **Remedy**: Store `replan_count` in `operation['detail']`, not `payload`.

#### Finding CD-7: Extensionless Script Compilation in Verification Plan
* **Severity**: 🟢 **Nit**
* **Affected Scope**: Lines 343–347
* **Description**: `compileall` does not check `bin/herdr-task` or `bin/herdr-factory`.
* **Remedy**: Add `python3 -m py_compile bin/herdr-task bin/herdr-factory`.

---

### 3. Technical Copy Editor (`egm-style`)

#### Finding STY-1: Structural Promise Discrepancy
* **Severity**: 🔴 **Blocker**
* **Affected Scope**: Line 255
* **Description**: Promised 4 ordered milestones but delivered flat list.
* **Remedy**: Align text to 4 milestone sections.

#### Finding STY-2: Emotive Rhetoric in Rationales
* **Severity**: 🟡 **Major**
* **Affected Scope**: Lines 282, 329, 337
* **Description**: "坚守...底线", "守护...不变量", "彻底证明系统级自主推进能力的恢复".
* **Remedy**: Replace with objective engineering statements.

#### Finding STY-3: Hyperbolic & Colloquial Phrasing
* **Severity**: 🟡 **Major**
* **Affected Scope**: Lines 264, 271, 298, 303, 335
* **Description**: "彻底消除", "绝对客观独立性", "回炉轮次", "表面假装 running", "物理阻断".
* **Remedy**: Replace with standard technical terminology ("修复轮次", "生命周期一致性").

#### Finding STY-4: Formatting & Metadata Cleanup
* **Severity**: 🟢 **Nit**
* **Affected Scope**: Lines 303, 362, 367
* **Description**: Inline LaTeX `$\ge 2$`, duplicate wording `Zero-Warning Clean Baseline`, trailing `*Drafted with EGM*`.
* **Remedy**: Use `>= 2`, rename heading, and remove trailing watermark.
