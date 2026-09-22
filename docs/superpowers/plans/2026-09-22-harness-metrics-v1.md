# Harness Metrics V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use inline execution with TDD checkpoints; do not modify Agent/Harness execution behavior.

**Goal:** Provide a bounded, read-only `HarnessRunMetrics` aggregation and `herdr-task metrics` CLI for one Run.

**Architecture:** Add one pure-ish metrics aggregation module that reads existing SQLite facts through a single bounded state-db aggregate query plus one latest ContextPack projection. Keep unsupported provider usage, observation reads, and handoff metrics explicit as `null` with metadata sources. Add only CLI presentation and tests; no metrics table, event writes, provider calls, or execution-path changes.

**Tech Stack:** Python standard library, SQLite, existing `state_db`, `TrajectoryLedger` identity rules, argparse CLI, pytest.

## Global Constraints

- Reuse Trajectory, Observation, Finding, ContextPack, Runtime/Task state as existing authorities.
- Do not read Observation content; aggregate `size_bytes` and metadata only.
- Run identity is always scoped by `run_id`; no cross-Run fallback or inference.
- Metrics queries are fail-safe and read-only; failures return a CLI error without changing runtime state.
- Provider token/cost/cache metrics remain `null` unless existing trajectory usage facts are reliable; V1 will expose only reliable `model_requests` if present.
- Do not change Agent, Observer, Compact, Handoff, Provider, schema, or execution behavior.

---

### Task 1: Define the aggregation contract and RED tests

**Files:**
- Create: `herdr/metrics.py`
- Create: `tests/test_metrics.py`

**Interfaces:**
- `HarnessRunMetrics` immutable dataclass with `to_mapping()` and stable JSON-safe null/unsupported fields.
- `get_run_metrics(run_id: str, *, db_path: Optional[Path] = None, now: Optional[float] = None) -> HarnessRunMetrics`.
- `aggregate_run_facts(run_id: str, *, db_path: Optional[Path] = None, now: Optional[float] = None) -> dict` remains internal; it must not read Observation content.

- [ ] Write tests first for: empty Run; complete Run with task_started, two observations, one finding, two verification events, one ContextPack, agent_done; observation dedup; multiple ContextPacks/latest size; Run A/B isolation; incomplete Run; explicit unknown usage/handoff fields.
- [ ] Run `pytest -q tests/test_metrics.py` and verify RED because the module/API does not exist.
- [ ] Define the expected mapping: counts are zero for empty Run, `finished_at` is null and wall time uses `now` for incomplete Run, verification pass/fail comes from the `verification.passed` fact, and unsupported fields are null with source metadata.

### Task 2: Add bounded SQL aggregation over existing facts

**Files:**
- Modify: `herdr/state_db.py` near existing trajectory/observation/context-pack read helpers
- Modify: `herdr/metrics.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Add an internal `aggregate_run_metric_rows(run_id, db_path)` query helper returning only scalar aggregates and the latest ContextPack row. It must use `COUNT`, `SUM`, `MIN`, `MAX`, `GROUP BY`/conditional aggregation and indexed `run_id` filters.
- Use `events` for trajectory/verification facts, `trajectory_findings` for findings, `observations` for `COUNT`/`SUM(size_bytes)`, and `context_packs` for count/latest serialized metadata.

- [ ] Implement event timing from the first trajectory event and terminal `run_completed`/`run_failed` event; use the Task row for `task_id`, `workflow_id`, `final_status` when the event identity resolves it.
- [ ] Implement observation counts using the unique `observations` rows, so dedup does not inflate `observations_created`; sum only `size_bytes`.
- [ ] Implement ContextPack count and latest serialized byte size from stored JSON columns without reading Observation content.
- [ ] Count `verification_completed` and conditional passed/failed facts from the existing payload; malformed/missing passed values remain failed/unknown according to the existing event contract and are covered by tests.
- [ ] Implement reliable model request count only from explicit existing trajectory event/usage facts; leave token/cache/cost null. Leave observation reads and handoff null unless a complete authoritative fact source exists.
- [ ] Run the focused tests and verify GREEN.

### Task 3: Add `herdr-task metrics` presentation

**Files:**
- Modify: `bin/herdr-task`
- Create or modify: `tests/test_harness_metrics_cli.py`

**Interfaces:**
- Add `metrics --run-id RUN_ID [--json]` parser entry.
- Add `cmd_metrics(args)` that calls `herdr.metrics.get_run_metrics` and writes either stable JSON to stdout or a concise human summary; errors go to stderr with non-zero exit and no state mutation.

- [ ] Add a subprocess test with a temporary SQLite DB and `HERDR_STATE_DB`/existing test fixture routing; assert `--json` parses and includes required keys.
- [ ] Add a human-output assertion for run id, wall time, observations, findings, compactions, verification, and unsupported usage/handoff markers.
- [ ] Run CLI tests and `python3 bin/herdr-task metrics --help`.

### Task 4: Document the measurement boundary

**Files:**
- Create: `docs/architecture/harness-metrics.md`

- [ ] Document purpose, data-source mapping, incomplete Run semantics, bounded-query rule, CLI examples, and explicit unsupported metrics.
- [ ] Include one real or fixture Run JSON example and state that Metrics are facts, not evaluation conclusions.
- [ ] Check links/format and keep the document short.

### Task 5: Fresh verification and review preparation

**Files:**
- No additional source files unless a test exposes a defect.

- [ ] Run `pytest -q tests/test_metrics.py tests/test_harness_metrics_cli.py`.
- [ ] Run `python3 -m compileall -q herdr services bin tests` and `git diff --check`.
- [ ] Run full `pytest -q` and record exact pass/skip/fail counts.
- [ ] Inspect the complete diff and SQL/query plans enough to verify no Observation content read, no writes, no provider calls, and no execution-path imports.
- [ ] Run `./bin/herdr-task metrics --run-id <fixture-run> --json` and preserve the actual JSON in the delivery report/PR description.

### Task 6: Delivery

**Files:**
- Modify: required S5/S6 evidence artifacts under `.omc/` only.

- [ ] Complete the mandatory deletion-first cleanup review before S5.
- [ ] Write fresh `.omc/verify-<session>.md` evidence after cleanup.
- [ ] Obtain an independent S6 review with `MERGE_READY`; any finding returns through S4 → S5 → S6.
- [ ] Commit the feature, tests, docs, and evidence; push `feat/harness-metrics-v1`; create one independent PR against `main`.
- [ ] Report baseline SHA, final SHA, changed files, commands, actual fixture JSON, reliable metrics, unsupported metrics, and explicit non-actions (no merge/deploy).
