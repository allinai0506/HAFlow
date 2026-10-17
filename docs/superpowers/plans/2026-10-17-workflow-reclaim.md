# Workflow sandbox reclamation

Goal: preserve recoverable output outside clones before workflow-level deletion; task cleanup remains non-destructive for downstream tasks.

Approved design: archive and verify at final workflow close, delete only owned managed clones, retain and retry on failure. Keep Git integration and business gates. Archive failed tasks as diagnostics without changing failed to success. Serve existing deliverable IDs from archive after deletion.

Files: herdr/clone_archive.py (new), bin/herdr-task, herdr/deliverables.py, herdr/workflow_close.py, tests/test_workflow_clone_reclaim.py (new), affected lifecycle tests, wiki/task-lifecycle.md, wiki/log.md.

- [x] Write real CLI tests in isolated HOME/SQLite for none/context/failed task deletion plus archive recovery and repeat close.
- [x] Run tests RED; old close retains context and failed clones.
- [x] Implement bounded private tar recovery archive with per-file SHA256 and source identity/content recheck, atomic publication. Reject unsafe/symlink roots. Do not follow file symlinks. Fail on budget exceed.
- [x] Wire close-workflow only: preserve before deletion, keep existing Git output gate, archive docs output and failed diagnostics; require deletion success before workflow completion. Use existing lifecycle ownership and journal.
- [x] Make deliverable projection and preview/download read verified archive members without extraction, preserving sensitive-file rules and ownership.
- [x] Run focused tests GREEN and affected lifecycle/controller/receipt/security suites; update intentional retention expectations.
- [x] Review diff for deletion safety, interruption/retry, ownership and reader behavior; update Wiki and record verification. No push/merge/deploy.

Recovery refinement: first snapshot remains immutable across partial deletion; clone_reclaim_started binds deletion intent to manifest SHA. Retry rechecks original directory inode and permits only matching remaining entries.

Release-guard follow-up: archive eligibility and explicit override flags must agree. Preflight without writes, atomically enter closing, then publish archives/metadata. Require recovery archive at the actual Workflow delete branch even for a late-created clone. Dry-run reports conditional archive and reclaim for context/docs/failed. Add both pause-orderings, four override flags with staged/untracked Git output, late clone and archive write-retry regressions. Wait for asynchronous cloud review completion before merging.
