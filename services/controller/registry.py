"""Sweep registered tasks, maintain listeners and schedule bounded recovery work.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def check_all_workflows_stage_advance(ports):

    # Startup-orphan reclaim runs before the sweep's waiting branches: a record
    # that never became ready must be converged before anything waits on it.
    try:
        ports.scan_startup_orphans()
    except Exception as exc:
        print(f"[STARTUP WATCHDOG ERROR] {type(exc).__name__}: {exc}")

    now = ports.time.time()
    workflow_ids = ports.active_registered_workflows()
    if now - ports._continuation_scan_at >= 30:
        ports._continuation_scan_at = now
        ports.schedule_workflow_continuations(workflow_ids, now)
    for wf in workflow_ids:
        try:
            ports.redeliver_pending_fix_loop(wf)
        except Exception as e:
            print(f"[REDELIVER ERROR] workflow={wf}: {e}")
        try:
            ports.check_workflow_stage_advance(wf)
        except Exception as e:
            print(f"[ADVANCE CHECK ERROR] workflow={wf}: {e}")
    from services.controller.cockpit_automation import schedule_automatic_controller_actions
    try:
        schedule_automatic_controller_actions(ports._get_store(), workflow_ids)
    except Exception as exc:
        print(f"[COCKPIT SCHEDULE ERROR] {type(exc).__name__}")

def sync_awake_guard(ports, active_workflows):
    """有活跃 workflow 时持有 caffeinate,避免机器休眠冻结整条流水线。

    controller 进程退出或 workflow 清零时自动释放;HERDR_AWAKE_GUARD=0 关闭。
    """


    disabled = ports.os.environ.get("HERDR_AWAKE_GUARD", "1").strip().lower() in (
        "0",
        "false",
        "off",
        "no",
    )

    want = (
        not disabled
        and bool(active_workflows)
        and ports.sys.platform == "darwin"
        and ports.shutil.which("caffeinate") is not None
    )

    if want:
        if ports._awake_guard_proc is None or ports._awake_guard_proc.poll() is not None:
            try:
                ports._awake_guard_proc = ports.subprocess.Popen(
                    ["caffeinate", "-i", "-s", "-w", str(ports.os.getpid())],
                    stdout=ports.subprocess.DEVNULL,
                    stderr=ports.subprocess.DEVNULL,
                )
                print(
                    f"[AWAKE GUARD] caffeinate started "
                    f"workflows={len(active_workflows)}"
                )
            except Exception as exc:
                print(f"[AWAKE GUARD ERROR] {exc}")
                ports._awake_guard_proc = None
        return

    if ports._awake_guard_proc is not None:
        try:
            ports._awake_guard_proc.terminate()
        except Exception:
            pass
        ports._awake_guard_proc = None
        print("[AWAKE GUARD] caffeinate released")


# Registry watcher
def registry_watcher(ports):
    active_statuses = {
        "dispatched",
        "working",
        "blocked",
        "agent_done",
        "rework",
    }
    TERMINAL_LIKE_STATUSES = (
        "completed",
        "failed",
        "superseded",
        "cleaned",
        "committed",
        "integrated",
        "cleanup_ready",
    )

    last_advance_check = 0

    while True:
        try:
            now = ports.time.time()
            if now - last_advance_check >= 2:
                last_advance_check = now
                ports.check_all_workflows_stage_advance()
                ports.sync_awake_guard(ports.active_registered_workflows())

            tasks = ports.load_tasks()
            # Sentinel records observations; Controller is the only actor that
            # promotes a confirmed completion or blocker to a task transition.
            ports.process_structured_completions(now=now)
            ports.process_all_completion_observations(now=now)
            ports.process_blocked_observations()
            ports.process_gate_verdict_reconciliations()
            tasks = ports.load_tasks()
            task_ids_now = set()
            _wf_closed_cache = {}

            def _is_wf_closed(wfid):
                if not wfid:
                    return False
                if wfid not in _wf_closed_cache:
                    _wf_closed_cache[wfid] = bool(ports.workflow_closed(wfid))
                return _wf_closed_cache[wfid]

            for task in tasks:
                task_id = task["task_id"]
                status = task.get("status")
                task_ids_now.add(task_id)

                wf_id = task.get("workflow_id")
                if status in TERMINAL_LIKE_STATUSES or _is_wf_closed(wf_id):
                    with ports.lock:
                        running = (
                            task_id
                            in ports.task_sockets
                        )

                    if running:
                        ports.stop_task_listener(
                            task_id
                        )

                    ports._listener_backoff.pop(task_id, None)
                    ports._listener_giveup_logged.discard(task_id)
                    ports.attention_clear(f"{task_id}:done")
                    ports.attention_clear(f"{task_id}:blocked")
                    ports.attention_clear(f"{task_id}:attention")

                    # 基础设施失败(投递熔断/进程崩溃)自动作废补派,
                    # 不让整个节点空等人工 relaunch。
                    if status == "failed" and not _is_wf_closed(wf_id):
                        try:
                            ports.recover_router_isolation_tasks(
                                task.get("workflow_id"), tasks
                            )
                            ports.recover_infra_failed_tasks(
                                task.get("workflow_id"), tasks
                            )
                        except (OSError, RuntimeError, ValueError, AttributeError) as exc:
                            print(f"[AUTO RECOVER ERROR] {exc}")

                    # completed/committed + git 的终化重试必须在这里驱动:
                    # 该分支随即 continue,走不到后方的重试块。
                    if status in ("completed", "committed"):
                        try:
                            ports._check_finalize_retry(task, status, now)
                        except (OSError, RuntimeError, ValueError, KeyError,
                                ports.subprocess.SubprocessError) as exc:
                            print(f"[FINALIZE RETRY ERROR] {exc}")

                    continue

                # ---- listener 订阅:指数退避 + 封顶(僵尸 pane 护栏) ----
                if status in active_statuses:
                    with ports.lock:
                        already = (
                            task_id
                            in ports.listeners
                        )

                    if not already:
                        signature = (status, task.get("updated_at"))
                        slot = ports._listener_backoff.get(task_id)
                        if not slot or slot.get("signature") != signature:
                            # 任务真实状态变化即重置退避,避免误封活工位。
                            slot = {
                                "attempts": 0,
                                "next_allowed_at": 0.0,
                                "signature": signature,
                            }
                            ports._listener_backoff[task_id] = slot

                        if now >= slot.get("next_allowed_at", 0.0):
                            attempts = slot.get("attempts", 0) + 1
                            slot["attempts"] = attempts
                            if attempts >= ports.liveness.subscribe_max_attempts():
                                slot["next_allowed_at"] = (
                                    now + ports.liveness.subscribe_backoff_cap()
                                )
                                if task_id not in ports._listener_giveup_logged:
                                    ports._listener_giveup_logged.add(task_id)
                                    print(
                                        f"[LISTENER GIVEUP] "
                                        f"task={task_id} "
                                        f"pane={task.get('pane_id')} "
                                        f"attempts={attempts} -> "
                                        f"retry every {int(ports.liveness.subscribe_backoff_cap())}s"
                                    )
                            else:
                                slot["next_allowed_at"] = (
                                    now + ports.liveness.backoff_delay(attempts)
                                )
                            ports.start_task_listener(
                                task_id
                            )

                # ---- tests_completed 连续评估检查 (仅对活跃工作中的任务) ----
                # dispatched 排除：Agent 尚未开始工作，不会有真实测试结果；
                # rework 保留：Agent 仍在 inner loop 迭代，和 working 等价对待。
                if status in ("working", "rework"):
                    try:
                        ports.check_task_tests_completed(task, store=ports._get_store(), now=now)
                    except Exception as exc:
                        print(f"[TESTS_COMPLETED CHECK ERROR] task={task_id}: {exc}")

                # ---- Trajectory Observer: 旁路观察,非阻塞投递 ----
                # 观察线程与主轮询物理隔离;超时/模型失败/内部异常均不影响
                # Task/Workflow/Runtime 与事件推进。
                if (
                    status in ("working", "rework", "blocked")
                    and ports.observer_harness is not None
                ):
                    try:
                        ports.observer_harness.submit_observation(
                            task, store=ports._get_store(), now=now
                        )
                    except Exception as exc:
                        print(f"[OBSERVER CHECK ERROR] task={task_id}: {exc}")

                # ---- done 事件投递:受 attention episode 节流 + 监督网关把关 ----
                # terminal Observer checkpoint 已挂在统一 Done Gateway
                # (emit_done_if_allowed) 内,覆盖 listener/recovery/redelivery 全部路径。
                if status == "agent_done":
                    ports.redeliver_done_event(task, now=now)

                # ---- blocked SLA: Controller owns the one re-push and escalation ----
                if status == "blocked":
                    ports.schedule_blocked_sla_task(task, now=now)
                else:
                    ports.attention_clear(f"{task_id}:blocked")
                    ports.attention_clear(ports._blocked_sla_key(task_id))

                # ---- interrupted / paused 死区:超时未裁决即升级给总指挥 ----
                if status in ("interrupted", "paused"):
                    key = f"{task_id}:attention"
                    updated = float(task.get("updated_at") or 0)
                    with ports.lock:
                        already_queued = key in ports.queued_events
                    if (
                        updated
                        and now - updated >= ports.liveness.attention_grace()
                        and not already_queued
                        and not ports.attention_blocks_retry(key, now)
                    ):
                        print(
                            f"[REGISTRY WATCHER] "
                            f"task={task_id} "
                            f"status={status} -> attention event to coordinator"
                        )
                        ports.enqueue_coordinator_event(task, "attention")
                        if not ports.attention_get(key):
                            ports.attention_note(
                                key,
                                task,
                                "attention",
                                reason="status_requires_attention",
                                attempts=1,
                            )
                        ports.attention_throttle(key, now=now)
                else:
                    ports.attention_clear(f"{task_id}:attention")

                # ---- committed 滞留 / completed 但 commit 门禁瞬时失败(flaky):
                #      integrate 失败或 commit gate 抖动时按退避自动重试,
                #      达到上限后停止并升级人工(避免无限重试风暴)。
                #      completed 分支在上方的 terminal 分支内已驱动(随即
                #      continue,走不到这里);这里覆盖 committed 等终化中状态。
                ports._check_finalize_retry(task, status, now)

                if status == "rework" and not _is_wf_closed(task.get("workflow_id")):
                    pane_id = task.get("pane_id")
                    if pane_id:
                        runtime = ports.get_agent_runtime_status(pane_id)
                        if (
                            runtime in ("idle", "done")
                            and ports.check_task_deliverables_ready(task)
                            and ports._verification_execution_complete_for_rework(task)
                            and ports._retry_execution_complete_for_rework(task)
                        ):
                            has_done_marker, _screen = ports._completion_marker_snapshot(task)
                            ports._record_completion_sample(
                                task,
                                has_done_marker,
                                agent_status="idle" if runtime == "idle" else "done",
                            )

            for stale_id in list(ports._listener_backoff.keys()):
                if stale_id not in task_ids_now:
                    ports._listener_backoff.pop(stale_id, None)
                    ports._listener_giveup_logged.discard(stale_id)

        except Exception as e:
            print(
                f"[REGISTRY ERROR] {e}"
            )

        ports.time.sleep(1)


DEPENDENCIES = {'check_all_workflows_stage_advance': ('_continuation_scan_at', '_get_store', 'active_registered_workflows',
                                       'check_workflow_stage_advance', 'redeliver_pending_fix_loop',
                                       'scan_startup_orphans',
                                       'schedule_workflow_continuations', 'time'),
 'sync_awake_guard': ('_awake_guard_proc', 'os', 'shutil', 'subprocess', 'sys'),
 'registry_watcher': ('_blocked_sla_key', '_check_finalize_retry', '_completion_marker_snapshot',
                      '_get_store', '_listener_backoff', '_listener_giveup_logged',
                      '_record_completion_sample', '_retry_execution_complete_for_rework',
                      '_verification_execution_complete_for_rework', 'active_registered_workflows',
                      'attention_blocks_retry', 'attention_clear', 'attention_get',
                      'attention_note', 'attention_throttle', 'check_all_workflows_stage_advance',
                      'check_task_deliverables_ready', 'check_task_tests_completed',
                      'enqueue_coordinator_event', 'get_agent_runtime_status', 'listeners',
                      'liveness', 'load_tasks', 'lock', 'observer_harness',
                      'process_all_completion_observations', 'process_blocked_observations',
                      'process_gate_verdict_reconciliations', 'process_structured_completions',
                      'queued_events', 'recover_infra_failed_tasks',
                      'recover_router_isolation_tasks', 'redeliver_done_event',
                      'schedule_blocked_sla_task', 'start_task_listener', 'stop_task_listener',
                      'subprocess', 'sync_awake_guard', 'task_sockets', 'time', 'workflow_closed')}

__all__ = tuple(DEPENDENCIES)
