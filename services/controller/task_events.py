"""Handle normal Task notifications, stale episodes, decisions and queue cleanup.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

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
        if event_type == "done" and (
            ports.try_auto_accept(task_id) or ports.try_auto_verdict(task_id)
        ):
            ports.attention_clear(key)
            ports.finalize_completed_task(task_id)
            return

        if event_type == "done":
            try:
                _defer_task = ports.get_task(task_id)
                if _defer_task is not None and _defer_task.get("status") == "agent_done":
                    _defer_reason = ports.verdict_defer_reason(_defer_task) or "accept_race_or_unknown"
                    print(f"[VERDICT DEFERRED] task={task_id} reason={_defer_reason}")
            except Exception:
                pass

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


DEPENDENCIES = {'_handle_task_event': ('attention_clear', 'attention_get', 'attention_note', 'blocked_event_type',
                        'blocker_queue_episode', 'build_coordinator_message',
                        'coordinator_pane_for_workflow', 'coordinator_status',
                        'enqueue_coordinator_event', 'finalize_completed_task', 'get_task',
                        'handle_coordinator_delivery_stall', 'liveness', 'lock', 'queued_events',
                        'retry_coordinator_decision', 'subprocess', 'time', 'try_auto_accept',
                        'try_auto_verdict', 'verdict_defer_reason',
                        'wait_for_coordinator_decision')}

__all__ = tuple(DEPENDENCIES)
