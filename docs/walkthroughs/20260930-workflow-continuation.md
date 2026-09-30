# Workflow continuation gap — delivery evidence

## Change and scope

A running workflow with cleaned deliveries and missing required Tasks now has a persistent continuation obligation. The Controller recomputes the explicit plan and bounded read-only Git ancestry, queues a serialized recovery event, rechecks facts and canonical coordinator identity, and sends at most two actual prompts. Busy/unknown coordinators retain their send budget; prolonged inactivity escalates to a recoverable native notification. Projection shows received/adopted/unknown evidence and the target SHA.

No direct task launch, branch adoption, force-pass, production workflow mutation, service restart, merge or deployment is part of this delivery. Existing pauses, blockers, Git finalization and candidate/serial barriers remain authoritative. Nodes need an explicit list of at most 64 required IDs and an existing task. Successor execution hands responsibility to successor gates.

## Isolation and baseline

Independent CoW sandbox: `/Users/user/.herdr-controller/clones/workflow-continuation-0930`.
Branch: `fix/workflow-continuation`.
Fixed upstream baseline: `1132f68affdc3c915f986b7ea613d84c43d07e77` (`origin/main`).
The original ui-upgrade work area was not edited. This is an independently created sandbox, not a production-registered Herdr Task; production Task lifecycle / verify-baseline acceptance was not claimed. The reviewed patch is relative to the fixed upstream commit.

## Verification

- Initial regression: 17 failures, including the missing capability and the superseded-history projection blind spot; subsequently fixed with real temporary Git.
- Final affected matrix: `python3.13 -m pytest -q tests/test_workflow_continuation.py tests/test_liveness_guard.py tests/test_projection_engine.py tests/test_scheduler_dispatch_e2e.py tests/test_direct_stage_dispatch.py tests/test_console_projection_api.py` — 200 passed, 35 subtests passed; zero failed/skipped.
- Final full suite: `python3.13 -m pytest -q` — 2566 passed, 72 subtests passed, zero failed/skipped, 305.99 seconds.
- `python3 -m compileall -q herdr services bin tests` — exit 0.
- `git diff --check` — exit 0.
- Independent Standards and Spec reviews: original findings fixed and rechecked; no remaining confirmed blockers. Spec reviewer independently ran the 31-test subset before the final node-identity regression. The final 32 continuation tests are included in the affected/full counts above.

The persistence/read chain uses real temporary SQLite, frozen workflow JSON, config normalization, Controller reconciliation, EpisodeStore transactions and Projection. Two independent processes compete for one recovery claim. Regression coverage includes cross-workflow and cross-node IDs, replacement chains, satisfied parallel predecessors, paused/blocked/active states, in-flight Git finalization, adoption before/after actual branch movement, timeout/unknown evidence, restart, stale/duplicate queue rejection, busy budget, native notifier False receipts, notification retries, and nonblocking background scans.

## Unverified environment boundaries

Agent prompt and native notification transports are controlled substitutes. No live Agent turn, macOS notification delivery, controller deployment or production workflow recovery is asserted. Merge/deployment must be handled separately after PR review.
