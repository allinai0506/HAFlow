"""Coordinator decision waiting and retry delivery through explicit callbacks."""

import time


def handle_delivery_stall(
    item, task, elapsed, *, attention_get, attention_note,
    attention_retry_interval, coordinator_pane_for_workflow, notify_attention,
):
    from herdr.reason_labels import human_reason

    key = item["key"]
    event_type = item["event_type"]
    workflow_id = task.get("workflow_id")
    pane_id = coordinator_pane_for_workflow(workflow_id) or "unknown"
    episode = attention_get(key) or {}
    attempts = int(episode.get("attempts") or 0) + 1
    retry_at = time.time() + attention_retry_interval()
    attention_note(
        key, task, event_type, reason="coordinator_stalled", attempts=attempts,
        next_retry_at=retry_at,
        detail=f"waited={int(elapsed)}s pane={pane_id}",
    )
    print(
        f"[COORDINATOR STALLED] task={item['task_id']} event={event_type} "
        f"pane={pane_id} waited={int(elapsed)}s attempts={attempts} "
        f"-> attention recorded, retry_in={int(attention_retry_interval())}s"
    )
    notify_attention(
        "Herdr Factory · 总指挥停滞", task,
        f"事件 {human_reason(event_type)} 等待总指挥超过 {int(elapsed)}s（pane={pane_id}）。"
        "已记录 attention 并将慢速重试，请检查总指挥状态与集成健康。",
        "coordinator_stalled",
    )


def wait_for_decision(
    task_id, timeout=None, *, get_task, attention_get, attention_note,
    coordinator_decision_timeout, attention_retry_interval,
    clock=time.time, sleep=time.sleep,
):
    if timeout is None:
        timeout = coordinator_decision_timeout()
    deadline = clock() + timeout
    while clock() < deadline:
        task = get_task(task_id)
        if not task:
            print(f"[DECISION ERROR] task={task_id} missing")
            return None
        status = task.get("status")
        if status != "agent_done":
            print(f"[DECISION] task={task_id} status={status}")
            return status
        sleep(0.5)

    task = get_task(task_id)
    key = f"{task_id}:done"
    episode = attention_get(key) or {}
    attempts = int(episode.get("attempts") or 0) + 1
    if task:
        attention_note(
            key, task, "done", reason="decision_timeout", attempts=attempts,
            next_retry_at=clock() + attention_retry_interval(),
            detail=f"waited={int(timeout)}s still=agent_done",
        )
    print(
        f"[DECISION TIMEOUT] task={task_id} still=agent_done attempts={attempts} "
        f"-> attention recorded, retry_in={int(attention_retry_interval())}s"
    )
    return "agent_done"


def retry_decision(
    task_id, *, get_task, coordinator_pane_for_workflow,
    coordinator_discipline, run_prompt, wait_for_decision,
):
    task = get_task(task_id)
    if not task:
        print(f"[RETRY ERROR] task={task_id} missing")
        return None
    if task.get("status") != "agent_done":
        return task.get("status")

    message = f"""
HERDR_CONTROLLER_RETRY_EVENT

workflow_id: {task.get('workflow_id', 'unknown')}
task_id: {task_id}
stage: {task.get('stage', 'unknown')}
pane_id: {task.get('pane_id', 'unknown')}
agent: {task.get('agent', 'unknown')}

这是一次自动重试。

该 Task 仍停留在 agent_done，
说明上一次验收通知没有完成状态落盘。

请立即只处理这个已有 Task，不要创建新 Task：

1. 读取 Task Registry。
2. 读取 Agent 最终输出。
3. 执行：
   ~/HAFlow/bin/herdr-task verify-baseline {task_id}
4. 根据任务目标和验收标准完成正式验收。
5. 必须将 Task 状态更新为以下之一：
   - completed（门禁阶段必须带 --verdict pass|blocked，blocked 另附 --note）
   - rework
   - failed
6. 不要只输出文字报告而不更新 Task Registry。

{coordinator_discipline}
""".strip()
    print(f"[COORDINATOR RETRY] task={task_id}")
    result = run_prompt(
        [
            "herdr", "agent", "prompt",
            coordinator_pane_for_workflow(task.get("workflow_id")),
            message, "--wait", "--timeout", "120000",
        ],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        print(
            f"[COORDINATOR RETRY ERROR] task={task_id}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return wait_for_decision(task_id, timeout=30)
