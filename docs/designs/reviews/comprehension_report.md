# Comprehension Goldfish Audit Report

**Target Document**: [`docs/designs/workflow-autonomy-and-blocker-remediation.md`](workflow-autonomy-and-blocker-remediation.md)  
**Evaluation Standard**: Document Self-Containment, Clarity, Terminology Definitions & Assumptions

---

### What this proposes
This document proposes an automated self-healing and blocker-remediation architecture for long-running HAFlow workflows across four primary bottlenecks: agent routing, defect attribution and re-dispatch, workstation teardown hygiene, and workflow closeout visibility. It restricts cross-stage agent isolation to the active remediation episode with a strict two-round circuit breaker and role alternation for two-agent pools to prevent pool exhaustion during fix loops. It introduces pure in-memory causal attribution of defects and allows predecessor-changed operations to autonomously re-plan via `superseded` rather than halting for human intervention. Finally, it whitelists transient `test-evidence` directories in workspace teardown guards to eliminate non-zero exit codes, introduces an explicit `closing` status transition, and raises high-priority attention alerts with actionable diagnostic commands when closeout retries fail.

### How the existing system works, per this document
According to this document, HAFlow enforces stage isolation between implementation and verification agents by accumulating a flat, global exclusion set of all previously used agents across the entire workflow history, which rapidly exhausts small agent pools (3–5 instances) during multi-round fix loops and leads to routing deadlocks. When verification gates fail without explicitly populating affected task IDs, or when predecessor tasks change, the recovery engine defensively halts progress and transfers responsibility to human operators by entering `waiting_human`. Furthermore, workstation teardown guards fail with exit code 2 upon detecting unmanaged test artifacts such as `test-evidence/`, blocking workspace cleanup and clone recycling. Consequently, the automatic closeout pipeline enters a silent exponential backoff of up to 600 seconds without alerting operators, leaving completed workflows in an indefinitely hung pseudo-running state.

---

### Findings

#### Finding 1
* **Severity**: Major
* **Affected Scope**: Section "Technical Plan" & "Detailed Implementation Plan", component `services/controller/workflow_close.py` (lines 180–182, lines 302–304)
* **Discrepancy Description & Rationale**:
  The document specifies modifying `_note_close_backoff` in `services/controller/workflow_close.py` to post an Attention Episode via `ports.attention_post` after $\ge 2$ consecutive retries. However, `_note_close_backoff` is not defined in `workflow_close.py`; in that module, it is invoked on the injected controller ports adapter (`ports._note_close_backoff(workflow_id, close_key)`). The actual implementation of `_note_close_backoff` lives in `services/herdr-controller.py`. Listing `workflow_close.py` as the modification site for `_note_close_backoff` misleads the implementer and leaves the actual controller change unstated.

#### Finding 2
* **Severity**: Major
* **Affected Scope**: Section "Technical Plan" & "Detailed Implementation Plan", component `herdr/agent_router.py` (lines 114–117, lines 268–270)
* **Discrepancy Description & Rationale**:
  The document repeatedly directs the implementer to "重构 `_compute_used_agents`" (refactor `_compute_used_agents`) to calculate `stage_used_agents` using `candidate_episode_id`. However, no function named `_compute_used_agents` exists in `herdr/agent_router.py`. The stage exclusion calculation is currently inlined directly inside `_choose_agent_locked` (lines 588–601). A reader unfamiliar with the codebase cannot locate `_compute_used_agents` and the document should clarify that this calculation must either be extracted into a helper or modified inline within `_choose_agent_locked`.

#### Finding 3
* **Severity**: Major
* **Affected Scope**: Section "Goals" (line 35), "Technical Plan" Subsystem 2 (lines 148–150), "Detailed Implementation Plan" (lines 284–290)
* **Discrepancy Description & Rationale**:
  The document specifies "Generation Fencing & Debounce" with the requirement: *"在每次就地 Re-plan 时，要求通过 CAS 校验并递增 `workflow.generation_id`，且在 Controller 主循环中对同一节点施加 15 秒的防抖窗口（Debounce Window）"*.
  However:
  1. It is unstated what `workflow.generation_id` is, where it is persisted (in SQLite `workflows` table or in-memory metadata), and how CAS is executed (e.g., via SQLite conditional update or StateStore method).
  2. No database migration, schema change, or store module (`herdr/state_db.py` / `herdr/state_store.py`) is included in the "Target Files to Create/Modify" section.
  3. The implementation site and mechanism of the 15-second debounce window in the Controller main loop are absent from the Detailed Implementation Plan (neither `services/herdr-controller.py` nor `advancement.py` lists debounce logic). A reader has no way to discern how or where to implement the debounce timer or CAS generation check.

#### Finding 4
* **Severity**: Major
* **Affected Scope**: Lines 32, 57, 92, 115, 249, 269 (Concepts: `candidate_episode_id` vs `dispatch_round`)
* **Discrepancy Description & Rationale**:
  The document uses `candidate_episode_id` and `dispatch_round` without defining either term or explaining their lifecycle:
  1. In lines 115 and 249, they appear as interchangeable synonyms (`candidate_episode_id: int（或 dispatch_round: int）`), but in line 269 they are listed as two separate optional parameters (`candidate_episode_id: Optional[int] = None` and `dispatch_round: Optional[int] = None`). It is unclear whether they represent distinct concepts or if one is legacy.
  2. The sequence diagram (step 92) notes `candidate_episode_id 步进 (Round 1 -> Round 2)`. However, the document never specifies which component (Controller? WorkflowProgress? StateStore?) is responsible for stepping or minting this value, where it is stored on the Workflow/Task record, or how it gets passed through into `_choose_agent_impl`.

#### Finding 5
* **Severity**: Major
* **Affected Scope**: Lines 118–122, Line 272 (Section "Subsystem 1: Dual-Agent Role Alternation vs General Pools")
* **Discrepancy Description & Rationale**:
  The document defines a deterministic role-alternation state machine for a 2-agent pool: `Round 1: [A: Author, B: Reviewer] -> Round 2: [B: Author, A: Reviewer]`. However, it leaves the general routing behavior for pools of 3–5 agents (the typical size noted in line 22) underspecified.
  Specifically, line 117 states that exclusion will *"仅排除在当前轮次内已参与该阶段或互斥阶段的 Agent"*. If exclusion is strictly scoped to the *current* round, then at the start of Round 2, no tasks have run in Round 2 yet. This leaves unstated whether Agent A (the author whose code was blocked in Round 1) could be picked to review in Round 2 or re-picked to implement, or whether the router preserves cross-round author-reviewer exclusion memory when pool size $\ge 3$. The general multi-agent algorithm is left ambiguous.

#### Finding 6
* **Severity**: Nit
* **Affected Scope**: Line 48 (Open Question 1) vs Line 139 & Line 280
* **Discrepancy Description & Rationale**:
  Open Question 1 asks whether parallel tasks should all be remediated together or if the primary task modifying more files should be prioritized. However, Section 3.1 (line 139) and Section 5.3 (line 280) treat the collective fallback as already finalized ("系统坚决不盲推单一 commit，而是将这两个并行任务作为联合回炉集合，安全回退"). Open Question 1 should be updated to reflect that this design choice has already been settled.

#### Finding 7
* **Severity**: Nit
* **Affected Scope**: Lines 278–280 (Section 5.3 `workflow_progress.py`)
* **Discrepancy Description & Rationale**:
  The document lists as a proposed change to `workflow_progress.py`: *"在 candidate_responsible_source 中扩展匹配依据：除 commit == candidate 外，增加对 Task 元数据中 integrated_commit == candidate 及 (wrapup_delivery or {}).get('integrated_sha') == candidate 的纯内存校验"*.
  In the current codebase (`herdr/workflow_progress.py#L120-L151`), both `integrated_commit == candidate_sha` and `(task.get('wrapup_delivery') or {}).get('integrated_sha') == candidate_sha` already exist. Framing this as a new addition rather than acknowledging existing code creates confusion; the actual new change in `workflow_progress.py` is the multi-task fallback in `assess_workflow` when `affected` is empty.

#### Finding 8
* **Severity**: Nit
* **Affected Scope**: Lines 37, 180–181, 303 (Term: "Attention Episode" / `ports.attention_post`)
* **Discrepancy Description & Rationale**:
  The document relies on the concept of an "Attention Episode" and `ports.attention_post` as the mechanism for notifying operators of closeout failures, but never defines what an "Attention Episode" is for a reader unfamiliar with HAFlow's operator alerting subsystem. A brief inline definition or link to `wiki/` documentation would make this section self-contained.
