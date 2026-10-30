"""Reconcile persisted task status with the authoritative terminal runtime.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""


# Crash recovery / startup reconciliation
def get_agent_runtime_status(ports, pane_id):
    try:
        output = ports.subprocess.check_output(
            [
                "herdr",
                "agent",
                "get",
                pane_id
            ],
            text=True,
            timeout=10,
        )

        data = ports.json.loads(output)

        return (
            data["result"]["agent"]
            .get("agent_status", "unknown")
        )

    except Exception as e:
        print(
            f"[RECOVERY STATUS ERROR] "
            f"pane={pane_id}: {e}"
        )
        return None

def reconcile_task_state(ports, task_id):
    task = ports.get_task(task_id)

    if not task:
        return

    current = task.get("status")

    if current in (
        "completed",
        "committed",
        "integrated",
        "cleanup_ready",
        "cleaned",
        "failed"
    ):
        return

    # Registry 已经知道 Agent 执行结束，
    # 但 Controller 可能在通知总指挥前重启。
    if current == "agent_done":
        print(
            f"[RECOVERY] "
            f"task={task_id} "
            f"registry=agent_done "
            f"→ restore done event"
        )

        ports.emit_done_if_allowed(task)
        return

    pane_id = task.get("pane_id")

    if not pane_id:
        return

    runtime = ports.get_agent_runtime_status(
        pane_id
    )

    if runtime is None:
        return

    print(
        f"[RECOVERY] "
        f"task={task_id} "
        f"registry={current} "
        f"agent={runtime}"
    )

    if (
        current == "blocked"
        and ports.blocked_event_type(task) == "inner_loop_exhausted"
        and runtime in {"working", "idle", "done"}
    ):
        # Recover the volatile arbitration queue, keeping the durable blocker.
        ports.enqueue_coordinator_event(task, "inner_loop_exhausted")
        return

    # --------------------------------
    # Agent 当前正在运行
    # --------------------------------
    if runtime == "working":
        if current in (
            "dispatched",
            "blocked",
            "rework"
        ):
            ports._set_observed_status(task, "working", "recovery_working")
        return

    # --------------------------------
    # Agent 当前 blocked
    # --------------------------------
    if runtime == "blocked":
        if current in (
            "dispatched",
            "working",
            "rework"
        ):
            if not ports._set_observed_status(task, "blocked", "recovery_blocked"):
                return

        task = ports.get_task(task_id)

        if task and task.get("status") == "blocked":
            ports.enqueue_coordinator_event(
                task,
                ports.blocked_event_type(task)
            )

        return

    # --------------------------------
    # Agent 已经 done，但 Controller
    # 错过了 working/done 事件
    # --------------------------------
    if runtime == "done":
        current = ports.get_task(task_id).get(
            "status"
        )

        if current == "dispatched":
            if not ports._set_observed_status(
                task,
                "working",
                "recovery_working",
            ):
                return
            current = "working"

        elif current == "blocked":
            if not ports._set_observed_status(
                task,
                "working",
                "recovery_working",
            ):
                return
            current = "working"

        elif current == "rework":
            has_done_marker, _screen = ports._completion_marker_snapshot(task)
            ports._record_completion_sample(
                task,
                has_done_marker,
                agent_status="idle" if runtime == "idle" else "done",
            )
            return

        if current == "working":
            has_done_marker, _screen = ports._completion_marker_snapshot(task)
            ports._record_completion_sample(
                task,
                has_done_marker,
                agent_status="idle" if runtime == "idle" else "done",
            )
            return

        task = ports.get_task(task_id)

        if task and task.get("status") == "agent_done":
            ports.emit_done_if_allowed(task)

        return

    # Agent 曾经进入 working/rework，随后 Controller 重启时发现已经 idle
    if runtime in {"idle", "done"}:
        if current in {"dispatched", "working"}:
            is_fresh = False
            if hasattr(ports, "_gate_verdict_is_fresh"):
                if not ports._gate_verdict_is_fresh(task):
                    if hasattr(ports, "sync_verdict_from_business_acceptance"):
                        ports.sync_verdict_from_business_acceptance(task)
                is_fresh = ports._gate_verdict_is_fresh(task)
            if is_fresh and getattr(ports, "gate_verdict_ready", lambda t: False)(task):
                has_done_marker, screen = ports._completion_marker_snapshot(task)
                if ports._set_observed_status(task, "agent_done", "gate_verdict_recovery"):
                    ports.emit_done_if_allowed(ports.get_task(task_id), report_text=screen)
                return

    if runtime == "idle":
        if current in {"working", "rework"}:
            has_done_marker, _screen = ports._completion_marker_snapshot(task)
            ports._record_completion_sample(task, has_done_marker, agent_status="idle")
        return

    # dispatched -> idle 不自动推断完成；
    # unknown 也不自动推断。
    print(
        f"[RECOVERY NOOP] "
        f"task={task_id} "
        f"agent={runtime}"
    )


DEPENDENCIES = {'get_agent_runtime_status': ('json', 'subprocess'),
 'reconcile_task_state': ('_completion_marker_snapshot', '_gate_verdict_is_fresh',
                          '_record_completion_sample', '_set_observed_status',
                          'blocked_event_type', 'emit_done_if_allowed',
                          'enqueue_coordinator_event', 'gate_verdict_ready',
                          'get_agent_runtime_status', 'get_task',
                          'sync_verdict_from_business_acceptance')}

__all__ = tuple(DEPENDENCIES)
