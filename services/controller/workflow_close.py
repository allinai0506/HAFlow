"""Close delivered workflows after pending Git finalization and escalation checks.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def git_finalize_pending_tasks(ports, workflow_id):
    """completed/committed 且 git 集成的任务仍在 commit/integrate 终化管线中。

    自动 close 若抢先推进到 cleaned,在跑的 `herdr-task commit` 子进程随后
    撞 'cleaned -> committed' 非法转移,交付分支落不进集成链路
    (2026-09-17 wf-nexusarchive-0917-01 wrapup 实测)。终化由其有界重试
    自会收敛(耗尽则升级总指挥);收敛后下一轮 sweep 再 close。
    """
    try:
        tasks = ports.load_tasks()
    except Exception:
        return []

    return [
        t.get("task_id")
        for t in (tasks or [])
        if t.get("workflow_id") == workflow_id
        and t.get("status") in ("completed", "committed")
        and (t.get("integration_mode") or "none") == "git"
        and not t.get("finalize_escalated")
    ]

def git_escalated_tasks(ports, workflow_id):
    """H-1: machine-escalated git tasks still holding undelivered commits.

    Escalation is observability + retry-stop, never a closability proof.
    Auto-close must defer on these; manual close requires explicit
    ``--accept-escalated``/``--force``/``--abandon`` or ``supersede``.
    """
    try:
        tasks = ports.load_tasks()
    except (OSError, ValueError, RuntimeError, AttributeError,
            ports.subprocess.SubprocessError):
        return []
    return [
        t.get("task_id")
        for t in (tasks or [])
        if t.get("workflow_id") == workflow_id
        and t.get("status") in ("completed", "committed")
        and (t.get("integration_mode") or "none") == "git"
        and t.get("finalize_escalated")
    ]

def close_retry_interval(ports, attempts):
    """Bounded exponential backoff between auto-close attempts (60s..600s)."""
    try:
        attempts = max(0, int(attempts))
    except (TypeError, ValueError):
        attempts = 0
    return min(60 * (2 ** attempts), 600)

def _note_close_backoff(ports, workflow_id, close_key):
    """Record a failed auto-close attempt and throttle the next retry."""
    try:
        episode = ports.attention_get(close_key) or {}
        attempts = int(episode.get("attempts") or 0) + 1
    except (TypeError, ValueError):
        attempts = 1
    interval = ports.close_retry_interval(attempts - 1)
    ports.attention_note(
        close_key,
        {"task_id": f"{workflow_id}:close", "workflow_id": workflow_id},
        "auto_close_failed",
        "auto_close_failed",
        attempts=attempts,
    )
    ports.attention_throttle(close_key, interval=interval)
    print(
        f"[CLOSE RETRY BACKOFF] "
        f"workflow={workflow_id} "
        f"attempt={attempts} next_in={interval}s"
    )

def maybe_close_completed_workflow(ports, workflow_id):
    """Workflow 全部节点完成后,自动执行物理收尾(关 pane/删 clone/归档)。

    close-workflow 自带幂等与终态闸门;这里只负责防重入派发。
    """
    if not workflow_id or workflow_id in ports._workflow_close_inflight:
        return

    close_key = f"{workflow_id}:auto-close"
    if ports.attention_blocks_retry(close_key):
        return

    entry = ports._workflow_entry(workflow_id)

    # 夹具/临时 workflow 严禁物理收尾(会去操作不存在的 clone/pane)。
    if ports.liveness.workflow_is_foreign(entry):
        return

    if entry.get("status") == "completed":
        return
    # Fix-loop reopen 闩:重开后的 workflow 在首个任务进入 ACTIVE
    # 之前,旧任务仍全为完成系,必须挡住 sweep 的自消除 close。
    if entry.get("suppress_auto_close"):
        return

    # git 终化在途:必须等 commit/integrate 收敛,close 不得抢先物理收尾。
    pending_git = ports.git_finalize_pending_tasks(workflow_id)
    if pending_git:
        if workflow_id not in ports._close_deferred_logged:
            ports._close_deferred_logged.add(workflow_id)
            print(
                f"[CLOSE DEFERRED] workflow={workflow_id} "
                f"waiting git finalize: {','.join(pending_git)}"
            )
        return

    # H-1: auto-close never carries human confirmation, so escalated git
    # tasks (retry-exhausted / refused / main-dirty) must also defer.
    # Silent delivered with a never-integrated committed task is the exact
    # failure this blocks.
    escalated_git = ports.git_escalated_tasks(workflow_id)
    if escalated_git:
        if workflow_id not in ports._close_deferred_logged:
            ports._close_deferred_logged.add(workflow_id)
            print(
                f"[CLOSE DEFERRED] workflow={workflow_id} "
                f"waiting human confirm for escalated: "
                f"{','.join(escalated_git)}"
            )
        return

    ports._workflow_close_inflight.add(workflow_id)

    def _run():
        try:
            try:
                result = ports.subprocess.run(
                    [ports.TASK_MANAGER, "close-workflow", workflow_id],
                    text=True,
                    capture_output=True,
                    timeout=900,
                )
            except Exception as exc:
                print(
                    f"[WORKFLOW CLOSE ERROR] "
                    f"workflow={workflow_id}: close transport failed: {exc}"
                )
                ports._note_close_backoff(workflow_id, close_key)
                return
            if result.stdout.strip():
                print(result.stdout.strip())
            if result.returncode != 0:
                print(
                    f"[WORKFLOW CLOSE ERROR] "
                    f"workflow={workflow_id}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
                ports._note_close_backoff(workflow_id, close_key)
            else:
                ports.attention_clear(close_key)
        finally:
            ports._workflow_close_inflight.discard(workflow_id)

    ports.threading.Thread(
        target=_run,
        daemon=True,
        name=f"wf-close-{workflow_id}",
    ).start()


DEPENDENCIES = {'git_finalize_pending_tasks': ('load_tasks',),
 'git_escalated_tasks': ('load_tasks', 'subprocess'),
 'close_retry_interval': (),
 '_note_close_backoff': ('attention_get', 'attention_note', 'attention_throttle',
                         'close_retry_interval'),
 'maybe_close_completed_workflow': ('TASK_MANAGER', '_close_deferred_logged', '_note_close_backoff',
                                    '_workflow_close_inflight', '_workflow_entry',
                                    'attention_blocks_retry', 'attention_clear',
                                    'git_escalated_tasks', 'git_finalize_pending_tasks', 'liveness',
                                    'subprocess', 'threading')}

__all__ = tuple(DEPENDENCIES)
