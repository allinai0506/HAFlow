# ObservationPack V1 Design

**Date:** 2026-09-21
**Scope:** HAFlow Evidence Layer V1
**Delivery target:** working_tree

## Problem

Trajectory Ledger currently records durable facts and Trajectory Observer stores analysis Findings, but log transcripts and other verification material remain short excerpts or indirect references. ObservationPack adds a separate evidence layer: immutable, redacted, content-addressed metadata with bounded reads. Ledger and Finding records keep references and compact metadata only.

## Existing architecture reused

- `herdr/state_db.py` remains the SQLite authority, including schema initialization, WAL connections, and transaction boundaries.
- `herdr/trajectory.py` remains the append-only fact index. It receives an `observation_created` event containing only receipt metadata.
- `herdr/observer/engine.py` remains the analysis owner. It may create an Observation for evidence that is actually attached to a Finding, but Observation failure is best effort.
- `herdr/observer/live.py` remains the bounded, redacted live transcript reader.
- `herdr.supervisor.state.redact_text` is the single redaction utility; raw secrets are never persisted by ObservationPack.
- No existing Artifact Store was found. Artifact observations therefore reference an existing file and record its current size/hash without copying the file.

## Data model

`Observation` is a small immutable metadata record with:

```text
observation_id: str              # obs_<uuid>
run_id: Optional[str]
task_id: Optional[str]
workflow_id: Optional[str]
source_type: str                 # agent_log | verification | artifact | tool_output | runtime | other
source_ref: str
content_ref: str                 # local relative path or external artifact path
media_type: str
size_bytes: int
sha256: str
excerpt: Optional[str]           # redacted, max 1000 characters
created_at: float
metadata: dict                   # redacted JSON-safe receipt details
```

The SQLite `observations` table stores metadata only. Content-backed observations use an `observations/` directory next to the configured state database. The store derives this root from the same `HERDR_STATE_DB` resolution used by `state_db`; it does not introduce scattered Home-directory constants. Artifact observations use `content_ref` as the existing path and do not copy bytes.

## Store contract

The public API is:

```python
create_observation(
    *, run_id, task_id=None, workflow_id=None, source_type,
    source_ref, content, media_type="text/plain", metadata=None,
    excerpt=None, store=None, created_at=None,
) -> Observation
get_observation(observation_id, *, store=None) -> Optional[Observation]
list_observations(run_id=None, task_id=None, source_type=None, *, store=None) -> list[Observation]
read_observation(observation_id, offset=0, limit=16 * 1024, *, verify=False, store=None) -> dict
verify_observation(observation_id, *, store=None) -> dict
```

The default read limit is 16 KiB and the hard maximum is 64 KiB. Reads return the observation id, offset, returned byte count, total byte count, truncation flag, and content. Text/JSON reads use UTF-8 decoding that never raises for a partial multibyte boundary; binary reads return base64 content with `content_encoding="base64"` so arbitrary bytes are not corrupted.

Creation normalizes content to bytes, redacts text/JSON before hashing and writing, computes the receipt, writes the content using exclusive creation, and inserts metadata transactionally. A duplicate `(run_id, source_type, source_ref, sha256)` returns the existing canonical row. The content file is created before the metadata row; if metadata insertion fails, the newly-created file is removed only when it is not an existing canonical file. Existing rows and files are never overwritten.

## Integrity and concurrency

`sha256` and `size_bytes` describe the exact redacted bytes persisted or the exact referenced artifact bytes observed at creation. Text and JSON bytes are redacted before hashing; binary artifact files are hashed in 64 KiB chunks so large artifacts are never loaded into memory as one buffer. `verify_observation` checks file existence, size, and SHA-256; it returns `valid=False` with a bounded reason for missing or changed content. Metadata has no content-update API. Source references are capped at 512 characters and redacted metadata at 16 KiB.

SQLite uses a unique deduplication constraint and an immediate transaction. Concurrent processes race through the unique constraint, then re-read the canonical row. Content files use exclusive creation; a losing process removes only its own temporary candidate and returns the committed row. The concurrency test uses independent processes and SQLite connections rather than threads alone.

## Integration

### Agent log

Observer converts only evidence that is selected for a Finding. It passes the already bounded/redacted transcript to `create_observation(source_type="agent_log", source_ref=<log ref>)`. The Finding evidence item becomes `{type: "observation", observation_id, source_type, excerpt}`. If creation fails, the existing short `{type: "log", ref, excerpt}` item remains and the observer still persists the Finding.

### Verification

A small adapter serializes the existing verification receipt (`passed`, counts, lint/type errors, and original `evidence_id`) as redacted JSON with `source_type="verification"` and `source_ref="verification:<evidence_id>"`. Existing `evidence_id` remains in the event and metadata for backward compatibility; `observation_id` is additive.

### Artifact

An artifact adapter accepts an existing path/ref and kind, checks the file without copying it, and creates `source_type="artifact"` metadata containing `artifact_kind` and `artifact_path`. The `record_trajectory_event(..., "artifact_created", ...)` runtime path invokes this adapter when the referenced file is available, adds only `observation_id` to the artifact payload, then appends the compact receipt event. The receipt hash/size can later reveal that the external file changed.

### Trajectory

Successful runtime integration appends `observation_created` with only `observation_id`, `source_type`, `source_ref`, `size_bytes`, `sha256`, and the normal run/task/workflow identity. The low-level `ObservationStore.create` API is intentionally storage-only because its requested signature has no task/ledger context; all Observer, verification, and artifact runtime adapters call `record_observation_created` after successful creation. It never includes content or the full excerpt.

## Failure isolation

Observation persistence is wrapped at the Observer integration boundary. Storage, file, JSON, and SQLite errors are logged as bounded diagnostics and treated as missing Observation evidence. Existing deterministic signals, short redacted Finding evidence, and the workflow/agent path continue. Direct store APIs still raise ordinary validation/storage errors so callers can test and handle them explicitly.

## Verification scope

Tests cover creation receipts, 1 MiB bounded reads, tamper detection, exclusive immutability, same-content deduplication, changed-content new identity, redaction in content/excerpt/metadata, observation-created ledger references, Finding lookup, verification compatibility, artifact no-copy behavior, failure isolation, schema/index queries, and independent-process deduplication. No Context Compact, RAG, search, GC, permissions, remediation, dashboard, object storage, or vector database is included.
