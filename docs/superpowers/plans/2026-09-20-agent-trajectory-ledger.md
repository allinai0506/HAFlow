# Agent Trajectory Ledger Implementation Plan

> **For agentic workers:** This plan is executed inline in the current COW sandbox; no push, PR, merge, or deployment is authorized.

**Goal:** Record a machine-readable, append-only event stream for each HAFlow task execution without changing runtime behavior.

**Architecture:** Reuse the existing SQLite `events` store and add nullable trajectory columns rather than introducing a second persistence system. A focused `herdr/trajectory.py` adapter owns schema normalization, per-run sequence allocation, append-only writes, and reads; small integration calls are placed at task registration, kernel transition, and the existing tests_completed boundary.

**Tech Stack:** Python standard library, dataclasses, sqlite3, pytest, existing StateStore and RuntimeState.

## Global Constraints

- Preserve existing Workflow, Task state-machine, Agent routing, Worker, RuntimeState, CLI, and Continuous Evaluation behavior.
- Record facts only; never infer quality, stuckness, termination, or remediation.
- Keep old SQLite events valid; new `run_id` and `sequence` columns are nullable for legacy rows.
- Omit absent optional fields; do not write empty strings or fabricated runtime identity.
- Do not add external dependencies, dashboard code, or future Observer/self-optimization features.
- Preserve inherited pre-task changes in docs and `tests/test_script_bootstrap.py`.

## File map

- NEW: `herdr/trajectory.py`: `TrajectoryEvent`, `TrajectoryLedger`, `record_trajectory_event`, and legacy run-id helper.
- MODIFY: `herdr/state_db.py`: nullable event columns, indexes, atomic sequence allocation, event reconstruction.
- MODIFY: `herdr/kernel.py`: best-effort task transition and terminal trajectory events after existing transition.
- NEW: `bin/herdr-task`: persist run_id and record launch facts from `worker_result`.
- MODIFY: `services/herdr-controller.py`: record verification_completed from existing tests_completed evidence.
- NEW: `tests/test_trajectory.py`: focused storage/model tests.
- MODIFY: `tests/test_state_transition_gateway.py`: transition/terminal integration assertions.
- MODIFY: `tests/test_supervisor_tests_completed.py`: verification integration assertion if fixture permits.

### Task 1: Add failing trajectory contract tests

**Files:** NEW: `tests/test_trajectory.py`; NEW: `herdr/trajectory.py`

**Interfaces:** `TrajectoryEvent.from_mapping(mapping) -> TrajectoryEvent`; `TrajectoryEvent.to_mapping() -> dict`; `TrajectoryLedger(db_path).append_event(event_or_mapping) -> dict`; `TrajectoryLedger.list_events(run_id, event_type=None, task_id=None, agent_session_id=None) -> list[dict]`.

- [ ] Write tests for model normalization, append/read, per-run order, run isolation, omitted optional fields, and all runtime identity fields.
- [ ] Run `pytest tests/test_trajectory.py -v` and confirm it fails because the module/API is absent.

### Task 2: Implement the standalone trajectory adapter

**Files:** MODIFY: `herdr/trajectory.py`; MODIFY: `herdr/state_db.py`

- [ ] Add nullable `run_id` and `sequence` columns to the existing `events` table through `_ensure_event_columns`, plus a run/sequence index.
- [ ] Add atomic sequence allocation under a SQLite write transaction; use the inserted event id to form `event_id=evt_<id>`.
- [ ] Implement event mapping that omits `None` fields, keeps metadata as a dictionary, and preserves runtime identity fields exactly.
- [ ] Run focused tests and then adversarial checks for separate runs and concurrent append ordering.

### Task 3: Integrate launch, transitions, and verification

**Files:** MODIFY: `bin/herdr-task`; MODIFY: `herdr/kernel.py`; MODIFY: `services/herdr-controller.py`; MODIFY: `tests/test_state_transition_gateway.py`; MODIFY: `tests/test_supervisor_tests_completed.py`

- [ ] Add failing tests for task transitions producing ordered status/terminal events and tests_completed producing verification_completed.
- [ ] Generate/persist run_id in task registration, then append run/task/agent start events from actual worker fields.
- [ ] Append status and terminal events after the existing kernel transition; swallow Ledger errors after logging.
- [ ] Append verification_completed beside the existing tests_completed fact with bounded evidence fields.
- [ ] Run related tests GREEN and verify legacy tasks without run_id remain operable.

### Task 4: Refactor and verify

- [ ] Run ai-slop-cleaner Mode B on changed implementation and remove only dead/redundant code.
- [ ] Run focused tests, full `pytest`, and `python3 -m compileall herdr/ services/ bin/ tests/`.
- [ ] Verify inherited dirty files are unchanged; write `.omc/verify-trajectory-ledger-20260920.md` with exact evidence.
- [ ] Perform read-only review and write `.omc/review-trajectory-ledger-20260920.md` with `verdict: MERGE_READY` only if evidence supports it.
