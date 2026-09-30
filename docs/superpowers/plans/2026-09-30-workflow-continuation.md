# Workflow continuation implementation plan

Goal: Close the silent gap between integrated task delivery and the next planned task.
Architecture: Recompute obligations from existing workflow definitions, SQLite Tasks and read-only Git ancestry. Persist bounded recovery episodes in the existing Controller attention ledger. Keep the Controller as execution owner.

1. Add regression tests using temporary Git and SQLite for cleaned T3/T4a, missing T4b/T7, and unchanged base. Run RED.
2. Add a bounded inspector and pure obligation decision. Respect workflow pause, task blockers, active tasks, replacement chains and downstream execution. Unknown Git evidence never asserts adoption.
3. Add periodic Controller reconciliation, durable episode claims, at most two actual coordinator prompts, canonical named-agent identity checks, stale-queue revalidation and recoverable human notification. Run Git inspection in one bounded background scan so ordinary stage checks remain responsive. Delivery acknowledgement does not resolve an episode.
4. Reuse the inspector in workflow stall projection. Show received/adopted/unknown and missing planned IDs with the target SHA.
5. Test recovery after reload, concurrent claims, progress resolution, unrelated-run exclusion, timeout, and failure. Run targeted and full regression, compileall and diff check.
6. Update task lifecycle Wiki and append lessons. Review, commit, push and create PR against main. No merge/deployment/restart authorization.

Validation boundaries: Core Git/SQLite/attention/queue/projection are real in temporary fixtures. External Agent prompt and native notification transport are controlled substitutes. No production workflow changes, model calls, merges or deployment are authorized.
