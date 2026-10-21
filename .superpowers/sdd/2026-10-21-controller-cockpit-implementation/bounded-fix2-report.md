# Controller Cockpit — bounded repair cycle 2

## Status and provenance

- Scope: the six residual findings explicitly authorized for this cycle; local implementation, temporary SQLite/browser validation, and local commits only.
- Repository: `/Users/user/.herdr-controller/clones/wf-haflow-1021-controller-cockpit-impl`; branch `feat/controller-cockpit`.
- Fixed base: `5f370b7964c7ca070354030206c9b7db8c63474f`.
- Implementation commit: `77b4e61980e11066525f4707c0c4268f26e73076`.
- Implementation tree: `94c791e2e8da332b22221fc849cf5b69ebab720e`.
- Implementation and required scoped tests pass. Independent whole-branch review remains with the parent controller. This is not a `MERGE_READY`, deployment, CI, business-UAT, or production-safety verdict.
- Formal acceptance-evidence-gate status: **UNVERIFIED (environmental precondition blocked)**. The inherited untracked `.superpowers/brainstorm/` must be preserved; the gate requires an entirely clean Git worktree. It was not hidden, deleted, stashed, or added to this change to obtain a pass.
- No push, PR, merge, deployment, service restart, real Agent launch, or paid-model call was performed. The default-DB isolation deviation below prevents an unconditional claim of “no production state touched.”

The existing SDD owner, base, and authorized clone were retained. CodeGraph was used first for code discovery. Repository rules, engineering protocol, TDD, scoped cleanup, and verification-before-completion informed the work. `git fetch origin` refreshed remote knowledge without rebasing or changing the approved base. Existing brainstorm files were not edited.

## Six findings and changes

| Finding | Authoritative repair | Behavioral acceptance |
| --- | --- | --- |
| 1. Operation-less confirmation bypass | Persisted policy is checked in admission, launch-intent recording, and Task registration. Supervised roots retain durable Operations even with `HERDR_COORDINATOR_INTAKE=0`; after approval the actual advancement caller queues them. New Runs cannot bypass through operation-less compatibility writes. | Workflow-supervised, node-supervised, and `confirm_before: launch` reject unapproved intent/registration; automatic remains allowed. Actual Controller admission sends nothing before approval and queues/sends once after durable approval. |
| 2. Workflow-control TOCTOU | Carry `expected_identity` through Console controls to SQLite. Compare complete fingerprint, execution and candidate inside `BEGIN IMMEDIATE` at mutation. Rollback Task transitions, lock/override invalidation, and history share one transaction. Pause/resume, checkpoint, restore and step have corresponding write fences. | Replace execution and register a new Run between lookup and effect: rollback/checkpoint/restore/step reject with `identity_changed` and leave the new Run working. Pause/resume metadata interleavings reject. Another connection cannot acquire the rollback writer fence; injected second-transition failure rolls back the first transition/event. |
| 3. Controls hidden by 101+ task actions | Stable-prioritize workflow-level controls before applying the existing 100-action display cap. Exact action lookup remains separate from the display window. | 101 Tasks still expose resume/checkpoint/rollback/step. Real browser DOM has at most 100 action cards and resume changes selected SQLite state. |
| 4. Incomplete checkpoint dedupe fingerprint | Fingerprint the raw persisted workflow row and every persisted Task row used by checkpoint content, including metadata/payload, `agent_override`, timestamps and versions. | Metadata-only change produces a different checkpoint receipt and snapshot; another identical intent dedupes without creating a third checkpoint. |
| 5. Restore receipt query truncation | Filter workflow, event type, source, exact checkpoint/reason, watermark and time before `ORDER BY id DESC LIMIT 1`. Add matching expression indexes. | Restore A, then B, then B: A's readback still finds its event. SQLite EXPLAIN selects the exact-identity index without temporary sort. |
| 6. Recovery deep-link loses confirmation | An `operationId` filter retains only the matching `confirm_dispatch` action. Existing exact action retrieval is exposed to the reader when the target is outside the window. Explicit navigation invalidates older scoped requests. | Matching-operation-only runtime assertions; delayed operation result cannot overwrite later node navigation. Real browser locates/approves the oldest of 105 Operations outside default display and reads durable confirmation back from SQLite. |

The cohesive `herdr/workflow_control_store.py` owns SQLite workflow identity fences and rollback. No extra model, service, UI mode, dependency, or product capability was introduced.

## TDD evidence

The first exact six-finding batch was run before implementation:

```text
pytest -q tests/test_controller_cockpit_bounded_fix2.py tests/test_console_cockpit_runtime.py -k 'bounded_fix2 or operation_deep_link' --tb=short
17 failed, 2 passed, 45 deselected
```

After the initial repairs, that same batch reported `19 passed`. Failures reproduced unguarded supervised registration/intent/admission; old-execution control mutation; missing workflow controls; reused metadata-stale checkpoint receipt; missing A restore event; and discarded deep-link confirmation. These are executed RED results from the tool transcript, not reconstructed failures or independent external acceptance.

Further exact RED counterexamples were added before their respective compatibility repairs:

- Supervised checkpoint inventory replay and replacement of an existing operation-less Run: two failures, then green.
- Step evidence callback requiring another SQLite writer: one failure, then green. Evidence/file I/O now precedes the write transaction, followed by a fresh guard.
- Approved intake-disabled root queued by actual advancement: three policy variants failed, then green.
- Operation target outside the 105-Operation window: real browser timeout, then green after exact retrieval.
- Legacy pause/resume endpoint preserving supplied expected identity: two failures, then green.
- Delayed operation-scoped HTTP response after explicit node navigation: one failure, then green.
- Durable allocation-resource evidence after a policy change: one failure, then green. Evidence updates survive, but new Task registration remains blocked.

The final dedicated backend file has 28 parameterized cases. Frontend runtime and browser files add operation isolation, 101+ Tasks, and 105-Operation coverage.

Two inherited source-string UI assertions in `test_console_ops_repair_gate_separation.py` were stale relative to the prior Cockpit entrypoint replacement. They now invoke the existing real JavaScript runtime contracts for high-risk confirmation and header/signoff navigation. Their SQLite/API assertions remain intact; assertions were not weakened to accept the prior defect.

## Verification commands and results

All test fixtures isolate state before application imports. Final reruns additionally used an explicit child-process environment:

```text
HOME=/tmp/haflow-cockpit-fix2-final.eviNwR
HERDR_STATE_DB=/tmp/haflow-cockpit-fix2-final.eviNwR/<suite>.db
```

The directory was created with `mktemp -d`. Tests select their own `tmp_path` databases as needed; browser tests own/close distinct pages and temporary loopback HTTP servers. External terminal transport/service probes are controlled, while Console HTTP, projection, action execution, identity, receipts and SQLite remain real. No model/Agent is invoked.

### Affected backend/frontend suite

```text
env HOME=/tmp/haflow-cockpit-fix2-final.eviNwR HERDR_STATE_DB=/tmp/haflow-cockpit-fix2-final.eviNwR/state.db pytest -q tests/test_workflow_engine.py tests/test_controller_actions.py tests/test_controller_progress_actions.py tests/test_controller_cockpit_projection.py tests/test_console_controller_cockpit_api.py tests/test_console_controller_actions_api.py tests/test_controller_cockpit_automation.py tests/test_dispatch_recovery_ui.py tests/test_console_cockpit_runtime.py tests/test_console_task_detail.py tests/test_workflow_graph_projection.py tests/test_console_frontend_syntax.py tests/test_console_ui_modules.py tests/test_console_module_boundaries.py tests/test_controller_cockpit_final_fixes.py tests/test_controller_cockpit_bounded_fix2.py tests/test_node_dispatch_contract.py tests/test_direct_stage_dispatch.py tests/test_downstream_dispatch_contract.py tests/test_scheduler_dispatch_e2e.py tests/test_dispatch_supersede_enforcement.py tests/test_terminal_workflow_dispatch_recovery.py tests/test_dispatch_delivery_timeout.py tests/test_state_transition_gateway.py tests/test_state_db_v2.py tests/test_state_store.py tests/test_console_ops_repair_gate_separation.py --tb=short
```

Exit 0: **781 passed, 40 subtests passed in 53.17s**. This final rerun was after scoped cleanup; no code changed between this run and implementation commit.

### Full Controller Cockpit browser suites

```text
env HOME=/tmp/haflow-cockpit-fix2-final.eviNwR HERDR_STATE_DB=/tmp/haflow-cockpit-fix2-final.eviNwR/browser.db pytest -q tests/test_console_controller_cockpit_e2e.py --tb=short
```

Exit 0: **14 passed in 41.63s**, no skips. This is the full suite, serial, after final code cleanup.

```text
pytest -q -n 2 tests/test_console_controller_cockpit_e2e.py --tb=short
```

Exit 0: **14 passed in 23.53s**, no skips, after final code cleanup. At implementation commit `77b4e61980e11066525f4707c0c4268f26e73076`, the additional explicit-env rerun also passed:

```text
env HOME=/tmp/haflow-cockpit-fix2-final.eviNwR HERDR_STATE_DB=/tmp/haflow-cockpit-fix2-final.eviNwR/xdist.db pytest -q -n 2 tests/test_console_controller_cockpit_e2e.py --tb=short
14 passed in 23.76s
```

The six counterexamples and runtime isolation were also rerun at that committed code:

```text
env HOME=/tmp/haflow-cockpit-fix2-final.eviNwR HERDR_STATE_DB=/tmp/haflow-cockpit-fix2-final.eviNwR/acceptance.db pytest -q tests/test_controller_cockpit_bounded_fix2.py tests/test_console_cockpit_runtime.py --tb=short
75 passed, 2 subtests passed in 6.58s
```

### Post-cleanup focused suite

```text
pytest -q tests/test_controller_cockpit_automation.py tests/test_controller_cockpit_bounded_fix2.py tests/test_controller_cockpit_final_fixes.py tests/test_console_controller_cockpit_api.py tests/test_console_cockpit_runtime.py --tb=short
```

Exit 0: **143 passed, 2 subtests passed in 12.86s**. The larger affected suite was subsequently rerun as above.

### Static/CLI syntax

```text
python3 -m compileall -q herdr console services bin tests
python3 -m py_compile bin/herdr-task bin/herdr-factory services/herdr-controller.py console/herdr_factory_console.py
git diff --check
```

All exit 0, including a final pre-commit run. The no-extension CLI scripts are explicitly covered; compileall alone is not treated as their verification.

### Formal acceptance gate

```text
env HOME=/tmp/haflow-cockpit-fix2-final.eviNwR HERDR_STATE_DB=/tmp/haflow-cockpit-fix2-final.eviNwR/gate.db python3 /Users/user/.agents/skills/acceptance-evidence-gate/scripts/acceptance_gate.py run --repo /Users/user/.herdr-controller/clones/wf-haflow-1021-controller-cockpit-impl --contract /tmp/haflow-cockpit-fix2-final.eviNwR/acceptance-contract.json --out /tmp/haflow-cockpit-fix2-final.eviNwR/gate-output
```

At implementation HEAD `77b4e61980e11066525f4707c0c4268f26e73076`, exit 2:

```text
GATE ERROR: repository is dirty before checks: commit/stash changes and retry
```

The only untracked inherited material was `.superpowers/brainstorm/`. No receipt was generated, and no gate PASS/VERIFIED is claimed. The external contract maps the six user-approved criteria to the dedicated counterexamples/runtime/browser checks; it does not treat the generated tests as the requirement source. No protected external anchor was available. Parent independent review is still required.

## Cross-effect self-review

1. Confirmation authority is SQLite policy plus durable Operation decision, not a UI flag. Every new normal `save_task` path and changed operation-less Run identity validates admission. Existing bound-task immutable-identity/recovery checks remain in place. Existing legacy/type-only API rejection tests pass; type-only execution cannot supply missing authority.
2. Restore replay is a private immutable-checkpoint inventory insertion, not a public `save_task` bypass flag or launch API. A historical supervised snapshot restores without manufacturing launch intent or dispatch approval. New normal registration still rejects.
3. Repeated resource evidence for an already durable allocating intent can survive adoption of supervision, with workflow/node/task/execution identity checked. It grants no registration permission. This preserves cleanup/recovery evidence without reopening a launch.
4. Actual control mutations are inside SQLite writer transactions. Rollback state and audit changes commit together or all roll back. Step business evidence/file hashing is outside the writer lock and followed by another fingerprint check. Compatibility JSON projections and legacy stage-state cleanup occur after commit; they are not claimed to be atomic with SQLite.
5. Checkpoint fingerprints include complete persisted snapshot state. Pause/resume claim dedupe deliberately excludes the new fingerprint but retains the status epoch: a metadata edit cannot replay an already-claimed uncertain transition. The effect boundary still enforces the fresh full fingerprint. Existing interrupted-claim/restart/dedupe tests pass.
6. Prioritization does not increase `MAX_ACTIONS=100`. Exact target retrieval returns an isolated action; browser filtering intersects operation/node/task/run/pane targets. Explicit navigation invalidates late scoped responses. Existing bounded snapshot limits and fail-closed overflow remain.
7. Receipt queries filter exact identity before LIMIT and use matching SQLite expression indexes. Event IDs/watermarks prevent accepting an unrelated older event; A/B/B is now covered.
8. Scoped cleanup removed redundant post-query JSON matching and an unused import. No broad cleanup, dependency addition, or policy-default change was made. Wiki architecture and append-only log were updated; existing identity/transaction rules were reused rather than adding duplicate engineering rules.

No additional correctness blocker was identified in this self-review. This is not an independent review and does not prove absence of all bugs.

## Isolation deviation and unverified baseline

The following command was executed **without explicit temporary HOME/HERDR_STATE_DB**:

```text
./bin/herdr-task verify-baseline wf-haflow-1021-controller-cockpit-impl
```

It exited 1 with `Task not found: wf-haflow-1021-controller-cockpit-impl`; therefore CoW baseline verification did not pass. Ordinary Git status is not substituted for that baseline.

Subsequent source inspection showed the verifier's `load_tasks` constructs `SQLiteStateStore`, which calls `init_db`; thus this nominal verification path can initialize schema in the default Controller database. The candidate adds `CREATE INDEX IF NOT EXISTS` for `idx_events_checkpoint_identity` and `idx_events_control_reason`. The candidate-specific potential side effect is creation of those two indexes if absent; ordinary schema-initialization/connection side effects are also possible. No successful Task mutation, dispatch, launch, or service restart was observed. **Whether any actual default-DB change occurred is unverified.** A failing “Task not found” response is not proof of zero side effects.

The parent was informed immediately after recognizing this risk and instructed: use only explicit temporary environments; do not inspect, roll back, or further touch the default Controller DB; document command, side-effect bound and uncertainty. That instruction was followed. No compensating default-DB operation was attempted. Tests after that instruction used explicit temporary environment/fixtures. Production safety/unchanged-state cannot be asserted from this run.

## Handoff boundaries

- Required six fixes are implemented and have executed RED-to-GREEN evidence plus affected regression/browser checks.
- No production workflow was used for acceptance. Temporary real HTTP/browser/SQLite execution is not production UAT.
- CoW baseline: not verified, as documented above.
- Acceptance-evidence-gate: environmentally blocked by preserved inherited files; no passing receipt.
- Parent independent review and any authorized CI/merge-result full regression remain separate gates.
- No push, PR, merge, deploy, restart, cleanup of inherited brainstorm, or default-DB investigation is authorized by this handoff.
