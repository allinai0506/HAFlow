# HAFlow Collaboration Protocol V1

## Boundary

```text
Herdr = Agent Runtime + Communication Infrastructure
HAFlow = Collaboration Semantics + Orchestration + Governance + State/Evidence/Recovery
```

HAFlow never builds a message bus, chat system, pane/runtime, queue, or agent
runtime. All delivery reuses the existing path:

```text
dispatch_collaboration_event
→ resolve target pane from task.runtime / pane_id
→ build minimal prompt (herdr/collaboration.py, pure)
→ herdr agent prompt <pane> (existing binary, subprocess)
```

## CollaborationEvent

Table `collaboration_events` in the shared SQLite StateStore. Minimal fields:

```text
event_id, identity_key, run_id, workflow_id,
from_task_id, from_agent, from_pane_id,
to_task_id, to_agent, to_pane_id,
type, summary, artifact_refs, evidence_refs, context_refs,
requires_response, status, source_fact_id,
created/dispatched/acknowledged/completed_at (+ handoff_* mirrors)
```

V1 types only: `HANDOFF REQUEST RESULT BLOCKER REVIEW_REQUEST ACK`.
A `Handoff` is `CollaborationEvent(type=HANDOFF)`; no second model.

Identity (stable, run-scoped):

```text
(run_id, from_task_id, to_task_id, type, source_fact_id)
```

Every auto-handoff binds a `source_fact_id` (transition/Finding event id).
Status-only inference is forbidden.

## Not a chat message

Stored:最小必要上下文, 成果引用, 证据引用, 下一步要求.
Never stored: full conversation, prompt history, terminal transcript,
chain-of-thought. Artifact/evidence travel as refs
(`commit:`, `file:`, `observation:`, `verification:`, `finding:`);
agents fetch bodies through existing tools.

`ContextPack` stays the agent's working memory; handoffs carry at most
`context_refs`, never a ContextPack dump.

## Handoff lifecycle

```text
created → dispatched → acknowledged → completed
   └→ failed (from created/dispatched/acknowledged)
```

`dispatched` = prompt accepted by Herdr. `acknowledged` = target Task first
entered `working` (via `ack_collaboration_event_for_task`; exactly once).
Completion is recorded by the existing verification path, not by an "ACK" chat
message.

## Deterministic routing

```text
deterministic relation → code dispatches directly
needs judgment → Supervisor / Coordinator
```

V1 auto-routes (only these three):

```text
implementation_completed → HANDOFF → reviewer
review_completed         → HANDOFF → tester / verification
blocker                  → BLOCKER → coordinator
```

Anything else returns `None` and keeps the existing Coordinator path. Node
`depends_on`/`next` from the workflow template is reused; no second
dependency system. The original
`Workflow → dependency → node activation` chain is untouched; collaboration
adds `node activation → CollaborationEvent → Herdr direct dispatch`.

## Herdr dispatch mapping

```text
collaboration_dispatch_intent (events table)
→ herdr agent prompt <to_pane_id> (minimal prompt)
→ collaboration_dispatched + status=dispatched
```

Target panes resolve only from existing runtime identity
(`task.runtime`, `pane_id`, `agent`, `workspace_id`, `tab_id`).
Missing pane, unknown task, or run mismatch marks the event `failed` —
never silent fallback to another agent. `BLOCKER → coordinator` is the only
route allowed to target the coordinator pane.

Minimal prompt shape (§9): `HANDOFF FROM / TASK / SUMMARY(≤500) /
ARTIFACTS / EVIDENCE / NEXT ACTION / HANDOFF_ID`, total ≤2000 chars.

## Idempotency

`identity_key` has a UNIQUE constraint. `create_collaboration_event` runs
`BEGIN IMMEDIATE → SELECT → INSERT ON CONFLICT DO NOTHING → re-SELECT`
and returns the canonical row, so duplicate consumption yields one event.
`dispatch_collaboration_event` short-circuits non-`created` states
(`recovered: True`, no second prompt).

## Recovery

Mirrors the Supervisor `dispatch_intent → prompt → dispatched` pattern:

```text
collaboration_dispatch_intent recorded
→ prompt sent
→ status=dispatched
```

A restart that finds a prior intent without `dispatched` marks the event
dispatched with `recovered: True` instead of re-prompting, so Agent B never
runs twice. Known trade-off (shared with the Supervisor path): a crash
between intent-write and a never-sent prompt is counted as dispatched on
recovery; delivery certainty is traded for no-duplicate safety.

## Run isolation

All reads/writes are `run_id`-scoped. Dispatch compares the target Task's
`run_id` with the event's `run_id` and fails on mismatch, so `run-A`
handoffs never reach `run-B` tasks even when node/task names match.

## Metrics

Recorded facts only, no dashboard:

```text
handoff_created/dispatched/acknowledged/completed_at
→ dispatch_latency / ack_latency / handoff_latency
```

V1 also enables `handoff_wait_time`, `handoff_count`; `coordinator_hops`
has no authoritative fact source yet and is out of scope.

## Known limitations

- Handoff summaries travel unredacted: keep secrets out of `summary`; only
  refs cross the pane boundary, bodies are fetched through existing tools.
- Three auto-routes only; dynamic DAG, group chat, broadcast, discovery,
  Evidence Board UI, and Workspace Memory are explicitly non-goals.
- Intent-exists recovery assumes the prompt was sent (see Recovery).
- `review_completed → tester` follows the task spec; if a deployment's real
  template orders review differently, the template wins and the route table
  must be updated rather than worked around with LLM routing.

## Code map

- `herdr/collaboration.py` — pure semantics (identity, prompt, routing).
- `herdr/state_db.py` — `collaboration_events` + idempotent CRUD.
- `services/herdr-controller.py` — `dispatch_collaboration_event`,
  `ack_collaboration_event_for_task` (assembly + Herdr subprocess only),
  plus guarded production hooks (accelerator, never breaking main flow):
  `try_direct_stage_advance` → `maybe_dispatch_node_handoffs` (one HANDOFF
  per launched task on known edges), `handle_event` working branch →
  `maybe_ack_on_working`. Kill-switch: `HERDR_COLLABORATION_ENABLED=0`.
