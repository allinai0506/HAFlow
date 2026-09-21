# Trajectory Observer V1 Implementation Plan

> **For agentic workers:** This plan is executed inline on the isolated branch `feat/trajectory-observer-v1`; no push, PR, merge, or deployment is authorized without user confirmation.

**Goal:** Given a `run_id`, read the fact-only trajectory plus runtime/log evidence, and emit structured, evidence-backed `TrajectoryFinding`s without ever touching execution state.

**Architecture:** New `herdr/observer/` package (functional core + fail-safe harness). Facts come from the existing `TrajectoryLedger`; judgment confirmation reuses the existing `herdr/decision` `DecisionProvider` (noul `judge_many`); findings persist in a dedicated `trajectory_findings` SQLite table separate from the `events` fact store. The controller submits observations on daemon threads so a slow/unavailable model can never delay the polling loop.

**Tech Stack:** Python standard library, dataclasses, sqlite3, pytest, existing StateStore/TrajectoryLedger/DecisionProvider/Supervisor helpers.

## Global Constraints

- Observer is best-effort: any failure (crash, timeout, provider error, log read error) must not affect Workflow, Task, Agent, RuntimeState, or state transitions.
- Facts and judgments stay separated: `events` (source='trajectory') = fact; `trajectory_findings` = analysis. Never write a finding as a TrajectoryEvent.
- Every finding carries real evidence references (event_id/sequence/evidence_id/log ref); no guessing, no fabricated identifiers.
- Bounded by construction: recent-N events, key verification/terminal events, bounded log tail; no full trajectory or full log ever reaches the provider.
- Reuse `herdr/decision` providers; never hardcode a specific vendor in the observer package.
- No remediation: recommendations are advisory strings only (`continue|inspect|replan|retry|change_agent|request_human|interrupt`).
- No new third-party dependencies; no new daemon service.
- Preserve existing Workflow, Task state-machine, Agent routing, Worker, RuntimeState, Supervisor, CLI, and Trajectory Ledger behavior.

## File map

- NEW: `herdr/observer/models.py`: enums, `TrajectoryFinding`, `finding_key`/anchor helpers.
- NEW: `herdr/observer/config.py`: defaults + `~/.herdr-controller/observer.json` + env overrides (kill switch).
- NEW: `herdr/observer/signals.py`: deterministic signal detectors + provider question templates.
- NEW: `herdr/observer/context.py`: bounded `ObservationContext` + bounded log tail reader.
- NEW: `herdr/observer/engine.py`: `TrajectoryObserver.observe_run` (detect → confirm → dedup → persist).
- NEW: `herdr/observer/harness.py`: module-level `observe_run`, memoized provider, `ObservationScheduler` (daemon threads, non-blocking submit).
- NEW: `herdr/observer/__init__.py`: public surface.
- MODIFY: `herdr/state_db.py`: `trajectory_findings` table + `record_trajectory_finding`/`list_trajectory_findings`.
- MODIFY: `services/herdr-controller.py`: submit observation for working/rework/blocked tasks in `registry_watcher`.
- MODIFY: `bin/herdr-task`: `observe` subcommand (`--run-id`/`--task-id`/`--json`/`--no-model`).
- NEW: `tests/test_trajectory_observer.py`: tests 1–8 + scheduler/kill-switch/storage/dedup coverage.
- MODIFY: `wiki/index.md`, `wiki/task-lifecycle.md`, `wiki/log.md`: knowledge checkpoint.
- MODIFY: `docs/lessons/lessons-learned.md`: lesson (if a generalizable pitfall emerged).

### Task 1: RED — storage contract for findings

**Files:** NEW: `tests/test_trajectory_observer.py`; MODIFY: `herdr/state_db.py`

- [ ] Write failing tests: record/list round-trip, duplicate `finding_key` not re-inserted, run isolation, filters, JSON payload integrity.
- [ ] Implement `trajectory_findings` DDL in `_ensure_schema` + record/list functions with `INSERT ... ON CONFLICT(finding_key) DO NOTHING`.
- [ ] `pytest tests/test_trajectory_observer.py -k finding_store -v` GREEN.

### Task 2: RED — models and config

**Files:** NEW: `herdr/observer/models.py`; NEW: `herdr/observer/config.py`

- [ ] Write failing tests: finding mapping round-trip, finding_key stability across recomputation, enum validation, anchor changes produce new keys.
- [ ] Implement `TrajectoryFinding`, `FINDING_TYPES`, `SEVERITIES`, `RECOMMENDED_ACTIONS`, `finding_key_for(...)`; config defaults + env overrides + kill switch.

### Task 3: RED — deterministic signals

**Files:** NEW: `herdr/observer/signals.py`

- [ ] Write failing tests for stall (time alone → warning, never critical), repeated verification failures (evidence refs), runtime unavailable, no-progress reworks, repeated action (only with action events), verification_failure on done-claim, log-signature context problem, and healthy-run zero signals.
- [ ] Implement pure detectors over `(events, task, runtime, log_tail, now, config)` returning dataclass signals with evidence + anchor.

### Task 4: RED — bounded context and log tail

**Files:** NEW: `herdr/observer/context.py`

- [ ] Write failing tests: 1000 events → recent window ≤ N, serialized context ≤ budget, terminal events preserved; 5MB log → excerpt ≤ configured chars, ref preserved; secret redaction.
- [ ] Implement `read_log_tail` (bounded bytes→lines→chars) and `build_observation_context` with budget shrinking order (logs → artifacts → verification → events tail).

### Task 5: RED — engine + harness

**Files:** NEW: `herdr/observer/engine.py`; NEW: `herdr/observer/harness.py`; NEW: `herdr/observer/__init__.py`

- [ ] Write failing tests: healthy run → `[]` and provider not called; confirmed signal → persisted finding returned; model suppresses weak signal; provider unavailable → evidence-type signals survive, weak ones dropped; duplicate observation → single store row and stable finding_id; provider crash → task/status/events untouched; scheduler submit is non-blocking and failure-isolated; kill switch short-circuits.
- [ ] Implement `TrajectoryObserver` (gate → detect → context → confirm → dedup → persist) and `ObservationScheduler`.
- [ ] Run `pytest tests/test_trajectory_observer.py -v` GREEN.

### Task 6: Integration — controller trigger and CLI

**Files:** MODIFY: `services/herdr-controller.py`; MODIFY: `bin/herdr-task`

- [ ] Wire optional observer import + non-blocking submit for `working`/`rework`/`blocked`; failures logged and swallowed.
- [ ] Add `herdr-task observe` resolving run_id from `--run-id` or `--task-id`, with `--json` and `--no-model`.
- [ ] Smoke: `bin/herdr-task observe --run-id <tmp> --json` against a temp DB; assert exit 0 and JSON list shape.

### Task 7: S4 exit cleanup and S5 verification

- [ ] Run `ai-slop-cleaner` Mode B (deletion-first) on the new package; remove dead code/redundant wrappers; re-run focused tests.
- [ ] `pytest` full suite + `python3 -m compileall herdr/ services/ bin/ tests/` + CLI smoke; write `.omc/verify-feat_trajectory-observer-v1.md` with exact commands and exit codes.
- [ ] Write `.omc/review-feat_trajectory-observer-v1.md` via independent reviewer only (`--record-review`), verdict `MERGE_READY` only if evidence supports it.
