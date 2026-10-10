# Stale Dispatch Partial Launch Recovery Implementation Plan

> For agentic workers: Execute inline with `executing-plans`; use one controller and preserve the existing workflow and candidate.

**Goal:** Let an operator safely retire a stale dispatch whose private Agent pane started but never received a task, so the current candidate can be dispatched without weakening unknown-delivery protection.

**Architecture:** Keep workflow and dispatch facts in the existing SQLite recovery ledger. Add a distinct, audited abandonment path that proves exact task/intent/pane identity, requires an explicit operator confirmation, closes and archives only the abandoned private launch, then atomically retires the stale dispatch and creates a new current-generation dispatch obligation. Normal recovery and automatic retry continue to fail closed.

**Tech Stack:** Python standard library, SQLite, existing Herdr pane API, pytest.

## Global Constraints

- Keep `unknown delivery` fail-closed; do not add timeout-based retries or clear notification flags.
- Never classify a started Agent as absent from idle status alone.
- Require an explicit operator decision and record operator, reason, candidate, operation version, launch intent, terminal, and pane identity.
- Preserve the abandoned clone as an archive; do not delete it.
- Recheck candidate, operation version, Task inventory, intent identity, and native resource identity before and after external pane operations.
- Tests use temporary databases, directories, and fake native runners; never start a real Agent or modify production controller state.
- Preserve current workflow candidate `cb5e8be091eccfee4a3a2885485ea82ebe3013dc`; do not reopen the workflow.

## Files and Responsibilities

- `herdr/task_resources.py`: exact-identity inspection, idempotent close/archive, and recovery of a partial dynamic launch.
- `herdr/dispatch_recovery.py`: expose the explicit stale-partial-launch decision and persist its CAS-bound audit receipt.
- `herdr/node_dispatch_store.py`: ignore only stale operations retired by that exact audited decision; continue blocking every other unresolved prior delivery.
- `bin/herdr-task`: provide an operator CLI action with expected operation version, current candidate, operator, reason, and explicit abandonment confirmation.
- `console/herdr_factory_console.py`: project the action only when its exact preconditions hold and submit the same bounded decision payload.
- `tests/test_dispatch_recovery_ui.py`: state-machine and audit contract tests.
- `tests/test_launch_reconcile_cli.py`: CLI identity, confirmation, archive, retry, and refusal tests.
- `tests/test_node_dispatch_contract.py`: prove a new candidate dispatch proceeds only after the prior stale launch has been explicitly retired.
- `tests/test_scheduler_dispatch_e2e.py`: verify the controller registers one current-candidate Test task and does not duplicate or skip Review.

## Task 1: Specify and reproduce the cross-generation deadlock

- [ ] **Step 1: Add a failing temporary-database scenario** to `tests/test_dispatch_recovery_ui.py`: current workflow candidate is `sha-new`; old started Test dispatch belongs to `sha-old`; its launch intent has a private dynamic pane and no registered Task; a new Test operation exists and is blocked by the old dispatch.
- [ ] **Step 2: Assert existing recovery stays fail-closed**: ordinary `retry`, `verify`, and automatic reconciliation cannot retire the old started dispatch or launch the new Test task.
- [ ] **Step 3: Run the focused test** with `pytest -q tests/test_dispatch_recovery_ui.py -k partial_launch`; confirm it fails because no audited recovery action is exposed for the stale generation.

## Task 2: Implement an audited partial-launch abandonment primitive

- [ ] **Step 1: Add tests before code** in `tests/test_launch_reconcile_cli.py` for exact private clone/pane/terminal match, no registered Task, no Agent session, startup-only transcript, changed identity refusal, active/session-bearing Agent refusal, and idempotent retry after pane close or clone archival.
- [ ] **Step 2: Run** `pytest -q tests/test_launch_reconcile_cli.py -k partial_launch`; confirm the new contract fails before implementation.
- [ ] **Step 3: Implement a bounded primitive** in `herdr/task_resources.py` that accepts the expected intent ID, task ID, workflow/node, candidate SHA, terminal ID, pane ID, and explicit operator attestation. It must re-read the exact native pane and launch tag, refuse any registered Task/session/non-startup transcript/foreign cwd, persist a recovery phase before closing the pane, archive the clone without deleting it, and return evidence rather than claiming dispatch completion.
- [ ] **Step 4: Re-run** `pytest -q tests/test_launch_reconcile_cli.py -k partial_launch`; all cases must pass with native operations injected through a fake runner.

## Task 3: Retire stale responsibility and create current responsibility atomically

- [ ] **Step 1: Add a failing recovery-ledger test** in `tests/test_dispatch_recovery_ui.py` that supplies exact expected versions for the current operation and old operation, plus the partial launch identity; assert stale candidate, changed intent, Task registration, and operation-version races cause zero state changes.
- [ ] **Step 2: Run** `pytest -q tests/test_dispatch_recovery_ui.py -k stale_partial`; confirm the recovery action is unavailable or rejected before implementation.
- [ ] **Step 3: Add the explicit operator action** in `herdr/dispatch_recovery.py`. Persist the human decision separately from machine inventory evidence. After the native resource primitive returns its exact archive receipt, use one SQLite transaction to recheck current candidate/operation version, old operation version, absence of a registered Task, and intent receipt; then retire the old operation and unblock/create one current-candidate dispatch operation.
- [ ] **Step 4: Update `herdr/node_dispatch_store.py`** so only the old operation carrying this exact retirement receipt stops blocking `_prior_delivery_unknown`; do not treat generic `waiting_human`, timeout, missing pane, or idle status as retired.
- [ ] **Step 5: Re-run** `pytest -q tests/test_dispatch_recovery_ui.py -k stale_partial tests/test_node_dispatch_contract.py`; verify pass and fail-closed race cases.

## Task 4: Wire the operator entry point and controller continuation

- [ ] **Step 1: Add CLI contract tests** proving missing confirmation, missing reason, stale candidate, stale operation version, and foreign intent are rejected before any pane operation.
- [ ] **Step 2: Add the CLI action** to `bin/herdr-task` and the corresponding Console action to `console/herdr_factory_console.py`; both call the same core function and record the same receipt.
- [ ] **Step 3: Add an end-to-end scheduler test** that executes the authorized partial-launch retirement, performs one current-SHA Test dispatch registration, and verifies Review remains pending until Test produces a real gate verdict.
- [ ] **Step 4: Run** `pytest -q tests/test_dispatch_recovery_ui.py tests/test_launch_reconcile_cli.py tests/test_node_dispatch_contract.py tests/test_scheduler_dispatch_e2e.py`; report actual pass/fail/skip counts.
- [ ] **Step 5: Run repository checks**: `python3 -m compileall -q herdr services bin tests`, `python3 -m py_compile bin/herdr-task`, and `git diff --check`.

## Task 5: Independent review and local recovery handoff

- [ ] **Step 1: Review the diff and all call paths** from CLI/Console through core, SQLite, native pane inspection, controller claim, and task registration; verify no generic unknown-delivery path was weakened.
- [ ] **Step 2: Record the exact operator evidence required** for this incident: no Task for old intent, old candidate differs from current frozen candidate, pane cwd and terminal match its private tag, no agent session/task prompt exists, and archive is retained.
- [ ] **Step 3: Create an immutable controller release snapshot and restart only `com.user.herdr-controller`** after the patch passes all required gates; do not restart other services.
- [ ] **Step 4: Reconcile the existing old launch through the new audited action**, verify exactly one Test task registers at candidate `cb5e8be091eccfee4a3a2885485ea82ebe3013dc`, and verify Controller advances Review only after Test settles.
- [ ] **Step 5: Report code verification separately from live workflow progress**; leave the existing workflow open and do not push, create a PR, merge, or deploy the NexusArchive application.
