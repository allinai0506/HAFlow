"""Coordinator events responsibilities for the controller service.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def blocked_event_type(ports, task):
    """blocked 事件细分:内环耗尽走专属仲裁卡,其余走通用解除阻塞卡。

    内环协议(herdr-loop/evaluator)在重试耗尽时写 <clone>/.herdr-loop/
    BLOCKER.md 并输出 HERDR_TASK_BLOCKER 标记,Sentinel 置 blocked
    (sentinel_reason=inner_loop_exhausted)。此前 Controller 只发通用
    blocked 卡,工位自述的 BLOCKER.md 被丢弃——这是 Phase 0 设计的
    最后一跳(2026-09-13 设计,09-18 补齐)。
    """
    # Transition history is the current decision; metadata can survive a
    # later generic blocker. Retain the legacy fallback when no reason exists.
    history = (task or {}).get("status_history") or []
    latest = history[-1] if history and isinstance(history[-1], dict) else {}
    if latest.get("to") == "blocked" and latest.get("reason"):
        return (
            "inner_loop_exhausted"
            if latest.get("reason") == "inner_loop_exhausted"
            or latest.get("sentinel_reason") == "inner_loop_exhausted"
            else "blocked"
        )
    if task and task.get("sentinel_reason") == "inner_loop_exhausted":
        return "inner_loop_exhausted"
    return "blocked"

def blocker_queue_episode(ports, task):
    """Identity of the persisted blocker; metadata saves are not transitions."""
    from herdr.coordinator_events import blocker_episode

    return blocker_episode(task)

def enqueue_coordinator_event(ports, task, event_type):
    from herdr.coordinator_events import enqueue

    return enqueue(
        task,
        event_type,
        lock=ports.lock,
        queued_events=ports.queued_events,
        coordinator_queue=ports.coordinator_queue,
    )


DEPENDENCIES = {'blocked_event_type': (),
 'blocker_queue_episode': (),
 'enqueue_coordinator_event': ('coordinator_queue', 'lock', 'queued_events')}

__all__ = tuple(DEPENDENCIES)
