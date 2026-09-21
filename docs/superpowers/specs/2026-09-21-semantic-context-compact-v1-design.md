# Semantic Context Compact V1 Design

## Goal

Add an append-only `ContextPack` working-memory layer that gives the next Agent a bounded, traceable snapshot of a run without copying the run's raw history or allowing semantic output to manufacture facts.

## Architecture

`herdr/context_compact.py` is the functional core and imperative adapter. It reads a bounded recent Trajectory slice, selected verification events, bounded Finding rows, Observation metadata, Artifact refs, the latest ContextPack, and current Task/Runtime facts. It creates a deterministic machine-controlled base, optionally asks an injected `DecisionProvider` to select semantic items, verifies every returned reference, and persists one append-only snapshot. Provider errors, malformed JSON, lookup failures, and store failures are isolated to the compact operation.

The reducer uses the existing provider abstraction. Since providers expose judgment methods rather than a raw generation API, the V1 adapter asks a provider to choose among deterministic candidate summaries; a test/provider may also return a JSON mapping through the narrow reducer seam. No concrete vendor is imported by the compact core. `verified_facts` is always built from persisted Task/Runtime and Trajectory verification payloads, never copied from reducer output.

## ContextPack contract

The persisted snapshot contains `context_id`, `run_id`, `task_id`, `workflow_id`, `goal`, `current_state`, `completed`, `verified_facts`, `important_findings`, `evidence_refs`, `artifact_refs`, `open_issues`, `next_focus`, `source_event_sequence`, `created_at`, and `metadata`. JSON fields are stored as JSON text. `context_id` starts with `ctx_`. `important_findings` is capped at five and `next_focus` at three. All refs in a stored pack are references to existing event/finding/observation/artifact facts; invalid model refs are removed.

## Bounded input

Defaults are `max_input_chars=12000`, `max_recent_events=100`, `max_findings=10`, `max_observations=20`, and `max_artifacts=20`. The final serialized reducer input is hard-truncated by value-preserving stages: old events, low-severity findings, observation excerpts, and excess artifact refs are removed first. Goal, current state, latest verification, critical findings, and identity refs remain. Observation content is never loaded by compact V1.

## Storage and deduplication

`context_packs` is append-only with `(run_id, created_at)` and `(task_id, created_at)` indexes. `get_latest_context`, `get_context`, and `list_contexts` read snapshots. A repeated compact with the same run and source event sequence returns the existing latest snapshot, so no duplicate is inserted. A new sequence creates a new complete snapshot while retaining the previous one.

## Boundary isolation

Manual CLI compact is synchronous and reports a compact-specific error. Boundary compaction is scheduled on a daemon thread at `agent_done`/`rework` using a best-effort wrapper. It never participates in the state transition transaction and catches all exceptions; Task, Workflow, Coordinator, and Runtime state remain authoritative and continue normally.

## Acceptance

Tests cover basic construction, no large-content duplication, valid observation/finding refs, hallucinated refs, programmatic verification facts, bounded provider input, previous-context replacement, sequence deduplication, provider fallback, boundary isolation, no full Observation read, exact source sequence, and Facts/Evidence/Analysis separation.
