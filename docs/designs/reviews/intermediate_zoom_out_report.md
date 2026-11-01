# Intermediate Zoom Out Architectural Audit Report (Stage 8)

**Target Document**: [`docs/designs/workflow-autonomy-and-blocker-remediation.md`](workflow-autonomy-and-blocker-remediation.md) (Lines 1..368)  
**Standard**: [Zoom-Out Standard](file:///Users/user/Library/Application%20Support/Antigravity%20CLI%20Isolated/home/.gemini/config/plugins/egm/resources/document_standards/zoom-out-standard.md)  
**Reviewer**: Skeptical Architect (Zoom Out Goldfish)  
**Context Evaluated**: Target Document, Codebase Reality (`herdr/`, `services/`, `workflow_templates/`), Initial Zoom Out Report, Comprehension Report, Critic Report  

---

### Executive Architectural Assessment

The business goal of this document—restoring autonomous flow by preventing artificial agent pool starvation, eliminating teardown blocking, and unfreezing closeout—is critical and well-justified.

However, from a high-altitude architectural perspective, the document as currently written contains **3 critical system-killing blockers**, **3 major architectural entanglements**, and a **misdiagnosed root cause** that would leave production workflows (`code-craft-v1`) completely broken. 

The proposal attempts to resolve synchronization and lifecycle challenges by introducing speculative state transitions (`closing`), inverted role gymnastics, an unbacked database column (`workflow.generation_id`), and an arbitrary 15-second debounce sleep hack, while missing that existing codebase abstractions already provide simpler, robust solutions.

Below is the detailed architectural assessment and the clean path forward.

---

### Findings

#### Finding 1: Premature State Mutation to `'closing'` Creates an Irreversible Controller Freeze
* **Severity**: **Blocker**
* **Affected Scope**: Technical Plan §1 & §5 (Lines 78–106, 178–182), Detailed Implementation Plan §5 (Lines 293–298), Goal 6 (Line 37)
* **Simplicity Rationale & Architectural Assessment**:
  1. *State Machine Lockout*: In `herdr/transitions.py#L155`, legal transitions from `'closing'` are strictly `{"completed", "failed"}`. There is no legal transition edge back to `'running'` or `'waiting_human'`.
  2. *Reconciliation Skip*: In `services/controller/advancement.py#L97`, the entire advancement and recovery reconciliation sweep is gated on:
     ```python
     if record and record.get('status') == 'running':
         recovery_store.reconcile(...)
     ```
  3. Once `advancement.py` eagerly mutates `workflow.status` to `'closing'`, subsequent controller sweeps immediately stop reconciling this workflow.
  4. If the asynchronous closeout process (`close-workflow`) takes time to finalize pending Git tasks, encounters a dirty worktree, or enters exponential backoff (`_note_close_backoff`), the workflow is permanently abandoned in `'closing'` with zero reconciliation. Attempting to reopen or resume it raises `InvalidTransitionError`.
  5. Eagerly mutating persistent lifecycle state solely for UI visibility introduces an unrecoverable architectural dead-end.
* **Recommendation**:
  - Keep `workflows.status` as `'running'` until `herdr-task close-workflow` successfully finishes and writes the terminal receipt (`completed`).
  - Represent active closeout visibility via ephemeral runtime state (e.g. querying `ports._workflow_close_inflight` or the Attention episode `workflow_id:auto-close`), completely avoiding premature persistent state mutations.

---

#### Finding 2: Inverted Role Alternation is an Algorithmic Contradiction and Quality Anti-Pattern
* **Severity**: **Blocker**
* **Affected Scope**: Technical Plan §2.3 (Lines 118–128), Detailed Implementation Plan §2 (Lines 268–274), Goal 1 (Line 32), CUJ 1 (Lines 53–61)
* **Simplicity Rationale & Architectural Assessment**:
  1. *Algorithmic Contradiction*: The document proposes:
     $$\text{Round 1: } [\text{Author: } A, \text{ Reviewer: } B] \xrightarrow{\text{blocked}} \text{Round 2: } [\text{Author: } B, \text{ Reviewer: } A]$$
     Simultaneously, line 117 states that exclusion will *"仅排除在当前轮次内已参与该阶段或互斥阶段的 Agent"*. At the start of Round 2, Round 2 has 0 tasks, so `stage_used_agents` is completely empty. The router's static pool preference will simply pick Agent A again for implementation, breaking the claimed alternation formula.
  2. *Quality Anti-Pattern*: Forcing Reviewer B to implement the fix for Author A's 500 lines of code, while Author A reviews Reviewer B's fix, means Author A is reviewing code where 95%+ was authored by Author A. This triggers the exact self-review confirmation bias that Non-Goal 1 explicitly forbids.
  3. *Emergent Architecture*: The fundamental HAFlow quality invariant (`AGENTS.md` §4.1) is: **The verifier of candidate $C_k$ cannot be an author of candidate $C_k$**. Implementation nodes are not verifiers; nothing forbids Author A from fixing their own code!
     - In Round 1, Author A builds $C_1$, Reviewer B verifies $C_1$ (blocked).
     - In Round 2, Author A fixes the bug and produces candidate $C_2$.
     - When selecting the verifier for $C_2$, candidates are $\{A, B\}$. Since $A$ authored $C_2$, $A$ is excluded, and Reviewer B naturally re-verifies $C_2$.
     - Author-verifier independence is strictly preserved across both rounds with zero role-inversion gymnastics.
* **Recommendation**:
  - Eliminate the bespoke 2-agent inverted rotation state machine from `herdr/agent_router.py`.
  - Scope verifier exclusion strictly by candidate episode / round: when verifying candidate $C_k$, exclude only agents who authored tasks integrated into candidate $C_k$. Implementation nodes have no exclusion against fixing previous code.

---

#### Finding 3: Non-Existent Column `workflow.generation_id` and Over-Engineered 15s Debounce Hack
* **Severity**: **Blocker**
* **Affected Scope**: Technical Plan §3.2 (Lines 148–150), Detailed Implementation Plan §4 (Lines 284–291), Goal 4 (Line 35)
* **Simplicity Rationale & Architectural Assessment**:
  1. *Schema Missing*: The document specifies CAS validation on `workflow.generation_id`. In `herdr/state/schema.py#L29-L40`, the `workflows` table contains no `generation_id` column. Executing a CAS update against it raises `sqlite3.OperationalError: no such column: generation_id`.
  2. *Premature Hack*: The document proposes unconditionally returning `'superseded'` when predecessors change (`node_dispatch.py#L201`). Realizing that an already-started worker process (`operation['started'] == True`) might still be running and create pane/git conflicts, the design slapped on an arbitrary "15-second debounce window" in the controller loop and a speculative CAS column.
  3. Arbitrary sleep/debounce timers in state machine loops are classic anti-patterns that mask race conditions rather than resolving state invariants. Furthermore, `herdr/node_dispatch.py#L31` already models dispatch generation identity via `[workflow.get('execution_id'), workflow.get('created_at'), workflow.get('reopened_at')]`.
* **Recommendation**:
  - Drop the speculative `workflow.generation_id` column and the 15-second debounce window entirely.
  - In `node_dispatch.py`, when `not predecessors_current`, inspect the started task:
    - If the started task is in a terminal state (`completed`, `cleaned`, `superseded`), it can safely supersede without timers;
    - If the worker is still actively executing, either issue a clean cancellation or defer until worker cessation before superseding.

---

#### Finding 4: Misdiagnosed Root Cause in `candidate_responsible_source` Leaves Flagship Workflows (`code-craft-v1`) Broken
* **Severity**: **Major**
* **Affected Scope**: The Problem §2 (Lines 23–24), Technical Plan §3.1 (Lines 135–139), Detailed Implementation Plan §3 (Lines 278–280)
* **Simplicity Rationale & Architectural Assessment**:
  1. *Reinventing Existing Code*: The design proposes adding `integrated_commit == candidate` and `wrapup_delivery.integrated_sha == candidate` to `candidate_responsible_source` in `workflow_progress.py`. However, in `herdr/workflow_progress.py#L120-L124` and `L148-L151`, **these exact checks already exist**.
  2. *The True Root Cause*: In `wf-haflow-1029-02` (and all workflows using `workflow_templates/code-craft-v1.yaml`), the implementation node is named `'craft'`, not `'implementation'`.
  3. Line 142 of `herdr/workflow_progress.py` literally hardcodes:
     ```python
     if (task.get('node') or task.get('stage')) != 'implementation':
         continue
     ```
     Because `'craft' != 'implementation'`, `candidate_responsible_source` skipped all craft tasks and returned `(None, [])`!
  4. Furthermore, in `assess_workflow` fallback (`herdr/workflow_progress.py#L249`) and `_identity_reason` (`herdr/workflow_progress.py#L195`), `task.get('integrated_commit')` is completely omitted from the candidate comparison.
  5. If the proposed design is implemented as written, it will add redundant checks to an already functional branch while leaving the hardcoded `'implementation'` check intact. Flagship workflows will continue to fail with `affected_tasks_unknown` -> `waiting_human`.
* **Recommendation**:
  - Delete the proposal to add `integrated_commit` to `candidate_responsible_source` (it's already there).
  - Solve the real architectural defect: dynamically resolve the target node using the gate's `retry_node` configuration from DAG config instead of hardcoding `'implementation'`, and add `integrated_commit` to `assess_workflow#L249` and `_identity_reason#L195`.

---

#### Finding 5: Misplaced Domain Responsibility in Agent Router (`MAX_REMEDIATION_ROUNDS = 2`)
* **Severity**: **Major**
* **Affected Scope**: Technical Plan §2.4 (Lines 124–127), Detailed Implementation Plan §2 (Lines 271–274), Goal 2 (Lines 33–34)
* **Simplicity Rationale & Architectural Assessment**:
  1. The proposal injects `MAX_REMEDIATION_ROUNDS = 2` and a new `RouterRemediationExhausted` exception into `herdr/agent_router.py`.
  2. The Router's architectural role is stateless candidate selection and capacity matching for a single task dispatch.
  3. Retry budgets, iteration limits, and workflow-level halting belong to `herdr/node_capacity.py` (`max_tasks_per_node`, `launch_capacity_error`) and `herdr/workflow_progress.py#L269-L279` (`verifier_budget_exhausted`).
  4. Placing workflow iteration limits inside `agent_router.py` creates duplicate, conflicting budget accounting and violates the single responsibility principle.
* **Recommendation**:
  - Keep `agent_router.py` strictly focused on agent selection.
  - Enforce remediation limits through existing progress and capacity controls (`node_capacity.py` and `workflow_progress.py`), respecting template-configured bounds (`max_tasks_per_node`).

---

#### Finding 6: Narrow Teardown Symptom Patch Leaves Standard Test Artifacts Vulnerable to Lockout
* **Severity**: **Major**
* **Affected Scope**: Technical Plan §4 (Lines 157–168), Detailed Implementation Plan §1 (Lines 260–265), Goal 5 (Line 36), Open Question 2 (Lines 49–50)
* **Simplicity Rationale & Architectural Assessment**:
  1. The proposal only adds `"test-evidence"` to `BYPASS_IGNORED_DIRS` in `herdr/clone_archive.py`.
  2. In modern Python and Node testing environments (e.g. Playwright, Jest, Pytest-cov), test runs standardly generate `test-results/`, `playwright-report/`, `.coverage`, `coverage/`, and `htmlcov/`.
  3. Whitelisting only `"test-evidence"` is an ad-hoc band-aid tailored to a single test run; the very next run generating Playwright or coverage artifacts will fail teardown with exit code 2 and lock the workstation again.
* **Recommendation**:
  - Expand `BYPASS_IGNORED_DIRS` in `herdr/clone_archive.py` in one shot with standard transient test/coverage directories:
    ```python
    "test-evidence", "test-results", "playwright-report", "coverage", ".coverage", "htmlcov"
    ```
  - This provides a robust, generalized solution without requiring speculative template configuration schemas.

---

#### Finding 7: Misattributed Modification Target for `_note_close_backoff`
* **Severity**: **Nit**
* **Affected Scope**: Technical Plan §5.2 (Lines 180–182), Detailed Implementation Plan §6 (Lines 302–304)
* **Simplicity Rationale & Architectural Assessment**:
  - The plan targets `services/controller/workflow_close.py` for modifying `_note_close_backoff`. In reality, `_note_close_backoff` is called via the controller ports adapter (`ports._note_close_backoff`). The actual wiring and attention episode posting require coordination with `services/herdr-controller.py`.
* **Recommendation**:
  - Note in the implementation plan that `services/herdr-controller.py` must be updated alongside `workflow_close.py` to support attention episode posting on repeated closeout failures.

---

### Clean Architectural Path Forward

Before proceeding to Step 9 (Implementation Analysis), the design document must be revised to adopt this minimalist, sound architecture:

| Component | Broken Proposal | Clean Minimalist Architecture |
| :--- | :--- | :--- |
| **Closeout Status** | Mutate `workflow.status = 'closing'` in `advancement.py` | Keep `workflow.status = 'running'` until closeout finishes. Use `ports._workflow_close_inflight` or attention episodes for closeout visibility. |
| **Agent Isolation** | Inverted 2-agent role alternation (`[A, B] -> [B, A]`) | Scope verifier exclusion strictly by candidate author: Author A fixes Round 2, Reviewer B re-verifies Round 2. Alternation between authors and verifiers remains natural and independent. |
| **Predecessor Re-plan** | Unconditional `superseded` + 15s debounce + fake `generation_id` | Check started task status before superseding; wait for terminal worker state or issue cancellation. Remove debounce and CAS `generation_id`. |
| **Defect Attribution** | Add `integrated_commit` to `candidate_responsible_source` (already there) | Fix hardcoded `'implementation'` in `workflow_progress.py#L142` to match gate `retry_node` (`'craft'`), and add `integrated_commit` to `assess_workflow#L249`. |
| **Remediation Cap** | `MAX_REMEDIATION_ROUNDS = 2` in `agent_router.py` | Enforce limits via existing `node_capacity.py` (`max_tasks_per_node`) and `workflow_progress.py` (`verifier_budget_exhausted`). |
| **Teardown Whitelist** | Narrowly whitelist `"test-evidence"` | Whitelist standard test/coverage directories in `BYPASS_IGNORED_DIRS` (`test-evidence`, `test-results`, `playwright-report`, `coverage`, `htmlcov`). |
