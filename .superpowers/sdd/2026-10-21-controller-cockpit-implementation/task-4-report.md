# Task 4 — Workflow Controller Cockpit UI

## Scope and result

Implemented the approved [Task 4 brief](task-4-brief.md) in the existing CoW on `feat/controller-cockpit`.
Baseline: `8d8f3a6e44ac5fd0d7045d0ae1d243749d7f5ead`.
Implementation commits: `9a2f38e3d9c0bf9c40f40f8422dfcddc24c3e244` and `64401c01212db4c626ecc74565bd4f9108dcd6c5`.

The new Cockpit reads `/api/workflow/controller-cockpit`, displays the server's service, judgment, nodes, tasks, recovery, actions, receipts, and timeline, and submits server-authored `action_id` plus `expected_identity` to `/api/controller/execute-action`.
The browser does not derive action authority or run automatic actions. Existing Controller-tab and workflow/task URLs remain in place.

## Changed files

| File | Change |
| --- | --- |
| `console/static/console.html` | Add the four Cockpit state fields and compose its stylesheet once. |
| `console/static/console/styles/controller-cockpit.css` | Add responsive light-token layout and own the migrated `ctl-*` compatibility styles. |
| `console/static/console/styles/operations.css` | Remove the migrated Cockpit declarations, including their responsive rules. |
| `console/static/console/scripts/controller-cockpit.js` | Render the projection, preserve recovery forms, submit authoritative actions, display receipts, retain rejected cards, match Task/Run/Pane targets, and scope human decisions to the selected Workflow. |
| `console/static/console/scripts/workflow-reader.js` | Load the Cockpit with sequence and Workflow guards; handle unknown reads and pending operation receipts. |
| `tests/test_console_cockpit_runtime.py` | Execute the real composed JS for the projection, receipts, identity, forms, actions, and race boundaries. |
| `tests/test_console_frontend_syntax.py` | Replace obsolete action-modal assertions with the new Cockpit contract. |
| `tests/test_console_ui_modules.py` | Assert shared light tokens and retain legacy function hashes outside the deliberately changed functions. |
| `tests/test_console_module_boundaries.py` | Preserve the DOM, branding, and unrelated enhancement-style baseline; replace the obsolete Cockpit-style hash with the new composition assertion. |

The parent explicitly confirmed that `test_console_module_boundaries.py`, listed in the brief's verification and commit commands, is authorized. No API, Controller, Task-board, dependency, deployment, or service files changed.

## RED to GREEN evidence

1. The first requested runtime/module run produced **5 failed, 14 passed**. The failures covered the missing `openControllerCockpit`, `renderControllerReceipt`, guarded loader, unknown-state rendering, and stylesheet.
2. Receipt regressions produced **2 failed, 13 passed**: rejected cards disappeared after refresh, and a resolved operation left its immediate waiting receipt visible. Both behaviors now pass.
3. The explicit Workflow-target test failed with `TARGET=old` and `BAD=confirmed`. The reader now preserves the requested Workflow and marks a mismatched response unknown.
4. Unknown automation modes opened an execution form. A regression observed `MODAL=true`; unsupported modes now leave it closed.
5. The human-decision regression observed `workflow_id=__ctl__`. Submission now uses the selected Workflow, and foreign decision data is not rendered.
6. The runtime-target regression observed `RUN=false`, `WRONG=true`, and `PANE=false`. Target matching now requires every supplied Task, Run, and Pane field to match.

The final verification command is:

```bash
pytest -q tests/test_console_cockpit_runtime.py tests/test_console_frontend_syntax.py tests/test_console_ui_modules.py tests/test_console_module_boundaries.py tests/test_console_plain_chinese_ui.py tests/test_console_controller_cockpit_api.py
git diff --check
```

The committed implementation at `64401c01212db4c626ecc74565bd4f9108dcd6c5` returned **74 passed, 73 subtests passed**. `git diff --check` exited 0.
The API regression file uses temporary SQLite stores and the existing isolated HTTP fixture, including a durable pause/resume action receipt and authoritative readback. It does not launch real Agents or call paid models.

## Browser and DOM evidence

Rendered the final composed page offline in Google Chrome Headless, using fresh temporary profiles, a controlled Cockpit fixture, disabled background networking, and a `fetch` function that rejects requests. Both browser processes exited 0.
These checks validate real browser parsing, CSS, responsive layout, and DOM rendering. They are not a browser-to-live-service E2E claim.

| Viewport | Page | Surface | Primary button | Horizontal overflow | Service / Run / Action |
| --- | --- | --- | --- | --- | --- |
| 1440 × 1100 | `rgb(248, 249, 250)` | `rgb(255, 255, 255)` | `rgb(94, 106, 210)` | None | Present / present / 1 |
| 500 × 1000 | `rgb(248, 249, 250)` | `rgb(255, 255, 255)` | `rgb(94, 106, 210)` | None | Present / present / 1 |

Desktop and narrow-screen screenshots were visually inspected. Temporary artifacts are `/tmp/haflow-cockpit-browser.4zysTZ/handoff-cockpit.html`, `handoff-0.png`, and `handoff-1.png`.

## Self-review

- The Cockpit has one action list. Node/Task cards only locate identities and open existing Task details.
- Only `manual` and `confirmation_required` modes can open an execution form. Automatic, blocked, missing, and unknown modes cannot submit.
- Confirmation checks the current Workflow, read state, automation mode, and server identity again. High-risk and bypass actions require an explicit reason.
- Action submission sends no caller-derived command, action type, endpoint override, or substituted Task identity.
- An immediate `accepted_waiting` receipt remains visible across refreshes. Another operation's receipt cannot resolve it. A matching operation's resolved or superseded state can clear it.
- A rejected action snapshot remains visible and disabled even when the new projection omits it. A current action with a different expected identity or intent remains usable.
- Unknown reads render an alert and do not claim there are no todos. A wrong-Workflow response becomes unknown, and stale request sequences cannot overwrite a new selection.
- The existing recovery form retains its operator, reason, candidate, lineage, absent-resource, and startup-transcript confirmation fields.
- Inline handlers use HTML-escaped JavaScript arguments. Runtime tests decode attributes as the browser does before checking their syntax.
- Human decisions use the selected Workflow rather than the synthetic Controller tab ID, and foreign decision rows remain hidden.
- This was self-review, not independent review. No known blocking defect was found within Task 4 after the regression fixes.

## Limits and handoff concerns

1. The current Task 3 service projection supplies service status but may omit the last-check timestamp. The UI displays `未提供`; it does not invent a timestamp or claim a polling heartbeat.
2. A waiting receipt without `operation_id` has no reliable operation correlation key. It stays visible until superseded by a later immediate receipt or the Workflow selection changes. Refresh and the authoritative timeline remain available; unrelated success records cannot clear it.
3. Judgment and policy reason codes are shown verbatim. Their interpretation remains server-authored; no client-side scheduling judgment is added.
4. This work does not add automatic handlers beyond the server's transaction-fenced pause/resume registry. Unsupported automatic actions remain server-rejected.
5. Production UAT, live runtime probes, deployment, service restart, push, PR creation, merge, and full CI were not performed. The parent owns aggregate acceptance and the required CoW `verify-baseline`; this subtask did not access production Task state.
6. The pre-existing untracked `.superpowers` handoff files were preserved. Only this report is added from that directory.

The implementation followed the approved design and TDD plan. `verification-before-completion` supplies the fresh-check discipline, and `technical-writing` supplies the evidence-report structure.

## Review fix round 1 of 5

Review reported four defects at `11283e4e9af85e25ec11d1bb6a86aa2468fb5712`. Their confirmed reproductions supersede the earlier self-review's no-known-blocker statement. This round fixes them in `877d0f6f42d8a70f32d056b75cd6103d1ff3b35d`.

The reviewer did not provide a saved probe script or command. The parent confirmed this. The probes below reproduce the described conditions through the real composed-JS harness; they are equivalent regressions, not an unchanged rerun of an unavailable original script.

| Finding | Confirmed RED | Fix and GREEN evidence |
| --- | --- | --- |
| P1: failed Workflow B read reuses A's facts | `workflow_id=wf-2` contained A's `impl-x`, `run-1`, judgment, actions, and receipt | The error path keeps cache only when its Workflow matches. Otherwise it creates an unknown projection with empty collections. The regression also proves that legacy recovery fallback cannot leak A's operation. |
| P2: rejection disables a later same-name action | `SAME_FORM=true`, `NEW_ENABLED=false`, `INTENT_ENABLED=false` | Capture a detached action snapshot. Compare canonical Workflow/target, expected identity, intent payload, and safety policy. Key ordering does not change identity. The rejected snapshot stays disabled; a changed version or intent stays enabled. Confirmation rejects changed snapshots, and identical rejected requests cannot reopen their form. Server-side deduplication is unchanged. |
| Late legacy completion hides Cockpit | `HIDDEN=true` while `SELECTED=__ctl__` | Legacy success and failure callbacks do not render the flow view while Cockpit is selected. The callback guard includes the request sequence and current Workflow target. |
| Late decisions do not update Cockpit | `DECISION=false` after the matching response | Matching decision success and failure repaint the current Cockpit. A foreign response or old Workflow callback cannot replace its data. |

The initial four regression tests returned **4 failed, 20 deselected** before the fixes and **4 passed, 20 deselected** after them. Additional probes cover legacy/decision request failure, explicit Workflow changes with late callbacks, and hidden Task-view ownership. The Task-view probes first returned `TASKS_UNTOUCHED=false` for both response types; they now pass because only the active view is repainted.

The full focused command listed above returned **80 passed, 73 subtests passed**. `git diff --check` exited 0. This round changes only:

- `console/static/console/scripts/controller-cockpit.js`
- `console/static/console/scripts/workflow-reader.js`
- `tests/test_console_cockpit_runtime.py`
- This report.

The real offline Chrome checks were repeated at 1440 × 1100 and 500 × 1000 using the final code. Both processes exited 0. Each reported `cacheIsolated=true`, `sameRejectedDisabled=true`, `newIdentityEnabled=true`, `legacyPreserved=true`, and `decisionRepainted=true`. Both retained the approved page, surface, and primary colors, with no horizontal overflow. The screenshots were visually inspected.

Round 1 artifacts are `/tmp/haflow-cockpit-r1-browser.JSqRex/round-1-final.html`, `final-0.png`, and `final-1.png`.

The browser probes use controlled responses, fresh temporary browser profiles, and no external requests. They validate browser behavior without a production-state mutation; they do not establish production UAT. The existing limits above still apply. No backend action registry, receipt persistence, runtime, deployment, or Task-board code changed.
