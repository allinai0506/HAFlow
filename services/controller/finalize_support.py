"""Parse Git receipts, preserve worktree output and record finalize escalations.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def _should_allow_rebase(task):
    if not task or not isinstance(task, dict):
        return False
    if task.get("allow_rebase") is not None:
        return bool(task.get("allow_rebase"))
    if bool(task.get("onto")) or bool(task.get("onto_branch")):
        return True
    if bool(task.get("supersedes")):
        return True
    if bool(task.get("is_replay")) or bool(task.get("replay")):
        return True
    try:
        if int(task.get("dispatch_round") or 1) > 1:
            return True
    except (ValueError, TypeError):
        pass
    return False

# Identity facts pinned before integrate and re-checked afterwards: a change
# in any of them means the integrated result may not belong to this call.
_INTEGRATION_IDENTITY_KEYS = ('workflow_id', 'run_id', 'execution_id', 'clone_path', 'branch')


def _confirm_integration_result(ports, task, authorized_input_sha, entry_identity):
    """Prove the post-integrate HEAD belongs to this finalize's integrate call.

    A legal rebase rewrites this operation's own commit A into B, so the
    commit receipt alone cannot authorize later actions. B is attributable
    only when the persisted integration record and real Git objects explain
    exactly this A→B move: the recorded pre-rebase HEAD equals the authorized
    input, ``commit`` and ``integrated_commit`` agree on B, the task-scoped
    integration refs and the clone HEAD point at B, and B sits on the
    recorded integration base. Missing, foreign or conflicting evidence
    returns None (fail closed); the caller must stop instead of trusting the
    newest task snapshot. Read-only: every probe is a Git object lookup, and
    writes stay behind the caller's owned guards.
    """
    task = task or {}
    if task.get("status") != "integrated":
        return None
    if any(task.get(key) != (entry_identity or {}).get(key)
           for key in _INTEGRATION_IDENTITY_KEYS):
        return None
    head = task.get("commit")
    if not head or head != task.get("integrated_commit"):
        return None
    if not authorized_input_sha or task.get("integration_head_before_rebase") != authorized_input_sha:
        return None
    task_ref = f"refs/herdr/tasks/{task.get('task_id')}"
    integration_branch = task.get("integration_branch")
    base_sha = task.get("integration_base_sha")
    source_repo = task.get("source_repo")
    clone_path = task.get("clone_path")
    if (task.get("integration_ref") != task_ref or not integration_branch
            or not base_sha or not source_repo or not clone_path):
        return None

    def git_ref_sha(repo, ref):
        proc = ports.subprocess.run(
            ["git", "-C", repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            text=True, capture_output=True,
        )
        return proc.stdout.strip() if proc.returncode == 0 else None

    if (git_ref_sha(source_repo, task_ref) != head
            or git_ref_sha(source_repo, f"refs/heads/{integration_branch}") != head
            or git_ref_sha(clone_path, "HEAD") != head):
        return None
    ancestry = ports.subprocess.run(
        ["git", "-C", source_repo, "merge-base", "--is-ancestor", base_sha, head],
        text=True, capture_output=True,
    )
    if ancestry.returncode != 0:
        return None
    return head

def _settle_integration_result(ports, task, authorized_input_sha, entry_identity):
    """Confirm the integrated HEAD is attributable to this call, else escalate.

    Returns the confirmed HEAD SHA for the later guards, or None after
    escalating ``integrate_result_unverified``: the real integration result
    stays in place and a human adjudicates the evidence gap.
    """
    confirmed = _confirm_integration_result(ports, task, authorized_input_sha, entry_identity)
    if confirmed:
        return confirmed
    task_id = (task or {}).get("task_id") or "unknown"
    print(
        f"[FINALIZE REFUSED] "
        f"task={task_id} "
        f"reason=integrate_result_unverified"
    )
    ports._escalate_finalize(
        task,
        "integrate_result_unverified",
        {
            "authorized_input": authorized_input_sha,
            "commit": task.get("commit"),
            "integrated_commit": task.get("integrated_commit"),
            "integration_head_before_rebase": task.get("integration_head_before_rebase"),
            "integration_ref": task.get("integration_ref"),
            "integration_base_sha": task.get("integration_base_sha"),
        },
    )
    return None

def _preserve_none_mode_output(ports, task_id, task, own_head, guard):
    """Preserve worktree output and reconcile the head for none-mode finalize.

    Extracted verbatim from the git-finalize shell: probe the worktree, run
    the preservation commit through the normal commit rc handling, and adopt
    an unrecorded HEAD. Returns ``(own_head, refusal)``; ``refusal`` is a
    terminal outcome or None when the worktree is safe to release.
    """
    clone_path = task.get("clone_path")
    is_context_task = task.get("execution_mode") == "context"
    if not (clone_path and ports.os.path.exists(clone_path) and not is_context_task):
        return own_head, None
    status_res = ports.subprocess.run(
        ["git", "-C", clone_path, "status", "--porcelain", "--untracked-files=normal"],
        capture_output=True, text=True
    )
    if status_res.returncode != 0:
        from herdr.task_delivery import diagnostic_tail
        print(
            f"[FINALIZE ERROR] "
            f"task={task_id} "
            f"worktree status failed rc={status_res.returncode}: "
            f"{status_res.stderr.strip() or status_res.stdout.strip()}"
        )
        ports._record_finalize_event(
            task,
            "finalize_status_error",
            {
                "rc": status_res.returncode,
                "detail": diagnostic_tail(status_res.stdout, status_res.stderr),
            },
        )
        return own_head, {"retryable": True, "kind": "error", "rc": status_res.returncode}
    from herdr.repo_hygiene import teardown_output_diagnosis
    diag = teardown_output_diagnosis(status_res.stdout, task_id=task_id)

    head_res = ports.subprocess.run(
        ["git", "-C", clone_path, "rev-parse", "HEAD"],
        capture_output=True, text=True
    )
    head_sha = head_res.stdout.strip() if head_res.returncode == 0 else ""
    task_commit = task.get("commit")
    baseline = task.get("baseline_commit")
    unadopted_head = bool(head_sha and head_sha != task_commit and head_sha != baseline)

    if diag.get("reason") or unadopted_head:
        commit_env = dict(ports.os.environ)
        commit_env["HERDR_DEFER_HEAVY_TESTS"] = "1"
        guard("commit")
        commit_proc = ports.subprocess.run(
            [
                ports.TASK_MANAGER,
                "commit",
                task_id,
                "--message",
                f"chore(closure): preserve worktree output ({task_id})"
            ],
            text=True,
            capture_output=True,
            env=commit_env
        )
        commit_payload = ports._parse_commit_result(commit_proc.stdout)
        if commit_proc.returncode == 3:
            # Empty commit: no changes to adopt
            pass
        elif (commit_proc.returncode == 7 and commit_payload.get("task_id") == task_id
              and commit_payload.get("result") == "delivery_blocked"):
            delivery = commit_payload.get("delivery") or {}
            from herdr.task_delivery import record_delivery_failure
            if record_delivery_failure(task, delivery, store=ports._get_store()):
                print(f"[DELIVERY BLOCKED] task={task_id} waiting for scoped repair")
                return own_head, {"retryable": False, "kind": "delivery", "rc": 7}
            return own_head, {"retryable": False, "kind": "stale_delivery_result", "rc": 7}
        elif commit_proc.returncode == 4:
            print(
                f"[FINALIZE REFUSED] "
                f"task={task_id} "
                f"reason={commit_payload.get('reason')}"
            )
            ports._escalate_finalize(
                ports.get_task(task_id) or task,
                "commit_refused",
                commit_payload,
            )
            return own_head, {"retryable": False, "kind": "refused", "rc": 4}
        elif commit_proc.returncode == 75:
            print(
                f"[FINALIZE WAIT] "
                f"task={task_id} "
                f"reason=git_busy retry later"
            )
            if (commit_payload.get("task_id") == task_id
                    and commit_payload.get("result") == "wait"
                    and commit_payload.get("reason") == "git_index_lock"):
                return own_head, {"retryable": True, "kind": "wait", "rc": 75,
                        "reason": "git_index_lock", "budgeted": False}
            return own_head, {"retryable": True, "kind": "wait", "rc": 75}
        elif commit_proc.returncode != 0:
            from herdr.task_delivery import diagnostic_tail
            print(
                f"[FINALIZE ERROR] "
                f"task={task_id} "
                f"preservation commit failed rc={commit_proc.returncode}: "
                f"{commit_proc.stderr.strip() or commit_proc.stdout.strip()}"
            )
            ports._record_finalize_event(
                task,
                "finalize_commit_error",
                {
                    "rc": commit_proc.returncode,
                    "detail": diagnostic_tail(commit_proc.stdout, commit_proc.stderr),
                },
            )
            return own_head, {"retryable": True, "kind": "error", "rc": commit_proc.returncode}
        else:
            if commit_payload.get("task_id") == task_id and commit_payload.get("result") in ("created", "adopted"):
                own_head = commit_payload.get("commit") or None
            task = ports.get_task(task_id) or task

    if head_sha and head_sha != baseline:
        refreshed_commit = (ports.get_task(task_id) or task).get("commit")
        if not refreshed_commit:
            guard("commit", commit_head=head_sha)
            from herdr import kernel
            kernel.update_task_metadata(
                task_id,
                {"commit": head_sha, "commit_result": "reconciled"},
                store=ports._get_store(),
            )
    return own_head, None

def maybe_complete_on_task_done(ports, task_id, db_path=None):
    """Complete acknowledged handoffs whose target reached terminal done.

    Production hook for the authoritative task completion pipeline. Only
    ``acknowledged`` rows transition: a handoff never ACKed stays visible as
    stuck instead of being silently closed. Never raises.
    """
    if not ports.collaboration_enabled():
        return []
    try:
        from herdr import state_db as _sdb
        done = []
        for row in _sdb.list_collaboration_events(
            task_id=task_id, status="acknowledged", db_path=db_path,
        ):
            if row["to_task_id"] != task_id:
                continue
            done.append(_sdb.mark_collaboration_completed(row["event_id"], db_path=db_path))
        return done
    except Exception as exc:
        print(f"[COLLABORATION COMPLETE SKIPPED] task={task_id}: {type(exc).__name__}")
        return []

def _parse_commit_result(ports, output):
    """Extract HERDR_COMMIT_RESULT JSON from `herdr-task commit` output."""
    for line in (output or "").splitlines():
        if line.startswith("HERDR_COMMIT_RESULT="):
            try:
                payload = ports.json.loads(line.split("=", 1)[1])
            except ValueError:
                return {}
            return payload if isinstance(payload, dict) else {}
    return {}

def _parse_integrate_result(ports, output):
    """M-7: extract HERDR_INTEGRATE_RESULT JSON from integrate output."""
    for line in (output or "").splitlines():
        if line.startswith("HERDR_INTEGRATE_RESULT="):
            try:
                payload = ports.json.loads(line.split("=", 1)[1])
            except ValueError:
                return {}
            return payload if isinstance(payload, dict) else {}
    return {}

def _record_finalize_event(ports, task, event_type, payload):
    """Best-effort finalize observability event; never breaks finalization."""
    try:
        from herdr.trajectory import run_id_for_task

        store = ports._get_store()
        store.record_event(
            event_type,
            dict(payload or {}),
            workflow_id=(task or {}).get("workflow_id"),
            node_id=(task or {}).get("node") or (task or {}).get("stage"),
            task_id=(task or {}).get("task_id"),
            agent_id=(task or {}).get("agent"),
            source="controller",
            run_id=run_id_for_task(task or {}),
        )
    except (OSError, ValueError, RuntimeError, AttributeError,
             ports.subprocess.SubprocessError) as exc:
        print(f"[FINALIZE EVENT ERROR] {event_type}: {exc}")

def _escalate_finalize(ports, task, reason, detail=None):
    """Mark a git task escalated-to-human so close stops deferring on it."""
    task_id = (task or {}).get("task_id") or "unknown"
    payload = {"reason": reason, "status": (task or {}).get("status")}
    if detail:
        payload["detail"] = detail
    print(f"[FINALIZE ESCALATED] task={task_id} reason={reason}")
    ports._record_finalize_event(task, "finalize_escalated", payload)
    try:
        from herdr import kernel

        kernel.update_task_metadata(
            task_id,
            {
                "finalize_escalated": True,
                "finalize_escalate_reason": reason,
            },
            store=ports._get_store(),
        )
    except (OSError, ValueError, RuntimeError, AttributeError,
             ports.subprocess.SubprocessError) as exc:
        print(f"[FINALIZE ESCALATE ERROR] task={task_id}: {exc}")

def _empty_auto_releasable(ports, task):
    """H-3: anchored empties never auto-release; legacy time empties may.

    The old判据 returned True for any task with ``baseline_commit``, but
    ``commit_task`` EMPTY always persists ``commit_basis`` as
    ``baseline_commit``/``time``, so the ``empty_unreleasable`` branch was
    dead and every true vacuum task auto-advanced to ``cleanup_ready``.
    An anchor proves the *method* is credible, not that the *empty result*
    is credible: a completed git task that produced nothing needs a human.
    Only legacy anchor-less time-basis empties (same fail-closed timestamp
    guard as adoption) release; basis-absent records escalate.
    """
    task = task or {}
    if task.get("baseline_commit"):
        return False
    return task.get("commit_basis") == "time"

def clear_finalize_escalation(ports, task_id):
    """H-1: revoke machine-set finalize_escalated so finalize re-drives."""
    task = ports.get_task(task_id)
    if not task:
        return False
    if not task.get("finalize_escalated"):
        return True
    try:
        from herdr import kernel
        kernel.update_task_metadata(
            task_id,
            {"finalize_escalated": False,
             "finalize_escalate_reason": None},
            store=ports._get_store(),
        )
    except (OSError, ValueError, RuntimeError, AttributeError,
            ports.subprocess.SubprocessError) as exc:
        print(f"[FINALIZE UNCLEAR ERROR] task={task_id}: {exc}")
        return False
    ports.attention_clear(f"{task_id}:finalize")
    try:
        ports._finalize_retry_exhausted_logged.discard(task_id)
    except AttributeError:
        pass
    print(f"[FINALIZE UNESCALATED] task={task_id}")
    return True


DEPENDENCIES = {'maybe_complete_on_task_done': ('collaboration_enabled',),
 '_parse_commit_result': ('json',),
 '_parse_integrate_result': ('json',),
 '_record_finalize_event': ('_get_store', 'subprocess'),
 '_escalate_finalize': ('_get_store', '_record_finalize_event', 'subprocess'),
 '_empty_auto_releasable': (),
 'clear_finalize_escalation': ('_finalize_retry_exhausted_logged', '_get_store', 'attention_clear',
                               'get_task', 'subprocess')}

__all__ = tuple(DEPENDENCIES)
