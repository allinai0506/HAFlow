# HAFlow Action Protocol V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use inline execution with TDD checkpoints.

**Goal:** Persist and execute only VERIFY/RETRY Supervisor decisions through one crash-safe, run-scoped, idempotent Controller protocol.

**Architecture:** Keep facts in the existing SQLite state database. Add a small Intervention projection with a unique decision identity and lifecycle events; the Supervisor requests an Intervention, while the Controller claims and executes it. RETRY reuses the legal `rework` transition. VERIFY triggers the existing verification/rework path and never writes a verification verdict.

**Tech Stack:** Python standard library, SQLite WAL/transactions, existing StateStore, Controller, Trajectory and pytest.

## Global Constraints

- Only VERIFY and RETRY are executable in V1.
- Supervisor code must not transition tasks, launch agents, or modify workflows.
- One canonical Intervention per `(run_id, decision_id, action, task_id)`.
- Existing SQLite StateStore remains the only runtime database.
- `HERDR_SUPERVISOR_ENABLED=0` and `enforce=false` remain behavior-neutral.
- No production deployment, merge, push, or service restart.

### Task 1: Intervention model and atomic SQLite storage

**Files:**
- Create: `herdr/intervention.py`
- Modify: `herdr/state_db.py`
- Modify: `herdr/state_store.py`
- Test: `tests/test_intervention_store.py`

**Interfaces:**
- `Intervention.from_mapping()` / `.to_mapping()` validate action and status.
- `StateStore.create_intervention(...)` atomically returns the canonical row.
- `StateStore.claim_intervention(intervention_id)` atomically changes requested→running.
- `StateStore.complete_intervention(...)` and `fail_intervention(...)` persist result/error and lifecycle events.
- `StateStore.list_interventions(run_id=None, task_id=None, statuses=None)` reads only scoped rows.

- [ ] Write failing tests for schema, round-trip serialization, duplicate identity, invalid transition, and two SQLite connections concurrently creating one Intervention.
- [ ] Run `pytest tests/test_intervention_store.py -q`; expect failures because the model/API/table do not exist.
- [ ] Add the `interventions` table in the existing `_ensure_schema`, with `identity_key UNIQUE`, run/task fields, provenance JSON, timestamps, status, attempt budget, result and error.
- [ ] Implement transaction-local `BEGIN IMMEDIATE` create/claim/finalize operations and append `intervention_requested/started/completed/failed` to the existing events table.
- [ ] Re-run the store tests and confirm one canonical row under concurrent creation.

### Task 2: Supervisor-to-Intervention request boundary

**Files:**
- Modify: `herdr/intervention.py`
- Modify: `herdr/supervisor/harness.py`
- Modify: `tests/test_supervisor_interception.py`
- Create: `tests/test_action_protocol.py`

**Interfaces:**
- `request_intervention(store, task, evaluation, decision, config)` validates run ownership and returns canonical Intervention or `None` in observe-only mode.
- Harness invokes this request boundary only for enforced VERIFY/RETRY; it still never calls Task transitions.

- [ ] Add RED tests for enforce=false, disabled supervisor, provenance, stable identity, duplicate decision, and unsupported actions.
- [ ] Run the focused tests and verify they fail for missing durable Intervention behavior.
- [ ] Add the request boundary and have harness return the canonical intervention mapping with the decision.
- [ ] Preserve existing `pending_intervention()` compatibility by reading durable status first and falling back only to legacy policy events.
- [ ] Re-run focused Supervisor tests.

### Task 3: Controller execution and recovery

**Files:**
- Modify: `services/herdr-controller.py`
- Modify: `herdr/intervention.py`
- Test: `tests/test_action_protocol.py`
- Test: `tests/test_supervisor_interception.py`

**Interfaces:**
- `_execute_intervention(task, intervention, store)` is the only Controller action executor.
- `_supervisor_retry` uses the existing legal rework transition and records previous/new status and attempt count.
- `_supervisor_verify` enters the existing verification/rework verification route without writing a verdict.
- `recover_pending_interventions(store, run_id=None)` claims and resumes requested/running rows with run/task checks.

- [ ] Add RED integration tests for RETRY completion, VERIFY trigger without fake pass, action exception→failed, budget rejection, cross-run rejection, completed replay, crash recovery, and the full Controller checkpoint chain.
- [ ] Add atomic claim before execution; make retry execution detect an already-applied rework transition after crash before attempting it again.
- [ ] Enforce `attempt_count >= max_attempts` as a durable rejection with machine-readable error and no rework transition.
- [ ] Route VERIFY through the current legal verification/rework flow and wait for `verification_completed` as a separate fact.
- [ ] Call recovery before normal done redelivery/checkpoint fallback so RateGate cannot bypass durable pending work.
- [ ] Run the integration tests against temporary SQLite state and controlled task/workflow records.

### Task 4: Documentation, metrics decision, and regression coverage

**Files:**
- Create: `docs/architecture/action-protocol.md`
- Modify: `wiki/log.md`
- Modify: `docs/lessons/lessons-learned.md` only if a general concurrency/recovery lesson is established
- Modify: `tests/test_action_protocol.py`

- [ ] Document only implemented V1 lifecycle, VERIFY/RETRY semantics, identity, recovery, budget, run isolation and failure behavior.
- [ ] Add metrics only if sourced from persisted Intervention rows; otherwise document metrics as follow-up without speculative counters.
- [ ] Run targeted tests, full pytest, compileall, executable Python syntax checks and `git diff --check`.

### Task 5: S4 cleanup and S5/S6 evidence

**Files:**
- Scope cleanup to the changed Python and test files only.
- Create: `.omc/verify-01a2-action-protocol-v1.md`
- Create: `.omc/review-01a2-action-protocol-v1.md`

- [ ] Run `ai-slop-cleaner` Mode B on changed files; remove only dead/duplicate wrappers introduced by this task.
- [ ] Re-run all fresh verification commands after cleanup.
- [ ] Perform independent read-only review covering design, concurrency, run ownership, fail-safe and scope.
- [ ] Record `MERGE_READY` only if every required acceptance item has fresh evidence; otherwise record blockers and do not deliver.
