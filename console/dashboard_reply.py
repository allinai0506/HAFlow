"""Dashboard reply: one sentence continues the stuck task.

Live workstations get an urgent steer. A failed task is relaunched with the
same agent and the sentence at the front of the prompt. Force-pass is not a
reply.
"""

from herdr.dashboard import reply_plan


def _load_task(task_id):
    from console import inventory
    from herdr import kernel as herdr_kernel

    task = next((item for item in inventory.tasks() if item.get("task_id") == task_id), None)
    if task:
        return task
    try:
        store = herdr_kernel.get_state_store() if hasattr(herdr_kernel, "get_state_store") else None
        if store:
            return store.get_task(task_id)
    except Exception:
        return None
    return None


def _reopen_task(task_id: str, target_status: str):
    from herdr import kernel

    store = kernel.get_state_store() if hasattr(kernel, "get_state_store") else None
    return kernel.transition_task(
        task_id=task_id,
        to_status=target_status,
        reason="dashboard_reply_resume",
        source="dashboard_reply",
        store=store,
    )


def _steer(plan, *, task=None, probe_pane=None, reopen_task=None):
    from herdr import steering, task_resources

    task_obj = task or _load_task(plan.get("task_id"))
    if not task_obj:
        return {"ok": False, "status": "blocked", "reason": "task_not_found"}

    # AGENTS.md Invariant 11: volatile pane IDs are not proof of ownership;
    # verify instance identity before dispatching urgent steering.
    checker = probe_pane or task_resources.owned_live_pane
    owned, reason = checker(task_obj)
    if not owned:
        return {
            "ok": False,
            "status": "blocked",
            "reason": reason or "pane_not_owned",
        }

    # Reopen paused / interrupted tasks so Controller task_listener and
    # completion receipts can consume agent working and done events.
    reopen_to = plan.get("reopen_to")
    if reopen_to:
        reopener = reopen_task or _reopen_task
        reopener(plan["task_id"], reopen_to)

    return steering.queue_steer(
        plan["task_id"],
        instruction=plan["instruction"],
        operator="human",
        urgent=True,
    )


def _launch(payload):
    from console.control_api import _execute_legacy_controller_action

    return _execute_legacy_controller_action(payload)


def _steer_error(result):
    reason = str((result or {}).get("reason") or (result or {}).get("status") or "")
    if reason in {
        "no_pane_id",
        "agent_not_found",
        "pane_not_owned",
        "identity_mismatch",
        "session_mismatch",
        "process_dead",
        "pane_dead",
        "unproven",
    }:
        return "这件任务没有活着的工位，回复送不进去。"
    if reason == "soft_steer_not_supported":
        return "当前工位不能接收这句话，回复没有送达。"
    return "回复没有送到工位，请再试一次。"


def api_dashboard_reply(
    body,
    *,
    load_task=None,
    steer=None,
    launch=None,
    probe_pane=None,
    reopen_task=None,
):
    body = body or {}
    task_id = str(body.get("task_id") or "").strip()
    if not task_id:
        raise RuntimeError("找不到这件任务")
    task = (load_task or _load_task)(task_id)
    if not task:
        raise RuntimeError("找不到这件任务")
    plan = reply_plan(task, body.get("instruction"))
    if not plan.get("ok"):
        raise RuntimeError(plan.get("error") or "回复没有送出")
    if plan["mode"] == "steer":
        if steer is not None:
            if probe_pane is not None:
                owned, reason = probe_pane(task)
                if not owned:
                    raise RuntimeError(_steer_error({"reason": reason or "pane_not_owned"}))
            reopen_to = plan.get("reopen_to")
            if reopen_to:
                reopener = reopen_task or _reopen_task
                reopener(task_id, reopen_to)
            result = steer(plan)
        else:
            result = _steer(
                plan,
                task=task,
                probe_pane=probe_pane,
                reopen_task=reopen_task,
            )
        if not result or result.get("ok") is False or result.get("status") == "blocked":
            raise RuntimeError(_steer_error(result))
        return {
            "ok": True,
            "mode": "steer",
            "message": plan["message"],
            "task_id": task_id,
        }
    result = (launch or _launch)(plan["launch"])
    return {
        "ok": True,
        "mode": "resume",
        "message": plan["message"],
        "task_id": plan["new_task_id"],
        "supersedes": task_id,
        "result": result,
    }
