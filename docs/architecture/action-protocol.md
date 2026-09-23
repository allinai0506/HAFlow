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

VERIFY sends the Task into the existing `rework` route and dispatches the
existing Agent prompt path (`herdr agent prompt`) with the Intervention and
Decision identities. The prompt asks the Agent to run the existing
verification/test loop; it does not run a second test runner. Only a successful
prompt dispatch writes `verification_dispatched` and allows the Intervention
to complete. The result says `verification_requested=true`,
`verification_dispatched=true`, and `verification_pending=true`; it never
writes `passed=true` and never substitutes for the existing `tests_completed`
or `verification_completed` facts.

The later `tests_completed`/`verification_completed` receipt carries the same
Intervention identity. `extract_test_evidence()` reads one immutable
`EVAL_DONE.json` byte snapshot and derives the metrics, hash, and completion
metadata from that same read. It is accepted only when that snapshot is a new
version relative to the dispatch baseline (hash and evaluator completion
time); an old snapshot cannot be relabeled as the new VERIFY receipt. Rework
watchdog and recovery paths require a matching new verification receipt; old
deliverables alone cannot bypass a pending VERIFY.

Dispatch first records a durable `verification_dispatch_intent` containing the
Intervention identity and evidence baseline. If the Controller crashes after
the prompt is accepted but before `verification_dispatched` is written,
recovery consumes that intent and completes the dispatch receipt without
sending the prompt again. A dispatch failure leaves the latest VERIFY
Intervention failed and keeps rework blocking; it cannot be healed to
`agent_done` from old deliverables.

The same dispatch-intent and dispatch-receipt pattern is used by RETRY. A
RETRY is not complete merely because the Task entered `rework` or `working`;
the Controller must successfully submit the existing Agent prompt path and
persist `retry_dispatched`. Rework watchdogs therefore cannot promote a Task
from old deliverables while a new RETRY lacks dispatch evidence.

VERIFY uses the policy `max_verifications` as a durable action-layer budget.
The count is calculated from persisted VERIFY rows in requested, running,
completed, or failed state, so it survives process restart. Once the limit is
reached, the request is durably failed with
`verification_budget_exhausted` and is not dispatched.

## Idempotency

Creation and claim use `BEGIN IMMEDIATE` and the unique identity constraint.
Only a requested row can be claimed immediately. A stale running row can be
reclaimed only when its database lease has expired, using an atomic
`execution_owner` + `lease_until` compare-and-set. A live lease makes the
second Controller skip the row. Completion/failure also checks the owner, so a
reclaimed stale worker cannot finalize another Controller's execution.
Completed, failed, and superseded rows cannot execute again. Recovery is
task-scoped at the done gateway, so a pending action for another parallel Task
in the same Run cannot block or execute as part of this Task's done decision.

## Crash recovery

Controller done/recovery handling scans requested and running rows before
allowing normal done redelivery. Requested rows are claimed and executed.
Running rows are recovered only after a stale lease is atomically reclaimed.
Before executing, recovery searches canonical dispatch evidence for the exact
`intervention_id` and action. For RETRY, a Task transition alone is not
execution evidence; only `retry_dispatched` is. For VERIFY, only
`verification_dispatched` and its later completion receipt count. Task status
alone is never treated as proof. A durable pending
Intervention therefore cannot be bypassed by RateGate or a replayed
`agent_done` event.

Failed VERIFY rows are scoped to the current execution episode. Once a later
`agent_done` transition starts a new episode, an older failed VERIFY is no
longer allowed to block unrelated rework.

The Supervisor enabled/enforce kill switch is checked before action recovery;
when it is off, existing pending V1 actions are not replayed and the normal
flow is preserved. For a durable-capable StateStore, an unreadable
Intervention ledger is
`UNKNOWN`, not `NO_PENDING`: done emission fails closed and no coordinator
`done` event is sent. Legacy lightweight stores retain their pre-V1 behavior.
For VERIFY and RETRY, the successful durable table is authoritative; a failed
V1 request is recorded as non-durable and is never resurrected from a legacy
policy event.

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
failure. A `verification_completed` trajectory receipt is persisted before
the current evidence is passed to Supervisor evaluation/deduplication. If
that receipt write fails, the checkpoint returns without consuming the
evidence so a later poll can retry it.

## Metrics

V1 does not add speculative in-memory counters. The authoritative source for
future `intervention_total`, `retry_interventions`, `verify_interventions`,
and `intervention_failed` metrics is the persisted Intervention table and its
events; a metrics projection can be added separately without changing action
semantics.
