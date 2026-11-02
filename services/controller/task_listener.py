"""Consume task socket events, reconcile transitions and maintain subscriptions.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def handle_event(ports, task_id, agent_status):
    task = ports.get_task(task_id)

    if not task:
        return

    current_status = task.get("status")

    print(
        f"[TASK] "
        f"id={task_id} "
        f"workflow={task.get('workflow_id')} "
        f"stage={task.get('stage', 'unknown')} "
        f"pane={task.get('pane_id', 'unknown')} "
        f"agent={task.get('agent', 'unknown')} "
        f"status={agent_status} "
        f"task_status={current_status}"
    )

    # 终态绝不能被 Agent 普通事件覆盖
    if current_status in (
        "completed",
        "failed"
    ):
        return

    if (
        current_status == "blocked"
        and ports.blocked_event_type(task) == "inner_loop_exhausted"
        and agent_status in {"working", "idle", "done"}
    ):
        # Runtime liveness is not the coordinator's persisted decision.
        return

    if agent_status == "working":
        if current_status in (
            "dispatched",
            "blocked",
            "rework"
        ):
            ports._set_observed_status(task, "working", "recovery_working")
            # Collaboration accelerator: a working target ACKs its handoff.
            ports.maybe_ack_on_working(task_id)

    elif agent_status in {"idle", "done"}:
        if current_status in {"dispatched", "working", "rework"}:
            has_done_marker, screen = ports._completion_marker_snapshot(task)
            if not has_done_marker:
                # A gate verdict is an explicit machine recovery source for
                # prose-only gate outputs, but strictly requires current-execution
                # evidence (_gate_verdict_is_fresh or file-backed verdict) to prevent
                # stale terminal screen text from falsely completing unexecuted tasks.
                if current_status in {"dispatched", "working"}:
                    has_evidence = False
                    if hasattr(ports, "_gate_verdict_is_fresh"):
                        if not ports._gate_verdict_is_fresh(task):
                            if hasattr(ports, "sync_verdict_from_business_acceptance"):
                                ports.sync_verdict_from_business_acceptance(task)
                        has_evidence = ports._gate_verdict_is_fresh(task)
                    if not has_evidence and hasattr(ports, "read_gate_verdict"):
                        try:
                            v_res = ports.read_gate_verdict(task)
                            if isinstance(v_res, (tuple, list)) and len(v_res) >= 3:
                                if v_res[0] in ("pass", "blocked") and v_res[2] in ("file", "file+screen"):
                                    has_evidence = True
                        except Exception:
                            pass
                    if has_evidence and ports.gate_verdict_ready(task):
                        if ports._set_observed_status(task, "agent_done", "gate_verdict_recovery"):
                            ports.emit_done_if_allowed(ports.get_task(task_id), report_text=screen)
                        return
                # receipt-v1 tasks must strictly go through completion receipt validation
                # (consume_completion_receipt); only the gate-verdict recovery above is carved out.
                if task.get("completion_protocol") == "receipt-v1":
                    return
                # Rework recovery is allowed only after the existing
                # verification/retry evidence gates have passed.
                if (
                    agent_status == "idle"
                    and current_status == "rework"
                    and ports.check_task_deliverables_ready(task)
                    and ports._verification_execution_complete_for_rework(task)
                    and ports._retry_execution_complete_for_rework(task)
                ):
                    if ports._set_observed_status(task, "agent_done", "rework_recovery"):
                        ports.emit_done_if_allowed(ports.get_task(task_id), report_text=screen)
                    return
            if task.get("completion_protocol") == "receipt-v1":
                return
            # Both idle and done are settled runtime states; the durable
            # marker, sampling, elapsed-time and CAS gates still arbitrate.
            ports._record_completion_sample(
                task,
                has_done_marker,
                agent_status="idle" if agent_status == "idle" else "done",
            )
        return

    elif agent_status == "blocked":
        if current_status in (
            "working",
            "dispatched",
            "rework"
        ):
            has_done_marker, screen = ports._completion_marker_snapshot(task)
            from herdr.deep_preflight import classify_text, strip_known_status_chrome
            if screen and classify_text(strip_known_status_chrome(task.get("agent") or "", screen)) == "TOKEN_EXHAUSTED":
                agent = task.get("agent")
                workflow_id = task.get("workflow_id")
                if agent and workflow_id and hasattr(ports, "_get_store"):
                    try:
                        workflow = ports._get_store().get_workflow(workflow_id) or {}
                        from herdr.workspace_trust import merge_trust_failure_unhealthy
                        unhealthy, healthy = merge_trust_failure_unhealthy(workflow, agent, status="TOKEN_EXHAUSTED")
                        ports._get_store().update_workflow_metadata(workflow_id, {
                            "healthy_agents": healthy, "unhealthy_agents": unhealthy,
                        })
                        from herdr.state_store import sync_workflows_projection
                        sync_workflows_projection(store=ports._get_store())
                    except Exception:
                        pass
                ports._set_observed_status(task, "failed", "token_exhausted")
                return

            if ports._set_observed_status(task, "blocked", "agent_status_blocked"):
                task = ports.get_task(task_id)

                ports.enqueue_coordinator_event(
                    task,
                    ports.blocked_event_type(task)
                )


# Per-task Herdr subscriptions
def listen_task(ports, task_id):
    task = ports.get_task(task_id)

    if not task:
        return

    pane_id = task["pane_id"]

    sock = ports.socket.socket(
        ports.socket.AF_UNIX,
        ports.socket.SOCK_STREAM
    )

    try:
        sock.connect(ports.SOCKET_PATH)

        with ports.lock:
            ports.task_sockets[task_id] = sock

        request = {
            "id": f"task-{task_id}",
            "method": "events.subscribe",
            "params": {
                "subscriptions": [
                    {
                        "type":
                        "pane.agent_status_changed",
                        "pane_id": pane_id
                    }
                ]
            }
        }

        sock.sendall(
            (ports.json.dumps(request) + "\n").encode()
        )

        file = sock.makefile("r")

        first = file.readline()

        if first:
            # 订阅响应可能是错误(如目标 pane 已不存在),必须显式失败,
            # 交由 registry_watcher 的退避逻辑处理,禁止伪装成已订阅。
            try:
                payload = ports.json.loads(first)
            except Exception:
                payload = None

            if isinstance(payload, dict) and payload.get("error"):
                raise RuntimeError(
                    str(payload["error"].get("message") or payload["error"])
                )

            print(
                f"[SUBSCRIBED] "
                f"task={task_id} "
                f"pane={pane_id}"
            )

            # Controller 重启后立即核对
            # Registry 与 Agent 当前真实状态。
            ports.reconcile_task_state(
                task_id
            )

        for line in file:
            if not line:
                break

            event = ports.json.loads(line)

            if (
                event.get("event")
                != "pane.agent_status_changed"
            ):
                continue

            status = (
                event["data"]
                .get(
                    "agent_status",
                    "unknown"
                )
            )

            ports.handle_event(
                task_id,
                status
            )

    except Exception as e:
        print(
            f"[LISTENER ERROR] "
            f"task={task_id}: {e}"
        )

    finally:
        with ports.lock:
            ports.task_sockets.pop(
                task_id,
                None
            )
            ports.listeners.pop(
                task_id,
                None
            )

        try:
            sock.close()
        except Exception:
            pass

def start_task_listener(ports, task_id):
    thread = ports.threading.Thread(
        target=ports.listen_task,
        args=(task_id,),
        daemon=True
    )

    with ports.lock:
        ports.listeners[task_id] = thread

    thread.start()

def stop_task_listener(ports, task_id):
    with ports.lock:
        sock = ports.task_sockets.pop(
            task_id,
            None
        )

    if sock:
        try:
            sock.shutdown(
                ports.socket.SHUT_RDWR
            )
        except Exception:
            pass

        try:
            sock.close()
        except Exception:
            pass

        print(
            f"[UNSUBSCRIBED] "
            f"task={task_id}"
        )


DEPENDENCIES = {'handle_event': ('_completion_marker_snapshot', '_gate_verdict_is_fresh',
                  '_get_store', '_record_completion_sample',
                  '_retry_execution_complete_for_rework', '_set_observed_status',
                  '_verification_execution_complete_for_rework', 'blocked_event_type',
                  'check_task_deliverables_ready', 'emit_done_if_allowed',
                  'enqueue_coordinator_event', 'gate_verdict_ready', 'get_task',
                  'maybe_ack_on_working', 'read_gate_verdict', 'sync_verdict_from_business_acceptance'),
 'listen_task': ('SOCKET_PATH', 'get_task', 'handle_event', 'json', 'listeners', 'lock',
                 'reconcile_task_state', 'socket', 'task_sockets'),
 'start_task_listener': ('listen_task', 'listeners', 'lock', 'threading'),
 'stop_task_listener': ('lock', 'socket', 'task_sockets')}

__all__ = tuple(DEPENDENCIES)
