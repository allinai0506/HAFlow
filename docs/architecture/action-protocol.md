# HAFlow Action Protocol V1

## Why Intervention exists

Semantic Supervisor already produces a `PolicyDecision`, but a decision is not
an execution fact. `Intervention` is the durable, run-scoped handoff between
that decision and the Controller. It records the request, execution state,
provenance, result, and failure without creating a second runtime database.

## Decision vs Intervention

An Evaluation describes observed evidence. A PolicyDecision describes the
Supervisor's policy output. An Intervention is created only for an enforced
`VERIFY` or `RETRY` decision and is executed only by the Controller. Observe
mode records the evaluation and decision but creates no executable
Intervention.

The canonical identity is:

```text
(run_id, task_id, decision_id, action)
```

The SQLite `interventions.identity_key` unique constraint makes repeated
checkpoints, event delivery, and Controller restarts converge on one row.

## Intervention lifecycle

```text
requested → running → completed
                    └→ failed
```

The existing SQLite `events` table records:

```text
intervention_requested
intervention_started
intervention_completed
intervention_failed
```

Each event contains the run and task identity. `pending_intervention()` reads
requested/running V1 rows before falling back to legacy policy-event behavior
for actions outside V1.

## RETRY

RETRY reuses the existing legal Task transition path and does not create a
second retry engine. A new retry while the Task is already `rework` enters the
legal `rework → working` next iteration; it never uses `rework → rework` as
fake evidence. The transition carries `intervention_id`, `decision_id`,
`action`, and the authoritative attempt count. The completed result records
the previous status, new status, and observed attempt count.

The Controller checks the persisted Task attempt count against the existing
Supervisor policy `max_attempts` before execution. When the budget is
exhausted, the Intervention is durably marked `failed` with
`retry_budget_exhausted`; no rework transition is attempted.

## VERIFY

VERIFY sends the Task into the existing `rework`/verification route and records
a durable `verification_requested` event carrying its Intervention identity.
The result is based on a fresh Task read and says
`verification_requested=true` and `verification_pending=true`. It never writes
`passed=true` and never substitutes for the existing `tests_completed` or
`verification_completed` facts. Those facts remain produced by the existing
verification machinery.

## Idempotency

Creation and claim use `BEGIN IMMEDIATE` and the unique identity constraint.
Only a requested row can be claimed immediately. A stale running row can be
reclaimed only when its database lease has expired, using an atomic
`execution_owner` + `lease_until` compare-and-set. A live lease makes the
second Controller skip the row. Completion/failure also checks the owner, so a
reclaimed stale worker cannot finalize another Controller's execution.
Completed, failed, and superseded rows cannot execute again.

## Crash recovery

Controller done/recovery handling scans requested and running rows before
allowing normal done redelivery. Requested rows are claimed and executed.
Running rows are recovered only after a stale lease is atomically reclaimed.
Before executing, recovery searches the Task status history and canonical
events for the exact `intervention_id` and action. A transition already tagged
with that identity completes the Intervention without repeating the side
effect; Task status alone is never treated as proof. A durable pending
Intervention therefore cannot be bypassed by RateGate or a replayed
`agent_done` event.

## Run isolation

Interventions use `run_id_for_task()` when created and recovery validates the
same ownership relation before executing. Missing Tasks and run mismatches
are recorded as machine-readable failures. An Intervention from one Run
cannot operate on a Task belonging to another Run.

## Failure semantics

An action-handler exception writes `intervention_failed` with the exception
type and bounded message. It does not leave the row permanently `running`.
Supervisor/provider failure before a durable request remains fail-safe and
does not fail the normal Task flow. A failure while creating a durable V1
request is also fail-safe: no handler runs and normal continuation remains
allowed. Once the request is persisted, the Controller owns its lifecycle and
the default done continuation remains intercepted, including after handler
failure.

## Metrics

V1 does not add speculative in-memory counters. The authoritative source for
future `intervention_total`, `retry_interventions`, `verify_interventions`,
and `intervention_failed` metrics is the persisted Intervention table and its
events; a metrics projection can be added separately without changing action
semantics.
