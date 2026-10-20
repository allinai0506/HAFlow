"""Parse Git receipts and record finalization/escalation outcomes.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

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
