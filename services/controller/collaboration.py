"""Deliver collaboration intents and acknowledgements with task/run ownership checks.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def _default_collab_sender(ports, pane_id, prompt):
    result = ports.subprocess.run(
        ["herdr", "agent", "prompt", str(pane_id), prompt],
        text=True, capture_output=True, timeout=120,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "collaboration prompt failed").strip()
        raise RuntimeError(f"collaboration dispatch failed: {detail[:400]}")
    return {"ok": True}

def _collab_task_pane(ports, task):
    pane = task.get("pane_id")
    if pane:
        return pane
    runtime = task.get("runtime") or {}
    return runtime.get("pane_id")

def _authoritative_task(ports, task_id, fallback, db_path=None):
    from herdr import state_db as _sdb
    return _sdb.get_task(str(task_id or ""), db_path=db_path)

def _legacy_evidence_allowed(ports, event, raw_ref, db_path=None):
    from herdr import state_db as _sdb
    from herdr.context_models import _canonical_evidence_ref
    from herdr.collaboration import collab_scope_for_task
    from herdr.trajectory import run_id_for_task

    ref = _canonical_evidence_ref(raw_ref) or str(raw_ref or "")
    if ":" not in ref:
        return False
    prefix, object_id = ref.split(":", 1)
    if prefix not in {"observation", "trajectory", "eval"} or not object_id:
        return False
    conn = _sdb.get_db_connection(db_path=db_path)
    try:
        if prefix == "observation":
            row = conn.execute(
                "SELECT run_id, task_id, workflow_id FROM observations WHERE observation_id = ?",
                (object_id,),
            ).fetchone()
        elif prefix == "eval":
            row = conn.execute(
                "SELECT run_id, task_id, workflow_id FROM eval_results WHERE eval_id = ?",
                (object_id,),
            ).fetchone()
        else:
            event_number = object_id.removeprefix("evt_")
            try:
                row = conn.execute(
                    "SELECT run_id, task_id, workflow_id FROM events WHERE id = ? AND source = 'trajectory'",
                    (int(event_number),),
                ).fetchone()
            except ValueError:
                row = None
        if row is None:
            return False
        row_workflow = str(row["workflow_id"] or "")
        event_workflow = str(event.get("workflow_id") or "")
        if row_workflow and row_workflow != event_workflow:
            return False
        task_id = row["task_id"]
        if task_id:
            task = _sdb.get_task(str(task_id), db_path=db_path)
            return bool(
                task
                and str(task.get("workflow_id") or "") == event_workflow
                and collab_scope_for_task(task) == str(event.get("run_id") or "")
                and str(run_id_for_task(task)) == str(row["run_id"] or "")
            )
        # A taskless fact has no persisted Task identity to bind it to this
        # Handoff. Compiler/storage accept taskless facts only through a
        # verified run-to-execution-scope map; the legacy prompt path has no
        # such snapshot, so fail closed instead of treating string equality as
        # provenance.
        return False
    finally:
        conn.close()

def _collab_task_run(ports, task):
    # Collaboration scope, NOT the per-task run_id: every herdr-task launch
    # mints its own run_id, so scope must be the shared workflow execution
    # identity. Fail closed when nothing identifies the execution.
    try:
        from herdr.collaboration import collab_scope_for_task
        return collab_scope_for_task(task)
    except Exception:
        return None

def collaboration_enabled(ports):
    import os as _os
    return _os.environ.get("HERDR_COLLABORATION_ENABLED", "1") != "0"

def _collab_prior_intent(ports, event_id, to_task_id, db_path):
    from herdr import state_db as _sdb
    try:
        rows = _sdb.list_events(
            event_type="collaboration_dispatch_intent",
            task_id=to_task_id, db_path=db_path,
        )
    except Exception:
        return None
    for row in rows or []:
        payload = row.get("payload") or {}
        if payload.get("collaboration_event_id") == event_id:
            return row
    return None

def _reconcile_collaboration_ack(ports, event_id, target, db_path):
    """ACK reconciliation shared by the dispatch and recovery paths.

    A target already working never emits another working transition, so a
    handoff created after that point would stick at dispatched without this
    check. Returns the acknowledged row, or None when not applicable.
    """
    from herdr import state_db as _sdb

    try:
        if (target.get("status") or "") == "working":
            return _sdb.mark_collaboration_acknowledged(event_id, db_path=db_path)
    except Exception as exc:
        print(f"[COLLABORATION ACK RECONCILE SKIPPED] event={event_id}: {type(exc).__name__}")
    return None

def _working_context_ref_valid(ports, event, target_task, db_path=None):
    """Validate V1 refs strictly; only an absent ref is legacy-compatible."""
    from herdr import state_db as _sdb
    from herdr.context_compiler import infer_agent_role
    from herdr.trajectory import run_id_for_task

    refs = list(event.get("context_refs") or [])
    authoritative_task = ports._authoritative_task(
        event.get("to_task_id"), target_task, db_path=db_path,
    )
    if authoritative_task is None:
        return False
    target_task = authoritative_task
    if str(target_task.get("workflow_id") or "") != str(event.get("workflow_id") or ""):
        return False
    if not refs:
        return True
    try:
        expected_role = infer_agent_role(target_task)
    except ValueError:
        return False
    for ref in refs:
        value = str(ref or "")
        if not value.startswith("wc_"):
            return False
        context = _sdb.get_working_context(value, db_path=db_path)
        if context is None:
            return False
        try:
            source_watermark = int(context.get("source_watermark") or 0)
        except (TypeError, ValueError):
            return False
        if len(value) <= 3 or not context.get("source_version") or source_watermark <= 0:
            return False
        if str(context.get("task_id") or "") != str(event.get("to_task_id") or ""):
            return False
        if str(context.get("workflow_id") or "") != str(event.get("workflow_id") or ""):
            return False
        if str(context.get("run_scope") or "") != str(event.get("run_id") or ""):
            return False
        expected_run_id = run_id_for_task(dict(target_task))
        if str(context.get("run_id") or "") != str(expected_run_id or ""):
            return False
        if str(context.get("agent_role") or "") != expected_role:
            return False
    return True

def dispatch_collaboration_event(ports, event_id, tasks_by_id, prompt_sender=None, db_path=None):
    """Dispatch one CollaborationEvent through the existing Herdr prompt path.

    Pure assembly: identity/run/pane checks, durable intent, minimal prompt,
    real ``herdr agent prompt`` delivery (injectable for tests). Never falls
    back to another pane or agent: missing target fails the event.
    """
    from herdr import collaboration as _collab
    from herdr import state_db as _sdb

    sender = prompt_sender or ports._default_collab_sender
    event = _sdb.get_collaboration_event(event_id, db_path=db_path)
    if event is None:
        raise ValueError(f"collaboration event '{event_id}' not found")
    if event["status"] in ("dispatched", "acknowledged", "completed"):
        return {"dispatched": True, "recovered": True,
                "status": event["status"], "event_id": event_id}
    if event["status"] == "failed":
        return {"dispatched": False, "status": "failed", "event_id": event_id}

    target = ports._authoritative_task(
        event["to_task_id"], (tasks_by_id or {}).get(event["to_task_id"]), db_path=db_path,
    )
    if target is None:
        return _sdb.mark_collaboration_failed(event_id, db_path=db_path)
    if (
        ports._collab_task_run(target) != event["run_id"]
        or str(target.get("workflow_id") or "") != str(event.get("workflow_id") or "")
    ):
        return _sdb.mark_collaboration_failed(event_id, db_path=db_path)
    pane_id = ports._collab_task_pane(target)
    if not pane_id:
        return _sdb.mark_collaboration_failed(event_id, db_path=db_path)
    source_task = ports._authoritative_task(
        event.get("from_task_id"), (tasks_by_id or {}).get(event.get("from_task_id")), db_path=db_path,
    )
    if (
        source_task is None
        or str(source_task.get("workflow_id") or "") != str(event.get("workflow_id") or "")
        or _collab.collab_scope_for_task(source_task) != str(event.get("run_id") or "")
    ):
        return _sdb.mark_collaboration_failed(event_id, db_path=db_path)
    if not ports._working_context_ref_valid(event, target, db_path=db_path):
        return _sdb.mark_collaboration_failed(event_id, db_path=db_path)
    prompt_event = dict(event)
    context_refs = event.get("context_refs") or []
    allowed_evidence_refs = set()
    for context_ref in context_refs:
        context_row = _sdb.get_working_context(context_ref, db_path=db_path)
        if context_row is None:
            continue
        for field_name in (
            "completed", "artifacts", "evidence", "findings", "decisions", "blockers",
            "open_questions", "verification", "handoffs",
        ):
            for item in context_row.get(field_name) or []:
                if field_name == "evidence" and item.get("source_ref"):
                    allowed_evidence_refs.add(str(item["source_ref"]))
                allowed_evidence_refs.update(
                    str(ref) for ref in item.get("evidence_refs") or [] if ref
                )
    if context_refs:
        from herdr.context_models import _canonical_evidence_ref
        filtered_evidence_refs = []
        for raw_ref in event.get("evidence_refs") or []:
            canonical_ref = _canonical_evidence_ref(raw_ref) or str(raw_ref)
            if canonical_ref in allowed_evidence_refs:
                filtered_evidence_refs.append(canonical_ref)
        prompt_event["evidence_refs"] = filtered_evidence_refs
    else:
        prompt_event["evidence_refs"] = [
            str(raw_ref)
            for raw_ref in event.get("evidence_refs") or []
            if ports._legacy_evidence_allowed(event, raw_ref, db_path=db_path)
        ]

    if ports._collab_prior_intent(event_id, event["to_task_id"], db_path) is not None:
        recovered = _sdb.mark_collaboration_dispatched(event_id, db_path=db_path)
        recovered["recovered"] = True
        recovered["dispatched"] = True
        acked = ports._reconcile_collaboration_ack(event_id, target, db_path)
        if acked is not None:
            acked["recovered"] = True
            acked["dispatched"] = True
            return acked
        return recovered

    _sdb.record_event(
        {"event_type": "collaboration_dispatch_intent",
         "workflow_id": event.get("workflow_id"),
         "task_id": event["to_task_id"],
         "agent_id": event.get("to_agent"),
         "run_id": event["run_id"],
         "payload": {"collaboration_event_id": event_id, "pane_id": pane_id},
         "source": "collaboration"},
        db_path=db_path,
    )
    prompt = _collab.build_handoff_prompt(
        prompt_event, next_action=f"Proceed as {event.get('to_agent') or ''}.",
    )
    try:
        sender(pane_id, prompt)
    except Exception:
        return _sdb.mark_collaboration_failed(event_id, db_path=db_path)
    marked = _sdb.mark_collaboration_dispatched(event_id, db_path=db_path)
    marked["dispatched"] = True
    # ACK fast-path: the target may already be working (it was launched
    # before this event existed, so the working-transition hook never fired
    # for it). Reconcile immediately instead of waiting for a transition
    # that may never come.
    acked = ports._reconcile_collaboration_ack(event_id, target, db_path)
    if acked is not None:
        acked["dispatched"] = True
        return acked
    return marked

def ack_collaboration_event_for_task(ports, to_task_id, db_path=None):
    """ACK all dispatched handoffs targeting a task that just entered working."""
    from herdr import state_db as _sdb

    acked = []
    for row in _sdb.list_collaboration_events(
        task_id=to_task_id, status="dispatched", db_path=db_path,
    ):
        if row["to_task_id"] != to_task_id:
            continue
        acked.append(_sdb.mark_collaboration_acknowledged(row["event_id"], db_path=db_path))
    return acked

def maybe_ack_on_working(ports, task_id, db_path=None):
    """Accelerator hook for the working transition: never breaks the caller."""
    if not ports.collaboration_enabled():
        return []
    try:
        return ports.ack_collaboration_event_for_task(task_id, db_path=db_path)
    except Exception as exc:
        print(f"[COLLABORATION ACK SKIPPED] task={task_id}: {type(exc).__name__}")
        return []


DEPENDENCIES = {'_default_collab_sender': ('subprocess',),
 '_collab_task_pane': (),
 '_authoritative_task': (),
 '_legacy_evidence_allowed': (),
 '_collab_task_run': (),
 'collaboration_enabled': (),
 '_collab_prior_intent': (),
 '_reconcile_collaboration_ack': (),
 '_working_context_ref_valid': ('_authoritative_task',),
 'dispatch_collaboration_event': ('_authoritative_task', '_collab_prior_intent',
                                  '_collab_task_pane', '_collab_task_run', '_default_collab_sender',
                                  '_legacy_evidence_allowed', '_reconcile_collaboration_ack',
                                  '_working_context_ref_valid'),
 'ack_collaboration_event_for_task': (),
 'maybe_ack_on_working': ('ack_collaboration_event_for_task', 'collaboration_enabled')}

__all__ = tuple(DEPENDENCIES)
