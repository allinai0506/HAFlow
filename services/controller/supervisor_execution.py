"""Execute and recover durable supervisor interventions under owner fencing.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def _execute_supervisor_intervention(ports, task, decision, action_handler, store=None):
    """Claim and execute one durable action, retaining legacy handler support."""
    intervention = decision.get("intervention") if isinstance(decision, dict) else None
    if not intervention:
        return action_handler(task, decision)
    store = store or ports._get_store()
    intervention_id = intervention.get("intervention_id")
    current = store.get_intervention(intervention_id)
    if current is None:
        raise RuntimeError("canonical intervention disappeared")
    if current.get("status") in ("completed", "failed", "superseded"):
        if current.get("status") == "failed":
            raise RuntimeError("intervention already failed")
        return current.get("result") or {"already_applied": True}
    claimed = store.claim_intervention(
        intervention_id,
        execution_owner=ports._INTERVENTION_EXECUTION_OWNER,
        recover_running=bool(decision.get("_recovery")),
    )
    if claimed is None:
        return {"already_claimed": True}
    owner = claimed.get("execution_owner")
    try:
        evidence = ports._intervention_execution_evidence(task, claimed, store)
        if evidence is not None:
            result = evidence
        elif action_handler in (ports._supervisor_retry, ports._supervisor_verify):
            result = action_handler(task, decision, store=store) or {}
        else:
            result = action_handler(task, decision) or {}
        store.complete_intervention(intervention_id, result, execution_owner=owner)
        return result
    except Exception as exc:
        from herdr.supervisor.state import redact_text
        error = getattr(exc, "intervention_error", None) or {
            "type": type(exc).__name__, "message": redact_text(str(exc)[:500]),
        }
        try:
            store.fail_intervention(intervention_id, error, execution_owner=owner)
        except ValueError:
            # A reclaimed owner or a concurrent terminal transition already
            # owns the durable outcome; do not mask the original action error.
            pass
        raise

def recover_pending_interventions(ports, store=None, run_id=None, task_id=None):
    """Recover requested/running actions before a done redelivery can pass."""
    store = store or ports._get_store()
    recovered = []
    pending = store.list_interventions(
        run_id=run_id, task_id=task_id, statuses=["requested", "running"],
    )
    for item in pending:
        task = store.get_task(item.get("task_id"))
        if not task:
            store.fail_intervention(item["intervention_id"], {"code": "task_missing"})
            continue
        from herdr.trajectory import run_id_for_task
        if str(run_id_for_task(task)) != str(item.get("run_id")):
            store.fail_intervention(item["intervention_id"], {"code": "run_mismatch"})
            continue
        decision = dict(item)
        decision["intervention"] = item
        decision["_recovery"] = True
        if item.get("action") == "RETRY":
            result = ports._execute_supervisor_intervention(task, decision, ports._supervisor_retry, store=store)
        elif item.get("action") == "VERIFY":
            result = ports._execute_supervisor_intervention(task, decision, ports._supervisor_verify, store=store)
        else:
            store.fail_intervention(item["intervention_id"], {"code": "unsupported_action"})
            continue
        recovered.append({"intervention": item, "result": result})
    return recovered

def supervisor_continue_flow(ports, result):
    """Whether the caller may run its default continuation (the done event).

    Fail-safe: no supervision result (disabled / skipped / crash) continues
    the original flow unchanged. An enforced intervention returns
    ``continue_flow=False`` so HAFlow orchestration owns the next step.
    """
    if not result:
        return True
    return bool(result.get("continue_flow", True))

def _supervisor_action_recovery_enabled(ports):
    """Read the current kill switch before replaying durable actions."""
    try:
        if ports.supervisor_harness is None:
            return False
        cfg = ports.supervisor_harness.load_config()
        from herdr.supervisor.config import supervisor_enabled
        return bool(cfg.get("enforce") and supervisor_enabled(cfg))
    except Exception:
        return False


DEPENDENCIES = {'_execute_supervisor_intervention': ('_INTERVENTION_EXECUTION_OWNER', '_get_store',
                                      '_intervention_execution_evidence', '_supervisor_retry',
                                      '_supervisor_verify'),
 'recover_pending_interventions': ('_execute_supervisor_intervention', '_get_store',
                                   '_supervisor_retry', '_supervisor_verify'),
 'supervisor_continue_flow': (),
 '_supervisor_action_recovery_enabled': ('supervisor_harness',)}

__all__ = tuple(DEPENDENCIES)
