# ObservationPack V1 Implementation Plan

> **For agentic workers:** This plan is executed inline under the unified-dev-flow controller-free route. Steps use checkbox syntax and each slice ends with a fresh test checkpoint.

**Goal:** Add an immutable, redacted, hash-verifiable Observation evidence layer backed by SQLite metadata and bounded file reads, then connect agent-log, verification, artifact, Trajectory, and Finding references without putting large content into the Ledger or Finding.

**Architecture:** Add one focused `herdr/observation.py` module that owns the public model, content files, receipt normalization, bounded reads, integrity checks, deduplication, verification/artifact adapters, and best-effort integration helpers. Reuse `herdr.state_db` for the `observations` metadata table and existing `redact_text` for all persisted text. Keep Trajectory append-only and Findings analytical: both store only compact Observation references.

**Tech Stack:** Python 3 standard library, `sqlite3` WAL connections, `dataclasses`, `hashlib.sha256`, filesystem exclusive-create, `pytest`, and `multiprocessing` with independent SQLite connections.

## Global Constraints

- Content is redacted before persistence; raw secrets are never stored.
- Observation content is immutable; there is no content-update API.
- Default reads are bounded to 16 KiB and hard-capped at 64 KiB.
- Deduplication key is `(run_id, source_type, source_ref, sha256)` and concurrent callers return one canonical row.
- Ledger and Finding contain metadata/ref only, never full content.
- Artifact observations reference existing files and do not copy large artifacts.
- Observation failures are best effort at Observer/runtime boundaries and cannot fail Workflow, Task, or Agent execution.
- No Context Compact, RAG, search, GC, permissions, remediation, dashboard, object storage, or vector database.
- Delivery target is `working_tree`; do not commit, push, create PR, merge, or deploy.

---

### Task 1: Observation schema and immutable store

**Files:**
- Create: `herdr/observation.py`
- Modify: `herdr/state_db.py`
- Test: `tests/test_observation.py`

**Interfaces:**
- Produces `Observation`, `create_observation`, `get_observation`, `list_observations`, `read_observation`, `verify_observation`, `create_verification_observation`, and `create_artifact_observation`.
- Uses `state_db.get_default_db_path`, `state_db.get_db_connection`, and `herdr.supervisor.state.redact_text`.

- [ ] **Step 1: Write failing store tests** for creation receipt fields, redacted text, SQLite metadata-only rows, default storage next to a temporary DB, and `obs_<uuid>` identity.
- [ ] **Step 2: Run the focused tests and confirm the expected import/API failures** with `pytest tests/test_observation.py -q`.
- [ ] **Step 3: Add the `observations` table and indexes** in `_ensure_schema`, including `UNIQUE(run_id, source_type, source_ref, sha256)` and `(run_id, created_at)`, `(task_id, created_at)`, `(source_type)`, and `sha256` indexes. Keep existing schema initialization idempotent.
- [ ] **Step 4: Implement `Observation` as a frozen dataclass** with `to_mapping()`/`from_mapping()` conversion and bounded validation for source types, media type, size, hash, excerpt, and JSON-safe metadata.
- [ ] **Step 5: Implement text/JSON normalization and redaction** so string content and serialized JSON are redacted before byte hashing, content write, excerpt generation, and metadata serialization; preserve binary bytes for artifact references.
- [ ] **Step 6: Implement exclusive content creation** under `<db parent>/observations/`, using `open(..., "xb")` for content-backed observations and writing metadata only after content succeeds. On metadata failure, clean up only the newly-created unreferenced file.
- [ ] **Step 7: Implement transactional deduplication** with SQLite `BEGIN IMMEDIATE`, unique-conflict re-read, and canonical row return. Never replace existing metadata or content.
- [ ] **Step 8: Implement `get_observation` and filtered `list_observations`** using parameterized SQL and stable `created_at, observation_id` ordering.
- [ ] **Step 9: Implement bounded `read_observation`** with offset validation, 16 KiB default, 64 KiB hard maximum, byte counts, truncation flag, and safe UTF-8 decoding.
- [ ] **Step 10: Implement `verify_observation`** to check existence, byte size, and SHA-256; return `valid=False` with bounded reason for missing/tampered content and optionally support verification from `read_observation`.
- [ ] **Step 11: Add failing-then-green tests** for 1 MiB bounded reads, tamper detection, content immutability, duplicate creation, changed content, secret absence from content/excerpt/metadata, and direct store failure behavior.
- [ ] **Step 12: Run `pytest tests/test_observation.py -q` and `python3 -m compileall herdr`**; checkpoint before integration.

### Task 2: Trajectory receipt and verification/artifact adapters

**Files:**
- Modify: `herdr/observation.py`
- Modify: `herdr/trajectory.py`
- Test: `tests/test_observation.py`
- Test: `tests/test_trajectory.py`

**Interfaces:**
- `create_verification_observation(verification, *, run_id, task_id=None, workflow_id=None, store=None) -> Observation` serializes the existing `evidence_id` and verification counters without removing compatibility fields.
- `create_artifact_observation(path, *, run_id, source_ref=None, artifact_kind=None, task_id=None, workflow_id=None, store=None) -> Observation` records external path, size, and hash without copying bytes.
- `record_observation_created(task, observation, *, ledger=None) -> Optional[dict]` appends the minimal trajectory event.

- [ ] **Step 1: Add failing verification adapter tests** asserting `source_type="verification"`, retained `evidence_id`, redacted JSON content, and a retrievable observation.
- [ ] **Step 2: Add failing artifact adapter tests** asserting `source_type="artifact"`, `content_ref` points to the original file, correct size/hash, and no `observations/<id>` copy for the artifact bytes.
- [ ] **Step 3: Add a failing Trajectory test** asserting `observation_created` contains observation id/source/ref/size/hash but no full content or full excerpt.
- [ ] **Step 4: Implement the adapters** with strict path/file validation, bounded receipt metadata, and no mutation of existing verification/artifact event fields.
- [ ] **Step 5: Implement the Trajectory helper** using the existing `record_trajectory_event_best_effort` seam; use task run identity and retain best-effort semantics.
- [ ] **Step 6: Run `pytest tests/test_observation.py tests/test_trajectory.py -q`** and inspect persisted event payloads for large-content absence.

### Task 3: Observer log evidence references

**Files:**
- Modify: `herdr/observer/engine.py`
- Modify: `herdr/observer/harness.py` only if dependency injection is needed
- Modify: `herdr/observer/models.py` only if the existing dataclass needs a typed helper
- Test: `tests/test_trajectory_observer.py`

**Interfaces:**
- The Observer keeps current signal generation and redaction intact, then best-effort converts selected `log` evidence into an Observation and returns a Finding evidence item with `type="observation"`, `observation_id`, `source_type`, and bounded excerpt.
- On store failure, the existing redacted `log` evidence item is retained and Finding persistence proceeds.

- [ ] **Step 1: Add failing Observer tests** for a repeated/live transcript Finding that creates one retrievable `agent_log` Observation and stores only its reference in Finding evidence.
- [ ] **Step 2: Add a failing failure-isolation test** with an ObservationStore that raises; assert `observe_run` still returns/persists the Finding and retains short fallback evidence.
- [ ] **Step 3: Add a failing dedup test** for repeated identical log evidence in the same run/source, asserting one observation id is reused and no content is placed in the Ledger/Finding row.
- [ ] **Step 4: Implement a narrow Observer evidence conversion helper** after `_redact_evidence`, reusing already bounded transcript data and `redact_text`; never capture every periodic transcript.
- [ ] **Step 5: Thread the existing store/db path into the helper** without changing provider scheduling, signal semantics, or controller blocking behavior.
- [ ] **Step 6: Run targeted Observer regression tests**, including `pytest tests/test_trajectory_observer.py -q` and the inherited test-isolation cases.

### Task 4: Cross-process correctness and adversarial coverage

**Files:**
- Modify: `tests/test_observation.py`
- Modify: `herdr/observation.py` or `herdr/state_db.py` only when a test exposes a real race

**Interfaces:**
- No new public API; this task proves the Task 1/2 contract at independent process boundaries.

- [ ] **Step 1: Add a multiprocessing test** where two processes use separate SQLite connections and create the same `(run_id, source_type, source_ref, content)` concurrently.
- [ ] **Step 2: Run the concurrency test alone** and confirm it either fails with duplicate rows/race exceptions or passes only after the implementation already supplies the required unique-conflict retry.
- [ ] **Step 3: Add adversarial tests** for path traversal in observation ids/source refs, negative offsets/limits, missing content files, changed external artifact files, invalid source types, non-JSON metadata, and secret-shaped metadata.
- [ ] **Step 4: Implement only the smallest validation/locking corrections** required by those failures; do not add speculative lifecycle or permission abstractions.
- [ ] **Step 5: Run `pytest tests/test_observation.py -q` repeatedly (at least 3 runs)** and record stable pass counts.

### Task 5: S4 cleanup and documentation alignment

**Files:**
- Modify: `herdr/observation.py`, `herdr/state_db.py`, `herdr/trajectory.py`, `herdr/observer/engine.py`, and tests only when cleanup identifies dead code
- Modify: `wiki/index.md` or the most specific Observation/Trajectory wiki page if the architectural relation is documented there

- [ ] **Step 1: Run `ai-slop-cleaner` in Mode B** on the changed source and tests, deletion-first; remove unused imports, dead wrappers, unreachable branches, and duplicated receipt conversion without changing the contract.
- [ ] **Step 2: Run `git diff --check` and the focused tests** after cleanup; treat any source/test change as invalidating earlier evidence.
- [ ] **Step 3: Update only the relevant wiki architecture entry and `wiki/log.md`** with the final ObservationPack boundary if the existing project rule requires the architecture change to be recorded.
- [ ] **Step 4: Re-run the focused tests after documentation edits only when code was touched by cleanup; otherwise record docs-only scope.

### Task 6: S5 verification, S6 review artifacts, and working-tree handoff

**Files:**
- Create: `.omc/verify-01a0c174-530a-7f01-850e-9101c46bc70f.md`
- Create through sanctioned reviewer path: `.omc/review-01a0c174-530a-7f01-850e-9101c46bc70f.md`

- [ ] **Step 1: Run focused evidence tests:** `pytest tests/test_observation.py tests/test_trajectory.py -q`.
- [ ] **Step 2: Run Observer/Supervisor regressions:** `pytest tests/test_trajectory_observer.py tests/test_supervisor_tests_completed.py -q`.
- [ ] **Step 3: Run the full suite:** `pytest -q`.
- [ ] **Step 4: Run static checks:** `python3 -m compileall herdr services bin tests` and `git diff --check`.
- [ ] **Step 5: Run project health baseline/comparison command** if available; record unavailable checks as explicit waivers rather than pass claims.
- [ ] **Step 6: Write S5 evidence** with command exit codes, test counts, the Mode B cleanup record, inherited baseline changes, and any skipped production acceptance (no deployment authorized).
- [ ] **Step 7: Run independent `google-code-review` and `code-review` against the current diff and this design/plan**; reviewer checks data integrity, concurrency, security, failure isolation, and scope.
- [ ] **Step 8: If review returns NEEDS_FIXES, return to Task 5, rerun all affected S5 commands, then repeat review; escalate after three rounds.**
- [ ] **Step 9: Record `MERGE_READY` only through the sanctioned independent-review mechanism** after the final diff hash matches; stop at verified working tree with no push/PR/deploy.

## S4 Exit Cleanup Plan (ai-slop-cleaner Mode B)

Bounded files: `herdr/observation.py`, `herdr/state_db.py`, `herdr/trajectory.py`, `herdr/observer/engine.py`, `herdr/observer/harness.py`, `services/herdr-controller.py`, and the new/changed tests. Pass order is deletion first, then duplicate receipt/validation logic, then naming and error-boundary review. Preserve the public API and all redaction, deduplication, bounded-read, and failure-isolation tests. Do not redesign the schema or add speculative abstractions. After each cleanup pass, rerun the focused Observation, Trajectory, Observer, and Supervisor tests before entering S5.
