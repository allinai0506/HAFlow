"""Apply and reconcile machine-readable gate verdicts with bounded deferral.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def max_clock_skew_seconds(ports):
    raw = ports.os.environ.get("HERDR_MAX_CLOCK_SKEW_SECONDS")
    if raw is not None and raw.strip():
        try:
            val = float(raw.strip())
            if val >= 0 and ports.math.isfinite(val):
                return val
        except (ValueError, TypeError):
            pass
    return ports.DEFAULT_MAX_CLOCK_SKEW_SECONDS

def task_gate_handoff_grace_seconds(ports):
    raw = ports.os.environ.get("HERDR_GATE_HANDOFF_GRACE_SECONDS")
    if raw is not None and raw.strip():
        try:
            val = float(raw.strip())
            if val > 0 and ports.math.isfinite(val):
                return val
        except (ValueError, TypeError):
            pass
    return ports.DEFAULT_GATE_HANDOFF_GRACE_SECONDS

def process_gate_verdict_reconciliations(ports):
    """PR #250 & Codex P1: Reconcile gate tasks that have written a valid verdict.

    Covers two failure modes:
    1. A working gate task where gate_verdict_ready is True is transitioned to agent_done.
       Requires verdict freshness for current execution epoch AND agent runtime settled (idle/done).
    2. A gate task failed by watchdog where a valid gate verdict file exists is auto-adopted.
    """
    processed = 0
    for task in ports.load_tasks():
        status = task.get("status")
        workflow_id = task.get("workflow_id")
        node_id = task.get("node") or task.get("stage")
        if task.get("gate") is False:
            continue
        if task.get("gate") is not None:
            is_gate = True
        elif workflow_id and node_id:
            is_gate = ports.node_is_gate(workflow_id, node_id)
        else:
            is_gate = False
        if not is_gate:
            continue

        task_id = task.get("task_id")
        if status in {"working", "dispatched", "rework"}:
            if not ports._gate_verdict_is_fresh(task):
                if hasattr(ports, "sync_verdict_from_business_acceptance"):
                    ports.sync_verdict_from_business_acceptance(task)
            if not ports._gate_verdict_is_fresh(task):
                continue
            pane_id = task.get("pane_id")
            runtime_settled = True
            if pane_id:
                try:
                    runtime = ports.get_agent_runtime_status(pane_id)
                    runtime_settled = (runtime in ("idle", "done"))
                except Exception:
                    runtime_settled = False
            if not runtime_settled:
                # Issue #250 & Codex review: If runtime is not settled, allow adoption after bounded handoff grace.
                # Timestamps beyond clock skew allowance are rejected from extending grace.
                verdict_mtime = ports._gate_verdict_file_mtime(task)
                grace = ports.task_gate_handoff_grace_seconds()
                skew_limit = ports.max_clock_skew_seconds()
                now = ports.time.time()
                is_future = verdict_mtime is not None and (verdict_mtime - now) > skew_limit
                if not verdict_mtime:
                    continue
                if not is_future and (now - verdict_mtime) < grace:
                    continue
            if ports.gate_verdict_ready(task):
                if ports._set_observed_status(task, "agent_done", "gate_verdict_recovery"):
                    ports.emit_done_if_allowed(ports.get_task(task_id))
                    processed += 1
        elif status == "failed" and task.get("failure_reason") == "hard_timeout_watchdog":
            if not ports._gate_verdict_is_fresh(task):
                if hasattr(ports, "sync_verdict_from_business_acceptance"):
                    ports.sync_verdict_from_business_acceptance(task)
            if not ports._gate_verdict_is_fresh(task):
                continue
            verdict, _, _ = ports.read_gate_verdict(task)
            if verdict in ("pass", "blocked"):
                if ports.try_auto_verdict(task_id):
                    processed += 1
    return processed

def try_auto_verdict(ports, task_id):
    """门禁节点规则化裁决：报告/终端给出唯一结论时直接落 verdict。

    pass -> 推进下一节点；blocked -> 既有 fix-loop 自动回流。
    信号缺失/冲突、非门禁节点、环境开关关闭时返回 False 回落总指挥。
    """
    if not ports.auto_verdict_enabled():
        return False

    task = ports.get_task(task_id)
    if not task:
        return False

    status = task.get("status")
    if status != "agent_done":
        # Issue #250 & Codex review: Allow adopting gate verdict if task was placed in failed
        # (e.g. by watchdog) but produced a valid, fresh machine-readable gate verdict.
        if not (status == "failed" and task.get("failure_reason") == "hard_timeout_watchdog" and ports._gate_verdict_is_fresh(task)):
            return False

    workflow_id = task.get("workflow_id")
    node_id = task.get("node") or task.get("stage")
    if not workflow_id or not node_id:
        return False

    if not ports.node_is_gate(workflow_id, node_id):
        return False

    verdict, note, source = ports.read_gate_verdict(task)
    if verdict not in ("pass", "blocked"):
        if hasattr(ports, "sync_verdict_from_business_acceptance"):
            ports.sync_verdict_from_business_acceptance(task)
            verdict, note, source = ports.read_gate_verdict(task)
    if verdict not in ("pass", "blocked"):
        return False
    if verdict == "blocked" and not note:
        note = f"gate verdict blocked (auto-verdict via {source or 'signal'})"

    if status == "failed":
        # Issue #250 & Codex review: Verify gate verdict is unchanged immediately before spawning adoption CLI
        latest_verdict, _, _ = ports.read_gate_verdict(task)
        if latest_verdict != verdict:
            print(
                f"[AUTO VERDICT ABORT] task={task_id}: gate verdict changed from {verdict} to {latest_verdict} before adoption",
                file=ports.sys.stderr,
            )
            return False

    set_cmd = [
        ports.TASK_MANAGER, "set", task_id, "completed",
        "--verdict", verdict, "--note", note,
    ]
    if verdict == "blocked":
        # PR #110:结构化归因只认结论文件字段,屏幕文本绝不作为来源。
        affected_ids = ports._verdict_affected_task_ids(task)
        if affected_ids:
            for affected_id in affected_ids:
                set_cmd.extend(["--affected-task-id", affected_id])
    result = ports.subprocess.run(set_cmd, text=True, capture_output=True)
    if result.returncode != 0:
        print(
            f"[AUTO VERDICT ERROR] task={task_id}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
        return False

    print(
        f"[AUTO VERDICT] task={task_id} node={node_id} "
        f"verdict={verdict} source={source}"
    )
    return True

def classify_verdict_deferral(ports, *, is_gate, auto_accept_on=True, auto_verdict_on=True,
                              stage_verdict=None, role_text="", has_changes=False,
                              gate_verdict=None):
    """Pure: name why the done-event fast path would defer to the coordinator.

    Returns "" when the fast path should have fired; a "" on a real miss
    means a CAS race or an unknown miss, never silence.
    """
    if not is_gate:
        if not auto_accept_on:
            return "auto_accept_disabled"
        if stage_verdict == "blocked":
            return "stage_verdict_blocked"
        if ports.re.search(r"(?:^|[^a-z])(review(?:er)?|adversarial)(?:$|[^a-z])",
                     str(role_text or "").lower()):
            return "review_role"
        if not has_changes:
            return "no_task_changes"
        return ""
    if not auto_verdict_on:
        return "auto_verdict_disabled"
    if gate_verdict not in ("pass", "blocked"):
        return "gate_signal_missing_or_conflict"
    return ""

def verdict_defer_reason(ports, task):
    """Best-effort reason why agent_done did not fast-path to verdict/completion."""
    try:
        workflow_id = task.get("workflow_id")
        node_id = task.get("node") or task.get("stage")
        is_gate = ports.node_is_gate(workflow_id, node_id) if workflow_id and node_id else True
        role_text = " ".join(str(task.get(key) or "") for key in
                             ("dispatch_role", "agent_role", "role", "task_id"))
        gate_verdict = None
        if is_gate:
            gate_verdict, _, _ = ports.read_gate_verdict(task)
        return ports.classify_verdict_deferral(
            is_gate=is_gate,
            auto_accept_on=ports.auto_accept_enabled(),
            auto_verdict_on=ports.auto_verdict_enabled(),
            stage_verdict=task.get("stage_verdict"),
            role_text=role_text,
            has_changes=ports.task_changes_recorded(task) if not is_gate else False,
            gate_verdict=gate_verdict,
        )
    except Exception:
        return "defer_reason_unknown"


DEPENDENCIES = {'max_clock_skew_seconds': ('DEFAULT_MAX_CLOCK_SKEW_SECONDS', 'math', 'os'),
 'task_gate_handoff_grace_seconds': ('DEFAULT_GATE_HANDOFF_GRACE_SECONDS', 'math', 'os'),
 'process_gate_verdict_reconciliations': ('_gate_verdict_file_mtime', '_gate_verdict_is_fresh',
                                          '_set_observed_status', 'emit_done_if_allowed',
                                          'gate_verdict_ready', 'get_agent_runtime_status',
                                          'get_task', 'load_tasks', 'max_clock_skew_seconds',
                                          'node_is_gate', 'read_gate_verdict',
                                          'sync_verdict_from_business_acceptance',
                                          'task_gate_handoff_grace_seconds', 'time',
                                          'try_auto_verdict', 'workflow_config_for'),
 'try_auto_verdict': ('TASK_MANAGER', '_gate_verdict_is_fresh', '_verdict_affected_task_ids',
                      'auto_verdict_enabled', 'get_task', 'node_is_gate', 'read_gate_verdict',
                      'subprocess', 'sync_verdict_from_business_acceptance', 'sys'),
 'classify_verdict_deferral': ('re',),
 'verdict_defer_reason': ('auto_accept_enabled', 'auto_verdict_enabled',
                          'classify_verdict_deferral', 'node_is_gate', 'read_gate_verdict',
                          'task_changes_recorded')}

__all__ = tuple(DEPENDENCIES)
