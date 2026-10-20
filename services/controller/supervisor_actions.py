"""Deliver supervised retry/verification actions using existing task primitives.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def _supervisor_pane_report(ports, task):
    """Agent done report source: bounded pane read, best-effort (never raises)."""
    pane_id = task.get("pane_id")
    if not pane_id:
        return None
    try:
        res = ports.subprocess.run(
            ["herdr", "pane", "read", pane_id, "--source", "visible"],
            text=True,
            capture_output=True,
            timeout=3,
        )
        return (res.stdout or "") + "\n" + (res.stderr or "")
    except Exception:
        return None

def _supervisor_attention(ports, task, decision, event_type):
    """Intervention ledger entry; task status is never touched here."""
    reasons = "; ".join((decision or {}).get("reasons") or ["policy intervention"])
    key = f"supervisor:{task.get('task_id')}"
    previous = ports.attention_get(key) or {}
    note = ports.attention_note(key, task, event_type, reasons)
    if previous.get("event_type") != event_type:
        ports._supervisor_notify(
            task,
            event_type.split("_")[-1],
            f"语义监督拦截: {reasons}",
        )
    return note

def _supervisor_notify(ports, task, action, message):
    """Best-effort operator visibility for an enforced intervention."""
    if ports.os.environ.get("HERDR_CONTROLLER_TEST"):
        return
    ports.notify_attention(
        "Herdr Factory · 语义监督拦截",
        task,
        message,
        f"supervisor_{str(action).lower()}",
    )

def _supervisor_retry(ports, task, decision, store=None):
    """RETRY -> the existing rework flow; return only observed state facts."""
    return ports._supervisor_retry_with_store(task, decision, store=store)

def _intervention_metadata(ports, decision, action, attempt_count=None):
    intervention = (decision.get("intervention") or {}) if isinstance(decision, dict) else {}
    metadata = {
        "intervention_id": intervention.get("intervention_id"),
        "decision_id": intervention.get("decision_id") or decision.get("decision_id"),
        "action": action,
    }
    if attempt_count is not None:
        metadata["attempt_count"] = int(attempt_count)
    return {key: value for key, value in metadata.items() if value is not None}

def _supervisor_retry_with_store(ports, task, decision, store=None):
    """Apply one new retry iteration using the existing legal task flow."""
    from herdr.intervention import attempt_count_for_task
    store = store or ports._get_store()
    fresh_before = store.get_task(task.get("task_id")) if store is not None else None
    fresh_before = fresh_before or task
    previous_status = fresh_before.get("status")
    current_attempt = attempt_count_for_task(fresh_before)
    intervention = (decision.get("intervention") or {}) if isinstance(decision, dict) else {}
    max_attempts = int(intervention.get("max_attempts") or 0)
    if max_attempts > 0 and current_attempt >= max_attempts:
        error = RuntimeError("RETRY budget exhausted")
        error.intervention_error = {
            "code": "retry_budget_exhausted",
            "attempt_count": current_attempt,
            "max_attempts": max_attempts,
        }
        raise error

    # A task already in rework is active work, not evidence that this new
    # Intervention ran. Re-enter the legal working path to start a new
    # iteration; never use rework -> rework as a fake retry.
    target_status = "working" if previous_status == "rework" else "rework"
    next_attempt = current_attempt + 1
    metadata = ports._intervention_metadata(decision, "RETRY", next_attempt)
    applied_status = target_status
    if store is not None and store.get_task(task.get("task_id")) is not None:
        from herdr import kernel
        kernel.transition_task(
            task_id=task.get("task_id"),
            to_status=target_status,
            reason="supervisor_retry",
            source="supervisor",
            metadata=metadata,
            store=store,
        )
    elif not ports.set_task_status(task.get("task_id"), target_status):
        raise RuntimeError("RETRY handler could not enter retry flow")
    fresh = store.get_task(task.get("task_id")) if store is not None else None
    fresh = fresh or ports.get_task(task.get("task_id")) or dict(fresh_before, status=applied_status)
    dispatch = ports._dispatch_supervisor_retry(fresh_before, decision, store)
    return {
        "action": "RETRY",
        "previous_status": previous_status,
        "new_status": fresh.get("status", target_status),
        "attempt_count": attempt_count_for_task(fresh),
        **dispatch,
    }

def _dispatch_supervisor_retry(ports, task, decision, store):
    """Dispatch a real new retry iteration through the existing Agent path."""
    intervention = decision.get("intervention") or {}
    pane_id = task.get("pane_id")
    if not pane_id:
        raise RuntimeError("RETRY dispatch requires task pane_id")
    intervention_id = intervention.get("intervention_id")
    decision_id = intervention.get("decision_id") or decision.get("decision_id")
    from herdr.trajectory import run_id_for_task
    prior_intent = ports._latest_action_dispatch_intent(
        task, store, "RETRY", intervention_id=intervention_id,
    )
    if prior_intent is not None and task.get("completion_protocol") != "receipt-v1":
        payload = dict(prior_intent.get("payload") or {})
        payload["dispatch_recovered"] = True
        store.record_event(
            "retry_dispatched", payload,
            workflow_id=task.get("workflow_id"),
            node_id=task.get("node") or task.get("stage"),
            task_id=task.get("task_id"), agent_id=task.get("agent"),
            source="supervisor", run_id=run_id_for_task(task),
        )
        return {"retry_dispatched": True, "dispatch_recovered": True}
    payload = {
        "intervention_id": intervention_id,
        "decision_id": decision_id,
        "action": "RETRY",
        "pane_id": pane_id,
        "attempt_count": intervention.get("attempt"),
        "verification_pending": False,
    }
    working_context_ref = ports._working_context_ref_for_task(task, store=store)
    if working_context_ref:
        payload["working_context_id"] = working_context_ref
    if task.get("completion_protocol") != "receipt-v1":
        store.record_event(
            "retry_dispatch_intent", payload,
            workflow_id=task.get("workflow_id"),
            node_id=task.get("node") or task.get("stage"),
            task_id=task.get("task_id"), agent_id=task.get("agent"),
            source="supervisor", run_id=run_id_for_task(task),
        )

    from herdr.workflow_docs import cli_path
    import shlex
    artifact_task = shlex.quote(str(cli_path()))
    prompt = (
        "Supervisor requested a fresh retry iteration for this task.\n"
        "Continue the existing rework/fix loop now, run the configured work "
        "and verification steps, and only report completion after the new "
        "iteration has actually executed.\n"
        f"HERDR_RETRY_INTERVENTION_ID:{intervention_id}\n"
        f"HERDR_RETRY_DECISION_ID:{decision_id}\n"
        "HERDR_RETRY_ACTION:RETRY\n"
        + (f"WORKING_CONTEXT_REF:{working_context_ref}\n" if working_context_ref else "")
        + "Load the immutable context by reference before retrying.\n"
        + (f"{artifact_task} working-context get --context-id {working_context_ref}\n"
           if working_context_ref else "")
    )
    if task.get("completion_protocol") == "receipt-v1":
        from herdr.supervisor_delivery import deliver
        def send(pane, text):
            from herdr.supervisor_delivery import current_delivery_task
            pane = current_delivery_task(task, store, native=True)['pane_id']
            result = ports.subprocess.run(
                ["herdr", "agent", "prompt", str(pane), text],
                text=True, capture_output=True, timeout=ports.SUPERVISOR_VERIFY_DISPATCH_TIMEOUT,
            )
            if result.returncode != 0:
                raise RuntimeError("Supervisor native prompt transport failed")
        return deliver(task, store, 'RETRY', payload, prompt, send)
    result = ports.subprocess.run(
        ["herdr", "agent", "prompt", str(pane_id), prompt],
        text=True, capture_output=True, timeout=ports.SUPERVISOR_VERIFY_DISPATCH_TIMEOUT,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "retry prompt failed").strip()
        raise RuntimeError(f"RETRY dispatch failed: {detail[:400]}")
    store.record_event(
        "retry_dispatched", payload,
        workflow_id=task.get("workflow_id"),
        node_id=task.get("node") or task.get("stage"),
        task_id=task.get("task_id"), agent_id=task.get("agent"),
        source="supervisor", run_id=run_id_for_task(task),
    )
    return {"retry_dispatched": True}

def _supervisor_verify(ports, task, decision, store=None):
    """VERIFY -> re-enter the existing verification/rework route.

    Verification facts are emitted later by the existing tests_completed and
    verification_completed path; this handler never manufactures a verdict.
    """
    store = store or ports._get_store()
    fresh_before = store.get_task(task.get("task_id")) if store is not None else None
    fresh_before = fresh_before or task
    previous_status = fresh_before.get("status")
    applied_status = previous_status
    if previous_status != "rework":
        metadata = ports._intervention_metadata(decision, "VERIFY")
        if store is not None and store.get_task(task.get("task_id")) is not None:
            from herdr import kernel
            kernel.transition_task(
                task_id=task.get("task_id"),
                to_status="rework",
                reason="supervisor_verify",
                source="supervisor",
                metadata=metadata,
                store=store,
            )
        elif not ports.set_task_status(task.get("task_id"), "rework"):
            raise RuntimeError("VERIFY handler could not enter verification rework")
        else:
            applied_status = "rework"
    dispatch = ports._dispatch_supervisor_verification(fresh_before, decision, store)
    fresh = store.get_task(task.get("task_id")) if store is not None else None
    fresh = fresh or ports.get_task(task.get("task_id")) or dict(fresh_before, status=applied_status)
    return {
        "action": "VERIFY",
        "previous_status": previous_status,
        "new_status": fresh.get("status"),
        **dispatch,
        "verification_requested": True,
        "verification_pending": True,
    }

def _working_context_ref_for_task(ports, task, store=None):
    try:
        from herdr.context_compiler import compile_working_context, infer_agent_role
        context = compile_working_context(
            workflow_id=task.get("workflow_id"),
            task_id=task.get("task_id"),
            agent_role=infer_agent_role(task),
            store=store,
        )
        return context.context_id
    except Exception as exc:
        print(f"[WORKING_CONTEXT SKIPPED] task={task.get('task_id')}: {type(exc).__name__}")
        return None

def _dispatch_supervisor_verification(ports, task, decision, store):
    """Submit one real verification prompt through the existing Agent path."""
    intervention = decision.get("intervention") or {}
    pane_id = task.get("pane_id")
    if not pane_id:
        raise RuntimeError("VERIFY dispatch requires task pane_id")
    intervention_id = intervention.get("intervention_id")
    decision_id = intervention.get("decision_id") or decision.get("decision_id")
    from herdr.trajectory import run_id_for_task
    prior_intent = ports._latest_verification_dispatch_intent(
        task, store, intervention_id=intervention_id,
    )
    if prior_intent is not None and task.get("completion_protocol") != "receipt-v1":
        payload = dict(prior_intent.get("payload") or {})
        payload["dispatch_recovered"] = True
        store.record_event(
            "verification_dispatched",
            payload,
            workflow_id=task.get("workflow_id"),
            node_id=task.get("node") or task.get("stage"),
            task_id=task.get("task_id"),
            agent_id=task.get("agent"),
            source="supervisor",
            run_id=run_id_for_task(task),
        )
        return {"verification_dispatched": True, "dispatch_recovered": True}
    dispatch_started_at = ports.time.time()
    evidence_baseline = ports._verification_snapshot(task.get("clone_path"))
    dispatch_payload = {
        "intervention_id": intervention_id,
        "decision_id": decision_id,
        "action": "VERIFY",
        "pane_id": pane_id,
        "dispatch_started_at": dispatch_started_at,
        "evidence_baseline": evidence_baseline,
        "verification_pending": True,
    }
    working_context_ref = ports._working_context_ref_for_task(task, store=store)
    if working_context_ref:
        dispatch_payload["working_context_id"] = working_context_ref
    if task.get("completion_protocol") != "receipt-v1":
        store.record_event(
            "verification_dispatch_intent",
            dispatch_payload,
            workflow_id=task.get("workflow_id"),
            node_id=task.get("node") or task.get("stage"),
            task_id=task.get("task_id"),
            agent_id=task.get("agent"),
            source="supervisor",
            run_id=run_id_for_task(task),
        )

    from herdr.workflow_docs import cli_path
    import shlex
    artifact_task = shlex.quote(str(cli_path()))
    artifact_loop = shlex.quote(str(cli_path().with_name('herdr-loop')))
    prompt = (
        "Supervisor requested a fresh verification execution for this task.\n"
        "Run the existing project verification/test loop now (including "
        f"{artifact_loop} eval when configured), inspect the resulting "
        ".herdr-loop/EVAL_DONE.json, and only report completion after the new "
        "verification evidence is written.\n"
        f"HERDR_VERIFY_INTERVENTION_ID:{intervention_id}\n"
        f"HERDR_VERIFY_DECISION_ID:{decision_id}\n"
        "HERDR_VERIFY_ACTION:VERIFY\n"
        + (f"WORKING_CONTEXT_REF:{working_context_ref}\n" if working_context_ref else "")
        + "Load the immutable context by reference before verification.\n"
        + (f"{artifact_task} working-context get --context-id {working_context_ref}\n"
           if working_context_ref else "")
    )
    if task.get("completion_protocol") == "receipt-v1":
        from herdr.supervisor_delivery import deliver
        def send(pane, text):
            from herdr.supervisor_delivery import current_delivery_task
            pane = current_delivery_task(task, store, native=True)['pane_id']
            result = ports.subprocess.run(
                ["herdr", "agent", "prompt", str(pane), text],
                text=True, capture_output=True, timeout=ports.SUPERVISOR_VERIFY_DISPATCH_TIMEOUT,
            )
            if result.returncode != 0:
                raise RuntimeError("Supervisor native prompt transport failed")
        return deliver(task, store, 'VERIFY', dispatch_payload, prompt, send)
    result = ports.subprocess.run(
        ["herdr", "agent", "prompt", str(pane_id), prompt],
        text=True,
        capture_output=True,
        timeout=ports.SUPERVISOR_VERIFY_DISPATCH_TIMEOUT,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "verification prompt failed").strip()
        raise RuntimeError(f"VERIFY dispatch failed: {detail[:400]}")
    store.record_event(
        "verification_dispatched",
        dispatch_payload,
        workflow_id=task.get("workflow_id"),
        node_id=task.get("node") or task.get("stage"),
        task_id=task.get("task_id"),
        agent_id=task.get("agent"),
        source="supervisor",
        run_id=run_id_for_task(task),
    )
    return {"verification_dispatched": True}


DEPENDENCIES = {'_supervisor_pane_report': ('subprocess',),
 '_supervisor_attention': ('_supervisor_notify', 'attention_get', 'attention_note'),
 '_supervisor_notify': ('notify_attention', 'os'),
 '_supervisor_retry': ('_supervisor_retry_with_store',),
 '_intervention_metadata': (),
 '_supervisor_retry_with_store': ('_dispatch_supervisor_retry', '_get_store',
                                  '_intervention_metadata', 'get_task', 'set_task_status'),
 '_dispatch_supervisor_retry': ('SUPERVISOR_VERIFY_DISPATCH_TIMEOUT',
                                '_latest_action_dispatch_intent', '_working_context_ref_for_task',
                                'subprocess'),
 '_supervisor_verify': ('_dispatch_supervisor_verification', '_get_store', '_intervention_metadata',
                        'get_task', 'set_task_status'),
 '_working_context_ref_for_task': (),
 '_dispatch_supervisor_verification': ('SUPERVISOR_VERIFY_DISPATCH_TIMEOUT',
                                       '_latest_verification_dispatch_intent',
                                       '_verification_snapshot', '_working_context_ref_for_task',
                                       'subprocess', 'time')}

__all__ = tuple(DEPENDENCIES)
