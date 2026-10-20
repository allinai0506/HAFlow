# Wrapup candidate delivery repair

Goal: close X-certified → wrapup Y → fresh certified S → PR → same-head receipt → completed using normal platform entries. No merge/deploy.

Architecture: the wrapup Task owns additional Git output; its delivery phase is separate from Task completion. Keep integration/publication/freeze distinct durable facts. Candidate-specific remote branches avoid overwriting an existing PR identity. Reuse normal scheduler dispatch and business evidence.

| Step | Owner / state | Durable proof | Next condition |
| --- | --- | --- | --- |
| X certified | upstream Task | integrated publication, freeze, test/review, delivery | wrapup edits |
| register Y | active wrapup | run/execution/parent episode + commit registration | clean attributable Git output |
| integrate S | same wrapup, still active | task-owned refs, integrated_commit + publication | S includes Y after normal rebase |
| freeze S | Controller | candidate_frozen + old verifier retirement | new test/review dispatch |
| verify S | independent test/review Tasks | same episode pass + business acceptance | delivery record |
| publish S | wrapup platform create-pr | candidate-specific branch + replacement PR receipt | live CI/review green |
| complete | original wrapup | S clone HEAD + same-head review receipt | ordinary completion gates |

- [x] RED: real CLI fails to register wrapup Y; no-change X control succeeds.
- [x] Implement narrowly scoped wrapup delivery admission, retain Task status through normal commit/adoption/integration.
- [x] Consume publication and freeze through Controller; retire previous verifiers idempotently without changing old verdicts; force rerun both nodes and retain finite budgets.
- [x] Validate new effective delivery and publish candidate-scoped replacement PR through existing create-pr; replay remote failures safely.
- [x] Exercise CLI artifacts/acceptance/verdict, PR and receipt; cover prior PR, failures, stale identity, Z and crash replay.
- [x] Wire both actual default templates and finish instructions, fix report ordering.
- [x] Cleanup and independent source reviews complete; user approved the narrow rebase recovery repair after the three-round escalation.
- [x] Final affected-suite evidence: 430 passed, 2 skipped, 9 subtests; independent reviews found no remaining confirmed blocker.
Delivery target: code commit and reviewable PR. Remote merge-result CI remains separate from isolated test proof; delivery URL and final CI state are reported in the PR session.

Rejected: mode-only change leaves completion ring; rewriting X evidence fabricates certification; ancestor-based receipt matching weakens identity; a new generic recovery framework expands scope. Old asynchronous results must remain tied to retired tasks and old episode. Each round has maximum existing verifier budget and wrapup rotation cap three. Conflicts/missing identity fail closed; unknown external publication is recovered via existing branch/PR inventory, never blind repeat effects.
