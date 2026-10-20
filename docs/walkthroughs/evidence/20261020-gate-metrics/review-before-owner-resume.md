# S6 review

verdict: NEEDS_FIXES
round: 3
reviewer: independent Standards and Spec agents (same model), plus gpt-5.6-sol max adversarial reviewer (cross-model)
source_sha256: 416773b827043354ce3aa2892501dbb02bd8d0d338c14b26ff9017853d20db19

Original obsolete-wait lifecycle, D11 incomplete EOF/tracked-empty patches, recurrent blocker sets, arbitrary rebaseline command, unsupported Console actions, identical append replay and eval consumption of stale lint contracts have been reproduced and fixed with targeted tests.

Remaining blocker: new business-gate wait omits reopened_at and terminal detection treats historical completed_at as current terminality. Canonical completed → in_progress(reopened_at) → running reuses old wait id and interleaved reconcile supersedes every new sample. Source: herdr/gate_wait.py and herdr/recovery_store.py. Regression: tests/test_gate_metrics_handoff.py::test_reopened_workflow_gets_a_new_wait_generation.

Required bounded repair: bind canonical reopen generation in identity/staleness, judge terminality against current lifecycle; verify new generation starts attempts=1 and survives interleaved controller reconciliation until escalation. Do not change state_store/transitions schema/contracts.

Owner decision pending under RULES.md / unified-dev-flow three-round escalation. No push, PR, merge, deployment or service restart authorized or performed.
