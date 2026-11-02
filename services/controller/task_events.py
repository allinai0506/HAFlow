"""Handle normal Task notifications, stale episodes, decisions and queue cleanup.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def _route_staged_auto_acceptance_failure(ports, key, task, result):
    from subprocess import TimeoutExpired

    from services.controller.auto_acceptance import (
        AUTO_ACCEPT_REWORK_LIMIT,
        AUTO_REWORK_REASON_PREFIX,
    )

    reason = getattr(result, "reason_code", "") or "unknown_acceptance_failure"
    detail = getattr(result, "detail", "") or "; ".join(getattr(result, "missing", ()))
    if reason in {"cas_mismatch", "task_not_agent_done"}:
        return
    if reason == "gate_node":
        reason = ports.verdict_defer_reason(task) or "unknown_acceptance_failure"
    attempts = ports.auto_rework_attempts(task)
    limit = AUTO_ACCEPT_REWORK_LIMIT
    reworkable = {
        "requirements_missing_sections", "plan_missing_sections",
        "plan_challenger_missing_sections", "implementation_no_controlled_changes",
    }
    identity = (task.get("workflow_id") and (task.get("node") or task.get("stage"))
                and isinstance(task.get("version"), int) and task.get("run_id")
                and task.get("execution_id"))
    if not identity:
        reason = "task_identity_incomplete"
    if reason in reworkable and attempts < limit:
        attempt = attempts + 1
        version, run_id, execution_id = task["version"], task["run_id"], task["execution_id"]
        prompt = (f"自动验收失败：{reason}。在当前 Task/Pane 就地修复交付物并重新自测、提交完成声明。"
                  "不得新建 Task/Pane，不得修改裁决或绕过门禁。\n"
                  + "缺少内容：" + ", ".join(getattr(result, "missing", ()))
                  + ("\nverify-baseline 明确没有受控变更，请完成已授权代码实现。"
                     if reason == "implementation_no_controlled_changes" else ""))
        cmd = [ports.TASK_MANAGER, "rework", task["task_id"],
               "--reason", f"{AUTO_REWORK_REASON_PREFIX}{reason}:attempt:{attempt}",
               "--prompt", prompt, "--request-id",
               f"auto-accept-rework:{task['task_id']}:{reason}:{attempt}:v{version}:{run_id}:{execution_id}",
               "--expected-status", "agent_done", "--expected-version", str(version),
               "--expected-run-id", run_id, "--expected-execution-id", execution_id]
        try:
            dispatched = ports.subprocess.run(cmd, text=True, capture_output=True, timeout=120)
            if dispatched.returncode == 0:
                ports.attention_clear(key)
                return
            # Only stable exit codes are retained; native output may contain secrets.
            detail = f"rework CLI rejected dispatch: exit={dispatched.returncode}"
        except (OSError, TimeoutExpired) as exc:
            detail = f"rework CLI {type(exc).__name__}; delivery unknown"
        current = ports.get_task(task["task_id"])
        if not current:
            return
        same_identity = all(current.get(k) == task.get(k) for k in ("run_id", "execution_id"))
        if current.get("status") == "rework" and current.get("rework_delivery") == "pending" and same_identity:
            task = current
            attempts = ports.auto_rework_attempts(task)
            reason = "rework_delivery_pending"
        elif not same_identity:
            if current.get("version") != version or current.get("status") != "agent_done":
                return
            task, reason = current, "task_identity_incomplete"
        elif current.get("status") != "agent_done" or current.get("version") != version:
            return
        else:
            reason = "rework_dispatch_failed"
    escalated = True
    next_retry_at = None
    if reason in {"verification_unavailable", "gate_signal_missing_or_conflict"}:
        episode = ports.attention_get(key) or {}
        attempts = int(episode.get("attempts") or 0) + 1
        limit = 3
        escalated = attempts >= limit
        if not escalated:
            next_retry_at = ports.time.time() + ports.liveness.backoff_delay(attempts)
    ports.attention_note(key, task, "done", reason=f"auto_acceptance_rejected: {reason}",
                         reason_code=reason, attempts=attempts, limit=limit,
                         escalated=escalated, next_retry_at=next_retry_at, detail=detail[:400])
    if escalated:
        try:
            ports.notify_attention("自动验收需要干预", task, f"{reason}: {detail[:400]}", reason)
        except Exception as exc:
            print(f"[ATTENTION NOTIFY ERROR] {type(exc).__name__}")


def _handle_task_event(ports, item):
    """Process one Task event, including stale checks and queue cleanup."""

    # ==============================================
    # Normal Task Event
    # ==============================================

    task_id = item["task_id"]
    event_type = item["event_type"]
    key = item["key"]

    if event_type == "attention":
        # attention 事件针对 interrupted/paused 等需要裁决的中间态,
        # 只要任务仍停留在待裁决状态就有效。
        expected_status = None
    elif event_type in {"blocked", "inner_loop_exhausted"}:
        expected_status = "blocked"
    else:
        expected_status = "agent_done"

    try:
        last_busy_log = 0
        wait_started = ports.time.time()

        # 规则化验收快路径:非门禁节点铁证齐备直接 completed,
        # 门禁节点有唯一结论标记则直接落 verdict;两者都不命中才回落总指挥。
        _auto_result = None
        _accept_snapshot = None
        if event_type == "done":
            _accept_snapshot = ports.get_task(task_id)
            if _accept_snapshot and ports.attention_escalated(key, _accept_snapshot):
                return
            _auto_result = ports.try_auto_accept(task_id)
        if event_type == "done" and (_auto_result or ports.try_auto_verdict(task_id)):
            ports.attention_clear(key)
            ports.finalize_completed_task(task_id)
            return

        if event_type == "done":
            try:
                _defer_task = ports.get_task(task_id)
                if _defer_task is not None and _defer_task.get("status") == "agent_done":
                    _defer_reason = getattr(_auto_result, "reason_code", "") or "accept_race_or_unknown"
                    if _defer_reason == "gate_node":
                        _defer_reason = ports.verdict_defer_reason(_defer_task) or "accept_race_or_unknown"
                    print(f"[VERDICT DEFERRED] task={task_id} reason={_defer_reason}")
            except Exception:
                pass

        _task_pre = ports.get_task(task_id)
        if _task_pre:
            _wf_id = _task_pre.get("workflow_id")
            _wf_cfg = ports.workflow_config_for(_wf_id) or {} if _wf_id else {}
            from herdr.stage_skills import is_staged_dev_flow_workflow
            if is_staged_dev_flow_workflow(_wf_cfg):
                # 与下方主循环相同的失效守卫必须先于 attention 落笔：
                # 已推进任务的排队事件不得写成假干预 (PR #356 评审遗留)。
                _staged_status = str(_task_pre.get("status") or "")
                if event_type == "attention" and _staged_status not in ("interrupted", "paused"):
                    print(
                        f"[QUEUE STALE] "
                        f"task={task_id} "
                        f"attention resolved actual={_staged_status}"
                    )
                    ports.attention_clear(key)
                    return
                if expected_status is not None and _staged_status != expected_status:
                    print(
                        f"[QUEUE STALE] "
                        f"task={task_id} "
                        f"expected={expected_status} "
                        f"actual={_staged_status}"
                    )
                    return
                if event_type == "done":
                    if ports.attention_escalated(key, _task_pre):
                        return
                    if _accept_snapshot and any(_accept_snapshot.get(k) != _task_pre.get(k)
                                                for k in ("status", "version", "run_id", "execution_id")):
                        return
                    _route_staged_auto_acceptance_failure(ports, key, _task_pre, _auto_result)
                    return
                _r = locals().get("_defer_reason", event_type)
                ports.attention_note(
                    key,
                    _task_pre,
                    event_type,
                    reason=f"auto_acceptance_rejected: {_r}" if event_type == "done" else str(_r),
                    detail=f"Task {task_id} {event_type} in staged workflow; coordinator bypass active",
                )
                print(f"[STAGED TASK EVENT BYPASS] task={task_id} event={event_type} reason={_r}")
                return

        while True:
            task = ports.get_task(task_id)

            if not task:
                print(
                    f"[QUEUE DROP] "
                    f"task={task_id} missing"
                )
                break

            current_task_status = task.get("status")

            if event_type == "attention" and current_task_status not in (
                "interrupted",
                "paused",
            ):
                print(
                    f"[QUEUE STALE] "
                    f"task={task_id} "
                    f"attention resolved actual={current_task_status}"
                )
                ports.attention_clear(key)
                break

            # 事件在等待期间已经失效
            if expected_status is not None and current_task_status != expected_status:
                print(
                    f"[QUEUE STALE] "
                    f"task={task_id} "
                    f"expected={expected_status} "
                    f"actual={current_task_status}"
                )
                break

            if event_type in {"blocked", "inner_loop_exhausted"} and (
                item.get("blocker_episode") != ports.blocker_queue_episode(task)
                or ports.blocked_event_type(task) != event_type
            ):
                print(f"[QUEUE STALE] task={task_id} blocker episode changed")
                # Legacy metadata updates cannot prove episode continuity.
                # Drop old authority, but retain reachability of the current
                # persisted blocker through the same episode-aware queue.
                ports.enqueue_coordinator_event(task, ports.blocked_event_type(task))
                break

            status = ports.coordinator_status(task.get("workflow_id"))

            if status in ("idle", "done"):
                message = ports.build_coordinator_message(
                    task,
                    event_type
                )

                print(
                    f"[COORDINATOR READY] "
                    f"task={task_id} "
                    f"event={event_type}"
                )

                if event_type in {"blocked", "inner_loop_exhausted"}:
                    current = ports.get_task(task_id)
                    if (
                        not current or current.get("status") != "blocked"
                        or item.get("blocker_episode") != ports.blocker_queue_episode(current)
                        or ports.blocked_event_type(current) != event_type
                    ):
                        print(f"[QUEUE STALE] task={task_id} changed before prompt")
                        if current and current.get("status") == "blocked":
                            ports.enqueue_coordinator_event(current, ports.blocked_event_type(current))
                        break

                result = ports.subprocess.run(
                    [
                        "herdr",
                        "agent",
                        "prompt",
                        ports.coordinator_pane_for_workflow(task.get("workflow_id")),
                        message,
                        "--wait",
                        "--timeout",
                        "600000"
                    ],
                    text=True,
                    capture_output=True
                )

                if result.returncode == 0:
                    ports.attention_clear(key)

                    print(
                        f"[COORDINATOR NOTIFIED] "
                        f"task={task_id} "
                        f"event={event_type}"
                    )

                    # done 事件经过总指挥正式验收后，
                    # 根据 integration_mode 自动集成并清理。
                    if event_type == "done":
                        decision = ports.wait_for_coordinator_decision(
                            task_id
                        )

                        # 第一次没有形成决策时，只自动重试一次。
                        if decision == "agent_done":
                            decision = ports.retry_coordinator_decision(
                                task_id
                            )

                        if decision == "completed":
                            ports.finalize_completed_task(
                                task_id
                            )

                        elif decision == "rework":
                            print(
                                f"[FINALIZE DEFER] "
                                f"task={task_id} "
                                f"status=rework"
                            )

                        elif decision == "failed":
                            print(
                                f"[FINALIZE STOP] "
                                f"task={task_id} "
                                f"status=failed"
                            )

                        else:
                            print(
                                f"[FINALIZE WAIT] "
                                f"task={task_id} "
                                f"status={decision}"
                            )
                else:
                    # 投递失败(如 agent_prompt_stalled):记录退避节流,
                    # 由 registry watcher 在退避窗口后按需重试,杜绝 1s 级风暴。
                    episode = ports.attention_get(key) or {}
                    attempts = int(episode.get("attempts") or 0) + 1
                    delay = ports.liveness.backoff_delay(attempts)
                    ports.attention_note(
                        key,
                        task,
                        event_type,
                        reason="delivery_failed",
                        attempts=attempts,
                        next_retry_at=ports.time.time() + delay,
                        detail=(result.stderr.strip() or result.stdout.strip())[:400],
                    )

                    print(
                        "[COORDINATOR ERROR]",
                        result.stderr.strip()
                        or result.stdout.strip(),
                        f"-> retry_in={int(delay)}s attempts={attempts}"
                    )

                break

            now = ports.time.time()

            elapsed = now - wait_started
            if elapsed >= ports.liveness.coordinator_delivery_sla():
                ports.handle_coordinator_delivery_stall(
                    item, task, elapsed
                )
                break

            if now - last_busy_log >= 15:
                last_busy_log = now
                print(
                    f"[COORDINATOR BUSY] "
                    f"status={status} "
                    f"task={task_id} "
                    f"waited={int(elapsed)}s"
                )

            ports.time.sleep(2)

    finally:
        with ports.lock:
            ports.queued_events.discard(item.get("queue_key", key))


DEPENDENCIES = {'_handle_task_event': ('TASK_MANAGER', 'auto_rework_attempts', 'attention_escalated', 'notify_attention', 'attention_clear', 'attention_get', 'attention_note', 'blocked_event_type',
                        'blocker_queue_episode', 'build_coordinator_message',
                        'coordinator_pane_for_workflow', 'coordinator_status',
                        'enqueue_coordinator_event', 'finalize_completed_task', 'get_task',
                        'handle_coordinator_delivery_stall', 'liveness', 'lock', 'queued_events',
                        'retry_coordinator_decision', 'subprocess', 'time', 'try_auto_accept',
                        'try_auto_verdict', 'verdict_defer_reason',
                        'wait_for_coordinator_decision', 'workflow_config_for')}

__all__ = tuple(DEPENDENCIES)
