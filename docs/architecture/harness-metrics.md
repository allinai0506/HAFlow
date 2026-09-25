# Harness Metrics V1

## Why

Harness Metrics makes one Run's observable execution facts queryable: elapsed
time, Trajectory volume, Observations, Findings, ContextPacks, and verification
results. It is an instrument, not an evaluation score or an optimization
policy.

## Data model and sources

`HarnessRunMetrics` is aggregated on demand from the existing SQLite facts:

| Metric | Source |
| --- | --- |
| run identity, task/workflow ownership, start/finish, event counts | Task identity projection plus Trajectory `events` |
| observation count and bytes | `observations` metadata (`size_bytes`) |
| findings | `trajectory_findings` |
| ContextPack count/latest size | `context_packs` metadata and serialized fields |
| WorkingContext compiles/reuse/change/latest size | `working_context_metric_events` and `working_contexts` |
| verification totals | `verification_completed` Trajectory payloads |
| task status | Runtime `tasks` projection |

Aggregation uses SQL counts/sums and does not read Observation content. Scalar
fact queries are scoped by `run_id`; identity resolution additionally checks the
persisted Task projection to prove unique ownership. Metrics do not write state.

Run identity is ownership-checked from the persisted Task projection: when a
Run's Trajectory facts reference a Task whose persisted `run_id` belongs to
another Run, the metrics report no `task_id` / `workflow_id` / status instead
of stitching across runs. A source-only Run with no authoritative Task row is
reported as unknown with zero source aggregates; event-carried identity is not
used as a substitute.

Verification classification is corruption-tolerant: a `verification_completed`
row whose payload is not valid JSON still counts in `verification_total`, but
is never classified as passed/failed. SQLite JSON predicates guard the
classification queries. If the latest ContextPack cannot be normalized, only
`latest_context_pack_bytes` becomes `null`; independent metrics remain
available.

`context_compact.trigger_count` is the count of successful ContextPack
creations. A skipped or attempted compact is not counted. ObservationPack uses
the count of successfully created, deduplicated Observations. The Observer and
Handoff trigger counts are `null` until a single authoritative production fact
exists for them.

## Currently unsupported

Model request/token/cache/cost usage, Observation read receipts, Observer
trigger count, and Handoff trigger count are `null` in V1. HAFlow does not yet
expose complete authoritative facts for these values, so Metrics does not infer
them from transcript rows or other proxies.

## CLI

Human-readable summary:

```bash
herdr-task metrics --run-id <run_id>
```

Stable machine-readable output:

```bash
herdr-task metrics --run-id <run_id> --json
```

An incomplete Run is queryable. Its `finished_at` is `null`,
`task_completed` is `false`, and `wall_time_seconds` runs from the first
Trajectory event to query time.

Metrics are facts only; they do not themselves establish that a task was
correct, efficient, or worth repeating.
