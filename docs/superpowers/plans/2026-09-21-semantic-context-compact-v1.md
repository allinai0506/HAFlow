# Semantic Context Compact V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the repository's TDD workflow and execute each task with fresh tests before progressing.

**Goal:** Build a bounded, append-only, reference-verified `ContextPack` working-memory layer for HAFlow runs.

**Architecture:** Keep compaction in one focused `herdr/context_compact.py` core module. Extend the existing SQLite state database with a JSON-column snapshot table and thin StateStore methods, then add a CLI adapter and isolated boundary trigger. The reducer is optional and injected; deterministic facts and reference verification remain program-owned.

**Tech Stack:** Python standard library, dataclasses, sqlite3, pytest, existing `TrajectoryLedger`, `ObservationStore`, `Trajectory Observer`, `DecisionProvider`, and argparse CLI.

## Global Constraints

- Preserve the Facts/Evidence/Analysis/Working Memory separation.
- Never load full Observation content during compaction.
- Never let provider output author `verified_facts` or unverified refs.
- ContextPack persistence is append-only; repeated identical source sequence deduplicates.
- Compact failures are best-effort and cannot mutate Task, Workflow, Runtime, or Coordinator state.
- Keep source changes under `herdr/`, `bin/`, `tests/`, `docs/`, and `wiki/`; no new dependencies.

### Task 1: Add the ContextPack persistence contract and model

**Files:**
- Modify: `herdr/state_db.py`
- Modify: `herdr/state_store.py`
- Create: `herdr/context_compact.py`
- Test: `tests/test_context_compact.py`

**Interfaces:**
- `ContextPack` dataclass with `to_mapping()` / `from_mapping()`.
- `save_context_pack(pack, db_path=None)`, `get_context_pack(context_id, db_path=None)`, `get_latest_context_pack(run_id, db_path=None)`, `list_context_packs(run_id, db_path=None)`.
- `StateStore.save_context_pack`, `get_context_pack`, `get_latest_context_pack`, and `list_context_packs` delegating to SQLite.

- [ ] Write failing tests for schema creation, round-trip JSON fields, append-only history, latest ordering, and same-sequence deduplication.
- [ ] Run `pytest tests/test_context_compact.py -q`; expect import/API failures.
- [ ] Add the `context_packs` table, indexes, JSON decoding, and thin StateStore methods.
- [ ] Run the focused persistence tests and confirm they pass.

### Task 2: Build bounded input, deterministic facts, and reference verification

**Files:**
- Modify: `herdr/context_compact.py`
- Test: `tests/test_context_compact.py`

**Interfaces:**
- `compact_run(run_id, *, task=None, store=None, provider=None, config=None, now=None) -> ContextPack`.
- `get_context(context_id, *, store=None)`, `get_latest_context(run_id, *, store=None)`, `list_contexts(run_id, *, store=None)`.
- `verify_context_references(...)` removes nonexistent event/finding/observation/artifact refs.

- [ ] Add failing tests for 100-event bounded input, 12k hard budget, metadata-only observations, exact source sequence, program-owned verification facts, valid refs, and hallucinated refs.
- [ ] Run focused tests and confirm failures.
- [ ] Implement bounded collection from Trajectory, Observer findings, Observation metadata, Task/Runtime facts, and artifact refs without calling `read_observation`.
- [ ] Implement deterministic `goal`, `current_state`, `verified_facts`, refs, and fallback semantic arrays.
- [ ] Implement hard budget trimming and post-reducer reference verification.
- [ ] Run focused tests and confirm pass.

### Task 3: Add optional reducer and fallback isolation

**Files:**
- Modify: `herdr/context_compact.py`
- Test: `tests/test_context_compact.py`

**Interfaces:**
- Reducer receives one bounded JSON-safe mapping and may return semantic arrays plus selection refs.
- Provider exceptions, invalid JSON, or invalid selections fall back to deterministic ContextPack output.

- [ ] Add failing tests for provider-selected semantic items, model-forged verified facts, malformed output, provider exception, and oversized input.
- [ ] Implement the narrow reducer adapter using the injected provider seam only; never import a vendor.
- [ ] Ensure `verified_facts` is rebuilt after reducer output and selected refs are filtered against persisted facts.
- [ ] Run focused reducer/fallback tests.

### Task 4: Add manual CLI and boundary best-effort trigger

**Files:**
- Modify: `bin/herdr-task`
- Modify: boundary caller in `services/herdr-controller.py` or existing transition adapter selected by current call path
- Test: `tests/test_context_compact.py`, relevant boundary test

**Interfaces:**
- `herdr-task compact --run-id RUN [--json] [--no-model]`.
- Boundary trigger is daemonized and catches all exceptions without changing transition results.

- [ ] Add failing CLI tests for help, pure JSON stdout, no-model output, and provider failure isolation.
- [ ] Add the compact subparser and imperative-shell dispatch.
- [ ] Add one minimal async best-effort hook at `agent_done`/`rework`, without waiting on it.
- [ ] Run CLI and boundary tests.

### Task 5: Documentation and cleanup

**Files:**
- Modify: `docs/references/cli-reference.md` if current CLI reference covers subcommands
- Modify: `wiki/index.md`, `wiki/log.md`, and/or relevant architecture page
- Modify: `docs/lessons/lessons-learned.md` only if a reusable lesson is confirmed

- [ ] Document the ContextPack contract, CLI, bounded defaults, and no-model semantics.
- [ ] Run `ai-slop-cleaner` Mode B on source changes and inspect its deletion-first result.
- [ ] Re-run focused tests after cleanup.

### Task 6: Verification and review evidence

- [ ] Run `pytest tests/test_context_compact.py -q`.
- [ ] Run related persistence/trajectory/observation/observer/runtime tests.
- [ ] Run full `pytest` and `python3 -m compileall herdr/ bin/ services/ tests/`.
- [ ] Run `./bin/herdr-task compact --help`; verify JSON stdout contains no diagnostics.
- [ ] Write `.omc/verify-20260921-context-compact.md` with command exit codes and cleanup evidence.
- [ ] Perform read-only Google-style review and write `.omc/review-20260921-context-compact.md` with `verdict: MERGE_READY` only if all blockers are resolved.
