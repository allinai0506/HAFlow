# Skeptical Architect (Zoom Out Goldfish) Audit Report

**Target Document**: [`docs/designs/workflow-autonomy-and-blocker-remediation.md`](workflow-autonomy-and-blocker-remediation.md)  
**Evaluation Standard**: Zoom-Out Standard (Root problem focus, Simplicity & YAGNI, Anti-weeds altitude control, Reusing existing codebase infrastructure, Avoiding premature optimization & architectural dead-ends)

---

### Executive Architectural Assessment

The design document addresses genuine pain points observed during long-running workflows (agent pool starvation during fix loops, abrupt halts on predecessor changes, teardown failures due to untracked test directories, and closeout backoff).

However, from an architectural simplicity, altitude, and codebase reuse perspective, the proposal suffers from significant **reinventing of existing facilities**, **misplaced domain responsibilities**, **premature complexity (magic debounce timers and duplicate counters)**, and an **irreversible state machine dead-end**.

Below are the detailed findings categorized by severity.

---

### Finding 1: Premature State Mutation to `closing` Creates an Irreversible Dead-End Lock
*   **Severity**: **Blocker**
*   **Affected Scope**: Technical Plan §5 (Lines 178–182), Detailed Implementation Plan §5 (Lines 293–298), Goal 6 (Line 37)
*   **Simplicity Rationale**:
    *   The document proposes having `services/controller/advancement.py` transition `workflows.status` to `"closing"` immediately before spawning the background teardown process (`ports.maybe_close_completed_workflow`).
    *   In the codebase state machine (`herdr/transitions.py#L150-L158`), legal transitions from `"closing"` are strictly `{"completed", "failed"}`. There is **NO** valid transition from `"closing"` back to `"running"`.
    *   Moreover, `services/controller/advancement.py#L96-L107` explicitly gates reconciliation on `record.get('status') == 'running'`:
        ```python
        if record and record.get('status') == 'running':
            recovery_store.reconcile(...)
        ```
    *   If `advancement.py` mutates status to `"closing"` before closeout completes, and the asynchronous close subprocess (`close-workflow`) encounters a transient lock or dirty file and backs off (`_note_close_backoff`), the workflow is permanently frozen: subsequent controller sweeps will skip reconciliation because the status is no longer `"running"`, and it cannot transition back to running.
    *   Mutating a lifecycle state eagerly solely for UI visibility introduces an architectural dead-end.
*   **Recommendation / Simplicity Alternative**:
    *   Do **NOT** mutate `workflow.status` to `"closing"` inside the controller advancement sweep.
    *   Leave the workflow status as `"running"` until `close-workflow` actually succeeds and marks it `"completed"`.
    *   If UI visibility of active closeout is required, expose an ephemeral runtime flag or query `ports._workflow_close_inflight` or the attention episode `workflow_id:auto-close`, rather than mutating the persistent lifecycle state.

---

### Finding 2: Reinventing Existing Infrastructure in Pure Recovery (`candidate_responsible_source`)
*   **Severity**: **Major**
*   **Affected Scope**: The Problem §2 (Lines 23–24), Technical Plan §3.1 (Lines 135–139), Implementation Plan §3 (Lines 278–280)
*   **Simplicity Rationale**:
    *   The design proposes modifying `candidate_responsible_source` in `herdr/workflow_progress.py`:
        > *"在 `candidate_responsible_source` 中扩展匹配依据：除 `commit == candidate` 外，增加对 Task 元数据中 `integrated_commit == candidate` 及 `(wrapup_delivery or {}).get('integrated_sha') == candidate` 的纯内存校验"*
    *   **Codebase Reality**: An inspection of `herdr/workflow_progress.py#L120-L124` and `L148-L151` reveals that this exact logic **ALREADY EXISTS**:
        ```python
        matches_candidate = (
            task.get('integrated_commit') == candidate_sha
            or delivery.get('integrated_sha') == candidate_sha
            or task.get('commit') == candidate_sha
        )
        ```
    *   The proposal assumes this facility is missing and designs work to create it. Proposing this code change fails to diagnose the real root cause of `affected_tasks_unknown`: why was `affected` empty in `assess_workflow`? (E.g., did `active_tasks(workflow, tasks)` exclude the task because it had a mismatched `execution_id`, was superseded, or was the gate task's `candidate_sha` not propagated?).
*   **Recommendation / Reuse Opportunity**:
    *   Delete the proposal to modify `candidate_responsible_source`.
    *   Focus directly on the root cause: ensure the verifier gate task faithfully records `candidate_sha` upon blocked verdict, or verify whether `active_tasks` filtering excluded the integrated task during assessment.

---

### Finding 3: Misplaced Responsibility & Over-Engineered State Machine in Agent Router
*   **Severity**: **Major**
*   **Affected Scope**: Technical Plan §2 (Lines 119–128), Implementation Plan §2 (Lines 268–274), Goal 2 (Lines 33–34)
*   **Simplicity Rationale**:
    1.  **Misplaced Domain Boundary**: The proposal introduces `MAX_REMEDIATION_ROUNDS = 2` inside `herdr/agent_router.py`, raising `RouterRemediationExhausted`. The Router's sole architectural role is agent selection and capacity matching (binding stage requirements to healthy agents). Budget limits, max task counts, and retry thresholds belong to `herdr/node_capacity.py` (`max_tasks_per_node`, `launch_capacity_error`) and `herdr/workflow_progress.py#L268-L280` (`verifier_budget_exhausted`). Placing remediation iteration limits inside `agent_router.py` duplicates budget accounting and creates circular domain entanglement.
    2.  **Rigid 2-Agent Formula**: The proposal specifies a bespoke 2-agent state machine formula:
        $$\text{Round 1: } [\text{Author: } A, \text{ Reviewer: } B] \xrightarrow{\text{blocked}} \text{Round 2: } [\text{Author: } B, \text{ Reviewer: } A]$$
        This is speculative over-engineering. If routing isolation is scoped to the candidate episode (`candidate_episode_id`), the verifier simply excludes agents that authored that specific candidate episode. Alternation between 2 agents happens naturally as an emergent property without hardcoding a dedicated 2-agent role transition mechanism. Hardcoding 2-agent transitions introduces dead-ends when pools have 1 agent (with `allow_soft_degrade`) or 3+ agents.
    3.  **Altitude Drop & Non-Existent Abstraction**: The document repeatedly references refactoring `_compute_used_agents` (Lines 116, 270), but no such function exists in `agent_router.py` (used agents are computed inline at lines 588–601, which already supports `allow_soft_degrade`).
*   **Recommendation / Reuse Opportunity**:
    *   In `herdr/agent_router.py`, implement round-scoped isolation generically: when `candidate_episode_id` is supplied, filter exclusion only against tasks matching that episode ID.
    *   Do **NOT** hardcode a 2-agent role-alternation state machine in the router.
    *   Enforce remediation round limits using the existing `herdr/node_capacity.py` (`max_tasks_per_node`) and `herdr/workflow_progress.py` (`verifier_budget_exhausted`), rather than introducing a new `RouterRemediationExhausted` exception into the router.

---

### Finding 4: Fragile Concurrency & Premature Complexity in `node_dispatch.py` (15s Debounce & Speculative `workflow.generation_id`)
*   **Severity**: **Major**
*   **Affected Scope**: Technical Plan §3.2 (Lines 141–150), Implementation Plan §4 (Lines 284–291), Goal 4 (Line 35)
*   **Simplicity Rationale**:
    *   The proposal modifies `herdr/node_dispatch.py#L201-L204`:
        ```python
        # Current:
        return ('waiting_human' if operation['started'] else 'superseded', ...)
        # Proposed:
        return 'superseded', {'reason': 'dispatch_predecessor_changed', 'responsibility_transferred_to': 'replan'}
        ```
    *   `operation['started']` signifies that worker execution has already been initiated. Unconditionally returning `'superseded'` when an operation is already started risks spawning a duplicate, concurrent worker before the previous worker process has settled, violating AGENTS.md §4.7.
    *   To counteract this race condition, the design introduces:
        1. A new `workflow.generation_id` field with CAS validation (ignoring that `node_dispatch.py#L31` already defines generation identity as `[execution_id, created_at, reopened_at]`);
        2. An ad-hoc "15-second debounce window (Debounce Window)" injected into the Controller main loop;
        3. A new `max_replan_attempts = 2` counter in operation payloads.
    *   Arbitrary sleep/debounce timers in state machine loops are classic anti-patterns that mask synchronization races rather than resolving state invariants.
*   **Recommendation / Simplicity Alternative**:
    *   Drop the 15-second debounce timer and the redundant `workflow.generation_id` CAS layer.
    *   Preserve the invariant: if `operation['started']` is True, verify whether the started task is in an active execution status (`transitions.py`). If the task has reached a terminal state (or was discarded), it can safely supersede without a debounce hack; if it is actively running, it should wait for worker settlement.

---

### Finding 5: Narrow Symptom Patch for Workstation Teardown Whitelist (`test-evidence` vs Standard Test/Coverage Artifacts)
*   **Severity**: **Major / Nit**
*   **Affected Scope**: Technical Plan §4 (Lines 157–171), Implementation Plan §1 (Lines 259–265), Goal 5 (Line 36), Open Question 2 (Lines 49–50)
*   **Simplicity Rationale**:
    *   Adding `"test-evidence"` to `BYPASS_IGNORED_DIRS` in `herdr/clone_archive.py` is a 1-line hardcoded patch tailored to a single test run (`wf-haflow-1029-02`).
    *   In standard Python/Node development, test and coverage runs regularly produce `.coverage`, `coverage`, `htmlcov`, `playwright-report`, and `.nyc_output`.
    *   Whitelisting only `"test-evidence"` leaves the door open for the very next test framework to trigger exit code 2 and lock teardown again.
*   **Recommendation / Reuse Opportunity**:
    *   Expand `BYPASS_IGNORED_DIRS` in `herdr/clone_archive.py` in one shot with standard test and coverage transient output folders:
        ```python
        "test-evidence", ".coverage", "htmlcov", "coverage", "playwright-report"
        ```
    *   This provides a robust, generalized solution without needing speculative declarative template configurations.

---

### Finding 6: Altitude Drops into Implementation Weeds & Missing Architectural Invariants
*   **Severity**: **Nit**
*   **Affected Scope**: Sections 2–5, Implementation Plan §1–§6 (Lines 253–338)
*   **Simplicity Rationale**:
    *   The Technical Plan drops repeatedly into low-level code mechanics: citing brittle code line numbers, prescribing exact Python dict assignments and local variable names.
    *   At the same time, high-level data contracts are left vague: How is `candidate_episode_id` propagated to task records during dispatch? What ensures database consistency across legacy tasks lacking `candidate_episode_id`?
*   **Recommendation**:
    *   Elevate the Technical Plan to focus on architectural invariants, state transition rules, and database schema contracts rather than ephemeral source code line numbers and snippet mechanics.

---

### Summary Checklist for Document Revision

| Vector | Finding | Action Required |
| :--- | :--- | :--- |
| **Architectural Dead-End** | Setting `workflow.status = 'closing'` in `advancement.py` | **Eliminate**. Keep status `running` until close completes; use runtime observation for closeout visibility. |
| **Reinventing Wheel** | Adding `integrated_commit` to `candidate_responsible_source` | **Eliminate**. The check already exists in `workflow_progress.py#L120-L151`. Investigate true assessment gap. |
| **Separation of Concerns** | `MAX_REMEDIATION_ROUNDS = 2` in `agent_router.py` | **Relocate**. Reuse `node_capacity.py` (`max_tasks_per_node`) and `workflow_progress.py`. |
| **Over-Engineering** | Bespoke 2-agent alternation state machine | **Simplify**. Make episode-scoped exclusion generic; alternation will emerge naturally. |
| **Premature Hack** | 15s debounce window & new `generation_id` in `node_dispatch` | **Eliminate**. Rely on active task state checks instead of magic timer debounces. |
| **Symptom Patch** | Whitelisting only `"test-evidence"` | **Generalize**. Include standard test/coverage directories (`.coverage`, `htmlcov`, `coverage`). |
