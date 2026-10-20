"""Submit supervisor and observer checkpoints without blocking normal task flow.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def _supervisor_log_pending(ports, task, action):
    """Record/refresh the pending-intervention ledger entry (log/notify once)."""
    key = f"supervisor:{task.get('task_id')}"
    reason = f"action {action} pending; done flow blocked until resolved"
    episode = ports.attention_get(key) or {}
    if episode.get("reason") == reason:
        return
    print(f"[SUPERVISOR PENDING] task={task.get('task_id')} {reason}")
    ports.attention_note(key, task, "supervisor_pending", reason)
    ports._supervisor_notify(task, action, reason)

def _observer_terminal_checkpoint(ports, task, now=None, completion_event=None):
    """Terminal Trajectory Observer checkpoint for agent_done.

    Fail-safe by contract: any failure only logs and returns False, so the
    existing done flow is never blocked. The observation itself runs on the
    observer's daemon worker (async, non-blocking).
    """
    if ports.observer_harness is None or not task:
        if completion_event is not None:
            completion_event.set()
        return False
    try:
        kwargs = {"store": ports._get_store(), "now": now}
        if completion_event is not None:
            kwargs["completion_event"] = completion_event
        try:
            submitted = ports.observer_harness.submit_terminal_observation(task, **kwargs)
        except TypeError as exc:
            # Keep older test adapters/in-process integrations fail-safe while
            # the built-in harness uses the completion barrier.
            if completion_event is None or "completion_event" not in str(exc):
                raise
            kwargs.pop("completion_event", None)
            submitted = ports.observer_harness.submit_terminal_observation(task, **kwargs)
            completion_event.set()
        if not submitted and completion_event is not None:
            completion_event.set()
        return submitted
    except Exception as exc:
        if completion_event is not None:
            completion_event.set()
        print(f"[OBSERVER TERMINAL CHECK ERROR] task={task.get('task_id')}: {exc}")
        return False

def supervisor_checkpoint(ports, task, trigger, report_text=None, test_evidence=None, evidence_id=None, now=None):
    """Semantic Supervisor 观察点:Jev/Provider 只产生信号与 Policy 结论,
    状态推进全部走既有流程;整体 fail-safe,绝不影响任务主链路。

    返回 harness 结果(None=未监督或异常)。调用方必须用
    ``supervisor_continue_flow(result)`` 决定是否继续 done:
    被 enforce 拦截的 intervention 绝不再走默认完成流程。
    """
    if ports.supervisor_harness is None or not task:
        return None
    try:
        fresh = ports.get_task(task.get("task_id")) or task
        actions = {
            # Policy actions map onto existing flows only. Durable VERIFY/RETRY
            # claims are owned by the Controller wrapper, not the Supervisor.
            "RETRY": lambda t, d: ports._execute_supervisor_intervention(t, d, ports._supervisor_retry),
            "ESCALATE": lambda t, d: ports._supervisor_attention(t, d, "supervisor_escalate"),
            "VERIFY": lambda t, d: ports._execute_supervisor_intervention(t, d, ports._supervisor_verify),
            "PAUSE": lambda t, d: ports._supervisor_attention(t, d, "supervisor_pause"),
            "REROUTE": lambda t, d: ports._supervisor_attention(t, d, "supervisor_reroute"),
        }
        if report_text is not None:
            report_reader = lambda: report_text
        else:
            report_reader = lambda: ports._supervisor_pane_report(fresh)
        result = ports.supervisor_harness.run_checkpoint(
            task=fresh,
            trigger=trigger,
            store=ports._get_store(),
            actions=actions,
            report_reader=report_reader,
            test_evidence=test_evidence,
            evidence_id=evidence_id,
            now=now,
            log=print,
        )
        if result and result.get("intercepted") and not result.get("handled"):
            action = (result.get("decision") or {}).get("action")
            ports.attention_note(
                f"supervisor:{fresh.get('task_id')}",
                fresh,
                "supervisor_unhandled",
                f"intercepted action {action} without a handler; done flow blocked",
            )
        return result
    except Exception as e:
        print(f"[SUPERVISOR SKIPPED] task={task.get('task_id')}: {type(e).__name__}: {e}")
        return None


DEPENDENCIES = {'_supervisor_log_pending': ('_supervisor_notify', 'attention_get', 'attention_note'),
 '_observer_terminal_checkpoint': ('_get_store', 'observer_harness'),
 'supervisor_checkpoint': ('_execute_supervisor_intervention', '_get_store',
                           '_supervisor_attention', '_supervisor_pane_report', '_supervisor_retry',
                           '_supervisor_verify', 'attention_note', 'get_task',
                           'supervisor_harness')}

__all__ = tuple(DEPENDENCIES)
