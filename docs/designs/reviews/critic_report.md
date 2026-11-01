# Critic Review Report (Stage 7)

**Target Document**: [`docs/designs/workflow-autonomy-and-blocker-remediation.md`](workflow-autonomy-and-blocker-remediation.md)  
**Target Scope**: FeatureRange (Lines 1..252)  
**Reviewers**: `egm-design` (Hostile Design Critic), `egm-structural` (Document Structural Architect), `egm-style` (Technical Copy Editor)  
**Standards Applied**: `resources/document_standards/feature-spec-standard.md`, Technical Style & Objectivity Rules

---

## Executive Summary

The antagonistic review swarm identified **3 Blocker**, **8 Major**, and **7 Nit** findings across architecture, structural compliance, and technical style. 

The most critical architectural finding is that the proposal contained three systemic dead-ends:
1. **Irreversible Closeout Dead-End**: Setting `workflows.status = 'closing'` permanently freezes reconciliation because the controller advancement sweep strictly gates on `status == 'running'`.
2. **Algorithmic Contradiction in Role Alternation**: The proposed `[A, B] -> [B, A]` role alternation fails algorithmically at Round 2 start (empty used-agents pool causes default preference to re-pick A anyway), while forcing Reviewer B to fix Author A's code and Author A to review their own underlying feature. Instead, Author A should fix Round 2 while Reviewer B re-verifies, preserving verifier independence naturally.
3. **Non-Existent Database Column**: `workflow.generation_id` does not exist in the SQLite `workflows` table schema; executing a CAS update on it will raise `OperationalError`.

---

## Findings by Reviewer

### 1. Architecture & System Critic (`egm-design`)

#### Finding DES-1: Premature State Mutation to `'closing'` Creates an Unrecoverable State Trap
* **Severity**: 🔴 **Blocker**
* **Affected Scope**: Lines 174–183 (Subsystem 4), Line 37 (Goal 6)
* **Description**:
  1. According to `herdr/transitions.py#L150-L158`, legal transitions from `'closing'` are strictly `{"completed", "failed"}`. There is no legal transition edge back to `'running'` or `'waiting_human'`.
  2. In `services/controller/advancement.py#L97`, the main sweep loop gates advancement on `if record and record.get('status') == 'running':`. Once a workflow is `'closing'`, subsequent controller sweeps completely ignore it.
  3. `workflow_progress.py#L266` and `node_dispatch.py#L24` treat non-`running` workflows as inactive.
  4. If the background closeout process is deferred (e.g. waiting human approval on Git commits) or enters exponential backoff, no future sweep will ever reconcile the workflow. Transitioning back to `'running'` raises `InvalidTransitionError`.
* **Remedy**:
  - Keep `workflows.status` as `'running'` until `close_workflow` actually exits 0 and records delivery receipt.
  - Track in-flight closeout via an in-flight operation/claim or runtime observation (`ports._workflow_close_inflight`), rather than mutating top-level lifecycle state.

#### Finding DES-2: Inverted "Role Alternation" State Machine is an Algorithmic Contradiction and Quality Anti-Pattern
* **Severity**: 🔴 **Blocker**
* **Affected Scope**: Lines 110–128 (Subsystem 1), Line 32 (Goal 1), Lines 53–61 (CUJ 1)
* **Description**:
  1. *Algorithmic Contradiction*: The spec states: "仅排除在当前轮次内已参与该阶段或互斥阶段的 Agent". At the start of Round 2, no tasks have run in Round 2 yet, so `stage_used_agents` is empty. The router's static stage preference will simply pick Agent A again.
  2. *Quality Anti-Pattern*: Forcing Reviewer B to fix Author A's code while Author A reviews Reviewer B's fix leads to Author A reviewing code where 95%+ was authored by Author A.
  3. *True Verifier Invariant*: HAFlow's rule is that *a verifier node cannot be assigned to the author of the candidate being verified*. Implementation nodes are NOT verifiers and have no exclusion on who fixes the code. Author A fixing their own bug in Round 2, followed by Reviewer B re-verifying Round 2, is strictly compliant with verifier independence.
* **Remedy**:
  - Eliminate the inverted rotation. Author A fixes their own code in Round 2, and Reviewer B re-verifies in Round 2.
  - Scope verifier exclusion by `dispatch_round`: when Verifier V is dispatched for round $k$, exclude only agents who authored code in round $k$.

#### Finding DES-3: Unbacked Schema Assertion: Non-Existent Column `workflow.generation_id`
* **Severity**: 🔴 **Blocker**
* **Affected Scope**: Lines 148–150 (Subsystem 2)
* **Description**:
  The spec requires CAS validation and incrementing `workflow.generation_id`. In `herdr/state/schema.py#L29-L40`, the `workflows` table schema does not have a `generation_id` column. CAS update against it will raise `sqlite3.OperationalError: no such column: generation_id`.
* **Remedy**:
  Base generation fencing on existing task/operation detail metadata (`replan_attempts` counter), or define a formal schema migration.

#### Finding DES-4: Candidate Attribution Hardcodes `'implementation'`, Breaking Custom DAGs
* **Severity**: 🟡 **Major**
* **Affected Scope**: Lines 135–139 (Subsystem 2)
* **Description**:
  In `herdr/workflow_progress.py#L142`, `candidate_responsible_source` hardcodes `if (task.get('node') or task.get('stage')) != 'implementation': continue`. In production templates like `code-craft-v1.yaml`, the implementation node is named `craft`. This causes `candidate_responsible_source` to return `(None, [])`, triggering `affected_tasks_unknown` and dumping the workflow into `waiting_human`. Also, line 195 `_identity_reason` omits `integrated_commit`.
* **Remedy**:
  Dynamically match against `retry_node` from gate definition (or filter by `not is_verifier_node(node)`), and include `integrated_commit` in `_identity_reason`.

#### Finding DES-5: Unconditional `'superseded'` on Started Operations Risks Orphaned Processes
* **Severity**: 🟡 **Major**
* **Affected Scope**: Lines 141–147 (Subsystem 2)
* **Description**:
  `operation['started'] == True` means a worker process or tmux pane is already running. Transitioning immediately to `'superseded'` without cancelling or waiting for worker cessation causes concurrent execution in the same workstation clone, causing `.git/index.lock` collisions and dirty commits.
* **Remedy**:
  Check if the started task has settled or issue a cancellation/interrupt before superseding.

#### Finding DES-6: Overly Narrow Whitelist for Teardown Bypass
* **Severity**: 🟡 **Major**
* **Affected Scope**: Lines 153–171 (Subsystem 3), Lines 49–50 (Open Question 2)
* **Description**:
  Whitelisting only `"test-evidence"` leaves the door open for Playwright and standard coverage runs (`playwright-report`, `test-results`, `coverage`, `.coverage`, `htmlcov`) to trigger exit code 2.
* **Remedy**:
  Expand built-in `BYPASS_IGNORED_DIRS` to include standard test/browser artifacts: `{"test-evidence", "test-results", "playwright-report", "coverage", ".coverage", "htmlcov"}`.

#### Finding DES-7: Missing Failure Modes & Edge-Case Bounds in Risks Section
* **Severity**: 🟡 **Major**
* **Affected Scope**: Feature Specification Standard compliance
* **Description**:
  Lacks structured failure mode mitigation for: remediation rounds exceeding limits across cascading verifiers, debounce delays during deadlocks, and recovery paths if replan attempts exceed caps.
* **Remedy**:
  Add an explicit "Risks & Limits / Mitigations" section.

---

### 2. Document Structural Architect (`egm-structural`)

#### Finding STR-1: Missing Explicit `Risks & Limits` Heading
* **Severity**: 🟡 **Major**
* **Affected Scope**: Lines 235–250 (`## Non-Functional Requirements`)
* **Description**: Feature Spec Standard mandates an explicit `Risks & Limits` (or `Risks & Mitigations`) section detailing failure modes, security implications, and operational limits alongside mitigations.
* **Remedy**: Align heading to `## Risks & Limits / Non-Functional Requirements` with a dedicated failure modes matrix.

#### Finding STR-2: Inconsistent Markdown Link Compliance
* **Severity**: 🟡 **Major**
* **Affected Scope**: Lines 141, 158, 180
* **Description**: While lines 137 and 179 use clickable `file:///` links with line anchors, lines 141, 158, and 180 reference source files and line ranges as plain inline backticks.
* **Remedy**: Convert all source file references to clickable markdown links.

#### Finding STR-3: Heading Taxonomy and Numbering Dissonance
* **Severity**: 🟢 **Nit**
* **Affected Scope**: Lines 74–182
* **Description**: Section 1 is `### 1. 系统端到端架构...`, but subsequent sections are `### 2. 子系统一：...`, `### 3. 子系统二：...`.
* **Remedy**: Unify numbering format.

#### Finding STR-4: Asymmetrical Alternative Enumeration
* **Severity**: 🟢 **Nit**
* **Affected Scope**: Lines 189–232
* **Description**: Sections 3 and 4 have only `Alternative 3A` and `4A` without `B`, and line 205 introduces raw code syntax `(`git log -1`)` into heading.
* **Remedy**: Rename subheadings to descriptive titles and balance enumeration.

#### Finding STR-5: Inconsistent Thematic Breaks
* **Severity**: 🟢 **Nit**
* **Affected Scope**: Lines 7, 70, etc.
* **Description**: Mixture of 80-character hyphens and standard 3-character hyphens `---`.
* **Remedy**: Standardize on `---`.

---

### 3. Technical Copy Editor (`egm-style`)

#### Finding STY-1: Anthropomorphism and Colloquial Slang
* **Severity**: 🟡 **Major**
* **Affected Scope**: Lines 15, 24, 28
* **Description**: Uses emotional slang: "弃疗挂起", "无条件甩给操作人", "假死悬挂".
* **Remedy**: Replace with precise technical phrasing ("转入人工介入等待 (`waiting_human`)", "导致工作流停滞无进展").

#### Finding STY-2: Promotional Hyperbole & Dev-Chat Fluff
* **Severity**: 🟡 **Major**
* **Affected Scope**: Lines 36, 157
* **Description**: "极简消除", "1 行代码打通", "确保工位释放零阻碍".
* **Remedy**: Replace with factual requirement definitions ("扩展清理忽略目录白名单以保证工位正常销毁").

#### Finding STY-3: Dramatic Metaphors & Alarmist Buzzwords
* **Severity**: 🟡 **Major**
* **Affected Scope**: Lines 33, 34, 127, 207, 221, 243
* **Description**: "无限 Token 乒乓燃烧", "替罪羊误杀", "打爆磁盘", "烧穿 API Quota".
* **Remedy**: Replace with measurable technical limits ("避免因循环重试产生非预期 Token 消耗", "避免误打回非责任任务", "磁盘耗尽", "超出 API 配额").

#### Finding STY-4: Pejorative Framing in Alternatives
* **Severity**: 🟡 **Major**
* **Affected Scope**: Lines 219–231
* **Description**: "严重过度设计（YAGNI）", "本就是临时丢弃物", "幽灵特性（Ghost Feature）".
* **Remedy**: State technical trade-offs objectively without caricaturing rejected alternatives.

#### Finding STY-5: Brand Slogans and Unprovable Absolutes
* **Severity**: 🟢 **Nit**
* **Affected Scope**: Lines 11, 21, 41, 59, 143, 242
* **Description**: "核心理念是...", "全自动向前流转", "全绿通过", "绝对不允许", "坚决禁止".
* **Remedy**: Use standard, neutral active voice technical language.
