# `herdr-task` Thin Entry Design

**Status:** Approved for spec review; implementation has not started.

## Goal

Reduce `bin/herdr-task` to no more than 500 lines while preserving the CLI's command, output, exit-code, state, and side-effect contracts.

## Context

PR #269 extracted Ops Center, parser families, rollout, runs, observability, execution, resources, verification, Git probes, and workspace fingerprinting. The entry script still owns large launch, dispatch, Git delivery, workflow lifecycle, and inline command-routing implementations. `main()` and several handlers mix parsing, validation, persistence, subprocess calls, and command-specific output. Many tests load the script as a module and patch its globals, coupling test seams to the monolithic file.

The approved scope is the entry file: it must be at most 500 lines. New modules should follow HAFlow's responsibility and size guidance; 500 lines is a design target where it fits cohesion, not a requirement to split a cohesive operation mechanically.

## Goals

- Keep `bin/herdr-task` as the executable compatibility entry point, limited to path bootstrap, delegation, and the existing top-level error/exit contract.
- Put parser ownership and command routing in `herdr/task_cli.py` and command-family orchestration in focused modules.
- Reuse current `herdr/` domain, persistence, Git coordination, delivery, and recovery APIs instead of copying their logic.
- Remove imports from `herdr/` back into `bin/herdr-task` and remove dynamic compatibility proxies once in-repository callers and tests use the owning modules.
- Migrate tests to patch dependencies at the module that owns the behavior. Keep black-box CLI tests for observable command behavior.
- Deliver in independently testable command-family PRs, ending with an enforced `bin/herdr-task <= 500` line limit.

## Non-goals

- Change CLI names, flags, defaults, JSON formats, stdout/stderr behavior, exit codes, state transitions, persistence schemas, or external side effects.
- Redesign task lifecycle, workflow lifecycle, Git adoption, or delivery semantics.
- Require the total Task CLI implementation across all modules to fit within 500 lines.
- Add a generic plugin registry, dependency injection framework, service container, or a facade that only forwards every function unchanged.

## Architecture

### Executable boundary

`bin/herdr-task` remains executable and owns only:

1. Resolving and adding the repository root to `sys.path`.
2. Importing and invoking `herdr.task_cli.main(argv=None)`.
3. Preserving the existing `WorkflowClosedError` user-facing message and exit status.

It will not define business operations, parser branches, persistence helpers, or compatibility copies of command handlers.

### CLI application boundary

`herdr/task_cli.py` owns `main(argv=None)`, parser construction delegation, and command-to-handler routing. Each command family module owns the full argument-to-operation orchestration for its commands. The facade must not reproduce command behavior inline.

### Command-family modules

Use these responsibility groups, reusing existing modules where they already own the core behavior:

- **Task launch and execution:** launch setup, capacity/preflight checks, dispatch, rework, task status/adoption, and launch reconciliation. Split large operations such as `_launch_task` into named steps with a single state/side-effect boundary; keep launch identity and durable recovery contracts in the existing task-resource APIs.
- **Git delivery lifecycle:** commit, candidate attribution, integrate, cleanup/purge, finalize, and delivery checks. Reuse `herdr.git_coordination`, `herdr.git_adoption`, `herdr.task_delivery`, and existing receipt/PR modules.
- **Workflow administration:** close/reopen, node status, supersede, stage reset, migration/recovery, and workflow-owned finalization. Reuse `herdr.workflow_close`, `herdr.workflow_recovery`, `herdr.workflow_repair_migration`, and the authoritative StateStore APIs.
- **Evidence and operator records:** review closure, notes, delivery records, obligations, and acceptance commands, grouped by their existing evidence or delivery contract rather than by generic CRUD labels.
- **Existing extracted families:** keep parser, rollout, runs, observability, execution, resources, verification, Ops Center, adoption probe, and fingerprinting in their current responsibility modules unless call-graph evidence shows a boundary is wrong.

A command module may use subprocesses or persistence where the command itself is the imperative adapter. Pure status, identity, and workflow decisions remain in or move to reusable domain modules. Avoid a shared module that accumulates imports and globals merely to let every command keep its old patch points.

### Dependency and compatibility rules

- Dependencies flow from `bin/` to `herdr/`; production `herdr/` modules never load `bin/herdr-task`.
- A command module imports the specific core operations and adapters it owns. Cross-command sharing goes to an existing domain module or a focused shared module only when at least two real consumers need that behavior.
- Do not retain dynamic attribute-copying or `sys.modules` injection to emulate the old monolith namespace.
- Tests that verify a command implementation import its owning module and patch that module's direct external seams. CLI subprocess tests continue to validate the executable contract end to end.
- Keep environment and state paths resolved with the existing lifecycle semantics; do not silently change import-time configuration behavior during extraction.

## Migration sequence

1. **Characterize the existing entry contract:** capture parser, output, exit, and state behavior while keeping the currently runnable monolithic router intact.
2. **Task launch/execution:** move launch and dispatch orchestration, then rework/status/adoption flows, reducing oversized functions at their state and recovery boundaries. Keep the old router importing the new owning modules during migration.
3. **Git delivery:** move commit/integrate/cleanup/finalize and candidate attribution under delivery ownership; keep the old router importing the new owning modules during migration.
4. **Workflow administration and evidence:** move workflow lifecycle, notes, delivery records, acceptance, and remaining operator commands; keep the old router importing the new owning modules during migration.
5. **Thin-entry closeout:** after every command handler has an owner, move parser routing to `herdr.task_cli`, leave only bootstrap and error/exit handling in `bin/herdr-task`, remove compatibility loading, and add line-limit/import-direction guards. Verify every parser command, success/error output, and affected real persistence/Git flows.

Each migration PR must be behavior-preserving and independently testable. A PR may leave `bin/herdr-task` above 500 lines while the approved sequence is in progress; the final migration PR must satisfy the line-count guard. Update this document if source inspection shows a proposed boundary conflicts with an existing owner module.

## Acceptance

- `wc -l bin/herdr-task` is at most 500 on the final migration candidate.
- Every existing parser command remains available with the same flags and defaults.
- CLI help, successful output, JSON stdout, error stderr, and exit codes match the pre-refactor behavior for affected commands.
- Real CLI-to-StateStore/SQLite and temporary-Git flows remain covered for launch, rework, adoption, delivery, and workflow lifecycle paths.
- Tests patch owning modules; no production module imports or dynamically loads the executable script.
- No new circular imports are introduced. Existing required full CI runs on the merged candidate.
- No known state-transition, identity, persistence, locking, subprocess-timeout, or recovery contract changes.

## Risks and mitigations

- **Leaked dependencies from script globals:** inventory function references and test patches before each extraction; move a dependency with its owner and migrate its tests in the same PR.
- **Import-time environment behavior:** add tests for configured paths before moving constants; keep initialization at a documented process boundary.
- **Accidental behavior change in long command flows:** split at durable state and external side-effect boundaries; run existing real SQLite and temporary-Git integration tests after each family migration.
- **File-count churn without lower complexity:** do not create forwarding-only modules; each new module must own a command family or a reusable domain capability.
- **Too much scope in one change:** retain the approved multi-PR sequence and require a clean, reviewable behavior-preserving diff per family.
