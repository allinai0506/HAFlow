# `herdr-task` Thin Entry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (recommended) or `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce `bin/herdr-task` from 7,548 lines to at most 500 while preserving every CLI and lifecycle contract.

**Architecture:** Keep the executable as a path bootstrap and delegate to `herdr.task_cli`. Move command orchestration into cohesive `herdr/task_cli_*.py` modules that call existing domain, persistence, delivery, recovery, and Git APIs. Migrate tests from script-global patching to owning-module seams while retaining subprocess CLI coverage.

**Tech Stack:** Python 3, pytest, argparse, HAFlow StateStore/SQLite, temporary Git repositories.

## Global Constraints

- Preserve CLI names, flags, defaults, output, JSON formats, exit codes, state transitions, schemas, and side effects.
- `bin/herdr-task` is at most 500 lines at final acceptance.
- Production `herdr/` modules never import or dynamically load `bin/herdr-task`.
- No forwarding-only modules, generic plugin registry, dependency injection framework, service container, or cycles.
- Test with temporary databases, directories, and Git repositories; do not start real Agents or write production state.
- Keep each command-family migration independently testable and behavior preserving.

---

## File and Ownership Map

- `bin/herdr-task`: executable path bootstrap, call to `herdr.task_cli.main(argv=None)`, and existing `WorkflowClosedError` message/exit handling only.
- `herdr/task_cli.py`: public CLI main, parser integration, and command-to-family routing. It must not reimplement handlers.
- `herdr/task_cli_execution.py` and focused task command modules as call-graph evidence requires: launch, capacity/preflight, dispatch, rework, status/adoption, and reconciliation orchestration. Reuse task resource and execution domain APIs.
- `herdr/task_cli_delivery.py` (new if no existing owner fits): commit, candidate attribution, integrate, cleanup/purge, finalize, delivery checks, and PR commands. Reuse `git_coordination.py`, `git_adoption.py`, `task_delivery.py`, `pr_delivery.py`, and delivery record modules.
- `herdr/task_cli_workflow.py` (new if no existing owner fits): workflow close/reopen, node status, supersede, stage reset, migration/recovery, and workflow-owned finalization. Reuse `workflow_close.py`, `workflow_recovery.py`, `workflow_repair_migration.py`, and StateStore.
- `herdr/task_cli_evidence.py` (new if no existing owner fits): review closure, notes, delivery records, obligations, and acceptance/operator evidence commands.
- Existing extracted modules remain owners for parser families, rollout, runs, observability, execution, resources, verification, Ops Center, adoption probe, and fingerprinting unless concrete call-graph evidence justifies moving a responsibility.
- Tests currently loading `bin/herdr-task` (including launch reconcile, delivery lifecycle, workflow lifecycle/finalize, review closure, gate/adoption, and note/delivery suites): move implementation-level imports and monkeypatches to owning modules; preserve executable subprocess checks for observable contracts.
- `tests/test_task_cli_architecture.py` (new): enforce entry line limit and import direction without asserting source text for behavior.
- `docs/superpowers/specs/2026-10-19-herdr-task-under-500-design.md`: update only if implementation discovery changes an approved boundary.

## Migration Principles

For each family, first identify the exact functions, imported globals, dynamic module shims, tests, and existing domain API owners. Add or adapt a behavior-level test around the current CLI/SQLite/temp-Git path before moving the code. Extract one ownership slice at a time, adjust direct dependency seams and tests in the same change, then run the focused suite and `git diff --check`. Preserve a runnable CLI after every migration PR. Avoid mechanical function forwarding and avoid changing state or side-effect ordering.

## Task 1: Characterize the Existing CLI Contract

**Files:**
- Modify: parser/subprocess tests only, plus new `tests/test_task_cli_entrypoint.py`
- Do not move `main()` yet: remaining handlers still live in `bin/herdr-task`, and importing those handlers back into `herdr/` would violate dependency direction.

**Interfaces:**
- Produces: behavior-level baseline tests for parser help, executable startup, and error/exit behavior.
- Consumes: the current executable and `herdr.task_cli_parser.build_parser()` without changing production code.

- [ ] Capture a baseline of `--help`, representative success/error stdout and stderr, and exit codes for one command from each existing parser family using subprocesses and temporary state.
- [ ] Add subprocess assertions in `tests/test_task_cli_entrypoint.py` for entry invocation, help, and the `WorkflowClosedError` user-facing message and exit status.
- [ ] Record representative help, success/error output, exit status, and temporary-StateStore readback for launch, delivery, workflow, and evidence command families.
- [ ] Keep `bin/herdr-task` unchanged in this task so every command remains runnable while handler ownership is migrated in Tasks 2–4.
- [ ] Run parser-family and entrypoint tests, compare captured CLI contract, and run `git diff --check`.

## Task 2: Extract Task Launch and Execution

**Files:**
- Modify: `herdr/task_cli_execution.py`; add a focused task launch module only if it owns substantive orchestration
- Modify: `bin/herdr-task` to import handlers from their owner modules while keeping its current parser and `main()` routing
- Test: `tests/test_launch_reconcile_cli.py`, `tests/test_pane_lifecycle_capacity.py`, `tests/test_scheduler_dispatch_e2e.py`, `tests/test_dispatch_idempotency.py`, `tests/test_delivery_rework.py`, `tests/test_issue_250_adopt_verdict_cli.py`, and related launch/status tests identified by monkeypatch inventory

**Interfaces:**
- Consumes: `herdr.task_resources`, `herdr.task_checkpoint`, workflow and router APIs, existing StateStore contracts, and family routing from Task 1.
- Produces: family-owned launch, dispatch, rework, status/adoption, and launch-reconciliation handlers imported by `herdr.task_cli`.

- [ ] Inventory references and patch sites for `_launch_task`, `launch_task`, `dispatch_task`, `cmd_rework`, `set_status`, `adopt_verdict_cli`, and `cmd_launch_reconcile`; list each durable write and external side effect in the migration notes.
- [ ] Add or update behavior tests for launch identity, capacity rejection, reconciliation recovery, dispatch idempotency, rework cleanup, and status/adoption outcomes using temporary state.
- [ ] Extract launch preflight and capacity orchestration while keeping resource registration and recovery behavior in existing domain APIs.
- [ ] Split `_launch_task` at named validation, resource reservation, execution start, and durable recovery boundaries; preserve ordering and failure compensation.
- [ ] Move dispatch, rework, status/adoption, and reconciliation handlers; change tests to patch their new owner modules' direct seams.
- [ ] Run the named focused suites and `git diff --check`; verify real CLI-to-SQLite state readback for at least launch and reconciliation.

## Task 3: Extract Git Delivery Lifecycle

**Files:**
- Create or modify: `herdr/task_cli_delivery.py`
- Modify: existing `bin/herdr-task` routing imports and move delivery handlers to the delivery owner; keep parser routing in place
- Test: `tests/test_delivery_lifecycle.py`, `tests/test_commit_adopt.py`, `tests/test_task_delivery.py`, `tests/test_finalize_git_index_wait.py`, `tests/test_cli_receipt_delivery.py`, `tests/test_delivered_base_frozen_pin.py`, and relevant temporary-Git tests

**Interfaces:**
- Consumes: `herdr.git_coordination`, `herdr.git_adoption`, `herdr.task_delivery`, `herdr.pr_delivery`, and existing receipt APIs.
- Produces: family-owned commit, integrate, cleanup/purge, finalize, delivery-check, and PR handlers.

- [ ] Inventory direct Git calls, repository locks, attribution helpers, test patch points, and persistence writes for delivery functions.
- [ ] Add or adapt temporary-Git behavior tests for commit attribution, integrate rejection/success, cleanup/finalize, frozen candidate identity, and delivery receipt validation.
- [ ] Move commit and candidate attribution handlers, then run commit/adoption/frozen-pin suites.
- [ ] Move integrate, cleanup/purge, and finalize orchestration, preserving lock scope, teardown guards, and recovery semantics.
- [ ] Move delivery checks and PR command routing; update patch sites to owning modules.
- [ ] Run the delivery suites against temporary Git repositories, verify persisted state/readback, and run `git diff --check`.

## Task 4: Extract Workflow Administration and Evidence

**Files:**
- Create or modify: `herdr/task_cli_workflow.py`, `herdr/task_cli_evidence.py`
- Modify: existing `bin/herdr-task` routing imports and move workflow/evidence handlers to their owners; keep parser routing in place
- Test: `tests/test_workflow_lifecycle_matrix.py`, `tests/test_workflow_finalize.py`, `tests/test_workflow_repair_contracts.py`, `tests/test_close_workflow_receipt_cli.py`, `tests/test_wrapup_review_closure.py`, `tests/test_note_add_fields.py`, `tests/test_zombie_obligation_regression.py`, and acceptance/delivery evidence tests

**Interfaces:**
- Consumes: existing workflow close/recovery/migration APIs and StateStore; existing evidence and delivery record APIs.
- Produces: workflow lifecycle handlers and evidence/operator record handlers imported by `herdr.task_cli`.

- [ ] Inventory workflow command reads/writes, close/reopen side effects, identity checks, and current script-global test patches.
- [ ] Add or adapt tests for close/reopen/node status/supersede/stage reset/migration/recovery, including SQLite readback and failure boundaries.
- [ ] Move workflow lifecycle and recovery command orchestration, preserving WorkflowClosedError handling and workflow-owned finalization semantics.
- [ ] Move review closure, notes, delivery records, obligations, and acceptance commands to their evidence owner modules.
- [ ] Migrate test imports and monkeypatches; retain subprocess coverage for CLI-visible behavior.
- [ ] Run workflow/evidence suites and `git diff --check`; verify no domain module imports the executable.

## Task 5: Move Routing and Close the Entry Boundary

**Files:**
- Create: `herdr/task_cli.py`
- Modify: `bin/herdr-task`, command-family modules, any production module that dynamically loads the script
- Create: `tests/test_task_cli_architecture.py`
- Test: all affected CLI, launch, delivery, workflow, evidence, and existing extracted-family tests

**Interfaces:**
- Produces: final `bin/herdr-task` at no more than 500 lines; `herdr.task_cli.main(argv=None)` owns parser routing; no production import path back to the executable.
- Consumes: all family handlers and subprocess contract tests from Tasks 1–4.

- [ ] Search the repository for imports, `SourceFileLoader` usage, `sys.modules` injection, copied globals, and monkeypatches targeting `bin/herdr-task`; migrate remaining in-repository call sites and confirm each parser handler already has an owning module.
- [ ] Move the existing parser dispatch into `herdr.task_cli.main(argv=None)` and keep its command selection, argument defaults, exception text, and exit statuses unchanged.
- [ ] Replace the old inline router with path bootstrap, delegation to `herdr.task_cli.main(argv=None)`, and the existing `WorkflowClosedError` message/exit contract.
- [ ] Remove dynamic compatibility proxies and duplicate handler copies only after callers use owning modules.
- [ ] Add architecture tests that assert the entry line count and production import direction using module/import metadata rather than handler source-text assertions.
- [ ] Confirm every registered parser command resolves to exactly one owning handler and run help/output/error/exit-code subprocess checks.
- [ ] Run focused suites for every migrated family, `python3 -m compileall -q herdr tests`, syntax-check `bin/herdr-task`, and `git diff --check`.
- [ ] Verify final line count with `wc -l bin/herdr-task` and record it alongside test results; run the repository-required CI gate on the merged candidate.

## Review and Delivery Checkpoints

- Keep the work on the approved isolated branch. Deliver command-family changes as reviewable PR-sized batches; do not merge or delete the sandbox/branch without separate authorization.
- After each task, review the complete affected call path and test seams, not only new module code.
- Final acceptance requires all spec criteria, including the 500-line limit, preserved real SQLite/temp-Git flows, no production script imports, and no known lifecycle contract changes.
