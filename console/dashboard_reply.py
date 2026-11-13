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


def _steer(plan):
    from herdr import steering

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
    if reason in {"no_pane_id", "agent_not_found"}:
        return "这件任务没有活着的工位，回复送不进去。"
    if reason == "soft_steer_not_supported":
        return "当前工位不能接收这句话，回复没有送达。"
    return "回复没有送到工位，请再试一次。"


def api_dashboard_reply(body, *, load_task=None, steer=None, launch=None):
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
        result = (steer or _steer)(plan)
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
