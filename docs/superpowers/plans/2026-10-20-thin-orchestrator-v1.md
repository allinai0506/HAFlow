# Thin Orchestrator V1 Implementation Plan

> **For agentic workers:** Execute inline under one controller. Do not dispatch nested implementation controllers. Use RED→GREEN per task and preserve HAFlow's durable dispatch, gate, and recovery contracts.

**Goal:** Allow a deterministic three-node workflow to launch and advance without a coordinator Agent/Pane while preserving worker autonomy, evidence gates, human decisions, idempotency, and restart recovery.

**Architecture:** Keep `herdr.node_dispatch_store` as the durable dispatch authority and `herdr.direct_dispatch` as the pure planner. Change only Controller routing so a ready, fully specified agent node attempts direct dispatch before consulting coordinator transport. Coordinator remains a compatibility/fallback path for genuinely non-deterministic or incomplete cases.

**Tech Stack:** Python 3, SQLite StateStore/recovery operations, existing `herdr-task launch`, pytest.

## Global Constraints

- Do not add a scheduler, state machine, Agent, framework, or LLM decision path.
- Do not change Agent Adapter or Router selection semantics.
- Do not weaken artifact, gate, candidate identity, lineage, or completion checks.
- Do not merge or deploy; delivery ceiling is a reviewable PR.
- Use `general-task-v1` unchanged as the selected validation fixture.

## Current dependency audit

| Path | Current behavior and evidence | LLM required? | Deterministic? | V1 |
| --- | --- | --- | --- | --- |
| `check_workflow_stage_advance` | After durable recovery reconciliation, lines 101–107 return early and defer all node operations as `coordinator_missing` when no coordinator pane exists. | No; readiness and dispatch operation already come from persisted facts. | Yes. | Modify. |
| `try_direct_stage_advance` | Lines 25–27 require both `project_root` and `coordinator_pane_id`, although launch uses `project_root`, Router and `herdr-task`; pane is not consumed. | No. | Yes. | Modify. |
| `_handle_stage_advance_item` first node | Lines 46–74 route `stage=start` to coordinator intake before trying the direct planner. Only after repeated coordinator stalls does it direct-dispatch. | Only when inputs/config are incomplete or ambiguous; not for a complete node contract. | Yes when planner returns dispatch/wait. | Modify ordering. |
| Direct-dispatch failure | Planner fallback, join errors, blocked verdicts and launch failures return `False`; stage delivery then uses one coordinator fallback and ultimately records attention/defer. Started durable operations never fall through to a second transport. | Sometimes; current reasons are conflated at final coordinator lookup. | Transient launch/recovery is deterministic; business adjudication is not. | Preserve durable recovery; make missing coordinator a fallback-specific reason, not a global prerequisite. |
| Continuation | `inspect_continuation` detects unresolved integration/task obligations; handler explicitly prompts coordinator to resolve missing work/adoption. It is not the normal ready-node dispatcher. | Potentially yes because it covers ambiguous missing work and integration adoption. | Some subcases may later be automated, but not needed for A→B→C dispatch. | No source change; regression evidence only. |
| Auto verdict | Existing machine-readable gate verdict is adopted without coordinator when valid; missing/invalid verdict remains fail-closed. | No for valid contract; yes/human for unresolved failures. | Yes. | Regression evidence only. |

## Runtime flow after V1

```text
persisted workflow + tasks + gate facts
                |
                v
Controller readiness sweep
  | reconcile recovery + node dispatch operations
  | validate dependencies / gate / candidate identity
  v
queue one stage_advance with durable operation lease
                |
                v
direct planner first (root and downstream nodes)
  | dispatch/wait -> existing Router -> herdr-task launch -> Worker
  | fallback     -> coordinator transport if live
  | no coordinator -> persisted bounded defer / human decision
                |
                v
task evidence + artifact/gate validation -> next sweep
```

### Task 1: RED tests for coordinator-independent dispatch

**Files:**
- Modify: `tests/test_direct_stage_dispatch.py`
- Modify: `tests/test_scheduler_dispatch_e2e.py`
- Modify: `tests/test_coordinator_auto_heal.py`

**Interfaces:**
- Consumes: `check_workflow_stage_advance(workflow_id)`, `_handle_coordinator_item(item)`, `try_direct_stage_advance(item)`.
- Produces: regression contracts for missing coordinator pane, first-node direct dispatch, fallback blocking, duplicate events and restart reconciliation.

- [ ] Add a direct-dispatch test whose project has `project_root`, sufficient requirement/config, and no `coordinator_pane_id`; assert one `herdr-task launch` and no coordinator prompt.
- [ ] Add a first-node stage-delivery test that asserts direct dispatch is attempted before coordinator intake and that success sends no LLM prompt.
- [ ] Add a sweep test with a persisted running workflow and no coordinator pane; assert the ready node is queued and its operation is not deferred as `coordinator_missing`.
- [ ] Add a fallback test where the deterministic planner refuses and no coordinator exists; assert bounded persisted defer/attention and no false completion.
- [ ] Run the new tests and record the expected failures against `1ddffd1`.

### Task 2: Minimal Controller routing change

**Files:**
- Modify: `services/controller/advancement.py`
- Modify: `services/controller/direct_dispatch.py`
- Modify: `services/controller/stage_delivery.py`

**Interfaces:**
- Consumes: existing `node_dispatch_store.operation_for_node/claim/start/direct_finished/transport_finished/defer` contracts.
- Produces: coordinator-independent deterministic path without changing planner, Router, Adapter or task lifecycle.

- [ ] Remove the global coordinator-pane early return from the node-based readiness sweep; retain coordinator lookup only in the legacy/fallback transport path.
- [ ] Remove `coordinator_pane_id` from direct-dispatch eligibility while keeping `startup_ready`, `project_root`, node contract, gate, join, candidate and launch checks.
- [ ] In stage delivery, attempt direct dispatch first for every agent node, including `stage=start`; invoke coordinator intake/fallback only when the planner returns `False`.
- [ ] Preserve the started-operation rule: a transport that may have created resources must never fall through to a second transport.
- [ ] Emit/record precise fallback reason at the existing attention/defer boundary without inventing defaults or infinite retry.
- [ ] Run Task 1 tests until GREEN.

### Task 3: Reliability and compatibility matrix

**Files:**
- Modify: `tests/test_direct_stage_dispatch.py`
- Modify: `tests/test_scheduler_dispatch_e2e.py`

**Interfaces:**
- Consumes: durable operation identity, lease, generation, predecessor runs and launch receipts.
- Produces: proof for scenarios C–E without relaxed assertions.

- [ ] Acceptance failure: persist a blocked/invalid gate result and assert downstream is not queued or launched.
- [ ] Human decision: use a non-agent or incomplete-contract node and assert explicit waiting/attention with a resumable operation.
- [ ] Duplicate completion/event: deliver the same trigger twice and assert one operation/task inventory.
- [ ] Restart recovery: recreate the Controller module/store over the same temporary SQLite database and assert the pending operation resumes once.
- [ ] Re-run the six requested regression modules.

### Task 4: Isolated three-node workflow evidence

**Files:**
- Create: `docs/walkthroughs/thin-orchestrator-v1.md`

**Interfaces:**
- Consumes: `general-task-v1` nodes `intake_and_scoping → deep_execution → review_and_delivery`.
- Produces: workflow/task identities, Worker execution traces, handoff evidence, deliverable path, acceptance facts and coordinator LLM call count.

- [ ] Start with temporary HOME/state DB/workflow docs and a controlled local project directory; leave production state untouched.
- [ ] Run the actual Controller and existing `herdr-task`/Agent path with the coordinator unavailable.
- [ ] Capture each node launch/end, upstream context in the successor prompt, final artifact and workflow close evidence.
- [ ] Run invalid-artifact, human-decision, duplicate-event and process-restart variants against the same real persistence boundary.
- [ ] If a real Agent cannot run, record the exact environment blocker and reproduction command; never label simulated transport as real Agent E2E.

### Task 5: Cleanup, verification, review and PR

**Files:**
- Modify: `wiki/log.md`
- Create: `.omc/verify-01a159d2-e8d4-7411-9391-11652d0899ec.md`
- Create: `.omc/review-01a159d2-e8d4-7411-9391-11652d0899ec.md`

**Interfaces:**
- Consumes: final diff and fresh post-cleanup evidence.
- Produces: `MERGE_READY` review artifact and one PR; no merge/deploy.

- [ ] Run `ai-slop-cleaner` Mode B and remove only actual redundant code introduced or exposed by this change.
- [ ] Run targeted pytest, compileall, bin script syntax/CLI checks, `git diff --check`, and isolated E2E.
- [ ] Record commands, exit codes, SHA, limits and E2E status in the verification artifact.
- [ ] Run health comparison, Google-style review and spec review; fix findings through S4→S5→S6, maximum three rounds.
- [ ] Append one concise verified-behavior entry to `wiki/log.md`; do not expand architecture or lessons without a separately proven need.
- [ ] Commit, push and create the focused PR only after `MERGE_READY`; inspect CI and review feedback until closed or explicitly blocked.

## GSTACK REVIEW REPORT

| Run | Status | Findings |
| --- | --- | --- |
| Scope challenge | PASS | User accepted the eight-file minimum; templates, `herdr/` core, continuation, architecture guide and lessons remain read-only. |
| Architecture | PASS | Reuses durable node-dispatch operations and existing direct planner; no parallel scheduler or state machine. Legacy fallback remains isolated. |
| Code quality | PASS | Three small Controller edits express the boundary directly; no new classes, services or dependencies. |
| Tests | PASS | RED cases cover no-pane root/downstream dispatch, fail-closed fallback, gate failure, human decision, duplicate events and restart recovery. |
| Performance | PASS | Removes an LLM/pane dependency from the hot path without adding polling, database scans or external calls. |
| Outside voice | PASS_WITH_NOTE | The referenced external article was unavailable; review is grounded in the user specification and current repository behavior. |

Test diagram:

```text
ready + valid contract + no coordinator --> exactly one Worker launch --> evidence --> next node
ready + invalid/incomplete contract       --> no launch --> persisted wait/human decision
blocked gate                              --> no downstream launch
duplicate event / restart                 --> same durable operation --> no duplicate task
uncertain launch side effects             --> awaiting_result --> never second transport
```

VERDICT: APPROVED FOR RED-FIRST IMPLEMENTATION

NO UNRESOLVED DECISIONS
