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
| run identity, start/finish, event counts | Trajectory `events` |
| observation count and bytes | `observations` metadata (`size_bytes`) |
| findings | `trajectory_findings` |
| ContextPack count/latest size | `context_packs` metadata and serialized fields |
| verification totals | `verification_completed` Trajectory payloads |
| task status | Runtime `tasks` projection |

Aggregation uses SQL counts/sums and does not read Observation content. The
query is scoped by `run_id` and does not write state.

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
