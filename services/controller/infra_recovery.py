"""Recover infrastructure-failed tasks and apply observed crash transitions.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""
import os

def _redispatch_sla_seconds():
    """Deadline for "supersede -> successor dispatched" before escalation.

    wf-haflow-1022-01 (2026-10-23): after the auto-supersede of a fuse-killed
    fix task, no dispatch path picked the node up again (scheduler already
    considered the node complete; fix-loop recovery sat in waiting_human) and
    the workflow stalled ~8h with zero events. The supersede alone is not a
    recovery; it is a promise that a successor appears. This SLA bounds how
    long that promise may stay unverified before a human must be told.
    """
    raw = os.environ.get("HERDR_AUTO_RECOVER_REDISPATCH_SLA", "300")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 300.0
    if value < 30.0:
        return 30.0
    if value > 3600.0:
        return 3600.0
    return value


def should_retry_finalize(ports, status, episode, now, max_attempts=None):
    """(should_retry, reason, exhausted) — committed/completed 的终化重试决策。

    `completed` 且 integration_mode=git 的任务若 commit 门禁瞬时失败，此前
    没有任何重试路径，只能人工或总指挥手工重试（实测一次 flaky gate 浪费
    55min 并烧掉总指挥大回合）。本函数给出统一的退避重试 + 上限判定。
    """
    if status not in ("committed", "completed"):
        return False, "", False

    limit = ports.FINALIZE_RETRY_MAX if max_attempts is None else int(max_attempts)
    attempts = int((episode or {}).get("attempts") or 0)
    if attempts >= limit:
        return False, "", True

    if float((episode or {}).get("next_retry_at") or 0) > now:
        return False, "", False
    reason = "integration_retry" if status == "committed" else "commit_retry"
    return True, reason, False

def verify_auto_recover_redispatch(ports, workflow_id, tasks=None):
    """Verify every armed auto-recover supersede actually produced a successor.

    Resolution: the superseded task gained ``superseded_by``, or its node has a
    live (non-terminal) task again. Past the SLA deadline without either, the
    episode escalates exactly once to a human notification plus a durable
    audit event. Notification and event writes happen outside the episode
    transaction (locks stay short; notification is best-effort control-bypass).
    """
    if not workflow_id or ports.workflow_closed(workflow_id):
        return False
    prefix = f"{workflow_id}:auto_recover:"
    now = ports.time.time()
    escalated = []
    with ports._attention_store.transaction() as episodes:
        for key in list(episodes.keys()):
            if not key.startswith(prefix):
                continue
            episode = episodes.get(key) or {}
            if episode.get("reason") != "auto_recover_awaiting_redispatch":
                continue
            task_id = str(episode.get("task_id") or "")
            task = None
            if task_id:
                try:
                    task = ports.get_task(task_id)
                except (OSError, RuntimeError, ValueError, AttributeError):
                    task = None
            node_id = (task or {}).get("node") or episode.get("node")
            resolved = bool((task or {}).get("superseded_by")) or any(
                t.get("workflow_id") == workflow_id
                and (t.get("node") or t.get("stage")) == node_id
                and t.get("task_id") != task_id
                and t.get("status") not in ports.liveness.RECOVERY_TERMINAL_STATUSES
                for t in (tasks or [])
            )
            if resolved:
                episodes.pop(key, None)
                print(
                    f"[AUTO RECOVER VERIFIED] workflow={workflow_id} "
                    f"task={task_id} successor present; contract closed"
                )
                continue
            if episode.get("escalated"):
                continue
            if now < float(episode.get("next_retry_at") or 0):
                continue
            episodes[key] = dict(episode, escalated=True, last_attempt_at=now)
            escalated.append((task_id, node_id, key))

    for task_id, node_id, key in escalated:
        detail = (
            f"auto-supersede of {task_id} (node={node_id}) produced no successor "
            f"within SLA; node has no live task. Manual recovery required: "
            f"relaunch a replacement or resolve the fix-loop recovery operation."
        )
        try:
            ports.notify_attention(
                "Herdr Factory · 自动补派未落地",
                {"task_id": task_id, "workflow_id": workflow_id},
                detail,
                "auto_recover_redispatch_stalled",
            )
        except (OSError, RuntimeError, ValueError, AttributeError):
            print(f"[AUTO RECOVER NOTIFY ERROR] workflow={workflow_id} task={task_id}")
        try:
            ports._get_store().record_event(
                "auto_recover_redispatch_stalled",
                {
                    "workflow_id": workflow_id,
                    "task_id": task_id,
                    "node": node_id,
                    "attention_key": key,
                },
                workflow_id=workflow_id,
                task_id=task_id or None,
                source="herdr-controller",
            )
        except (OSError, RuntimeError, ValueError, AttributeError) as exc:
            print(f"[AUTO RECOVER AUDIT ERROR] workflow={workflow_id}: {exc}")
    return bool(escalated)


def converge_dangling_replacement_obligations(ports, workflow_id, tasks=None):
    """Resolve superseded rows whose replacement obligation can never be met.

    wf-haflow-1022-01: a superseded row left ``replacement_pending=True`` with
    no successor (its replacement was launched as a fresh root without
    ``--supersedes``); the obligations walk then made ``node_is_complete``
    permanently False while the planner simultaneously refused redispatch
    ("node has active tasks") — a two-way deadlock that stalled wrapup.

    Convergence rule (conservative): the dangling obligation resolves only
    when its own node already carries a settled PASS verifying the CURRENT
    frozen candidate — the node's verification duty is factually fulfilled;
    waiting for a successor that will never come protects nothing. Every
    resolution is audited via a ``replacement_obligation_auto_resolved`` event.
    """
    if not workflow_id or ports.workflow_closed(workflow_id):
        return False
    if tasks is None:
        tasks = ports.load_tasks()
    node_tasks = [t for t in (tasks or []) if t.get("workflow_id") == workflow_id]
    if not node_tasks:
        return False
    try:
        frozen = str(ports._scheduler_current_frozen_candidate_sha(workflow_id) or "")
    except (OSError, RuntimeError, ValueError, AttributeError):
        frozen = ""
    import re as _re
    if not _re.fullmatch(r"[0-9a-fA-F]{40}", frozen):
        return False
    settled_nodes = {}
    for task in node_tasks:
        st = str(task.get("status") or "")
        node_id = task.get("node") or task.get("stage")
        if not node_id:
            continue
        if st in ("cleaned", "completed", "committed", "integrated", "cleanup_ready"):
            verdict = str(task.get("stage_verdict") or "")
            if verdict in ("pass", "approved"):
                verified = str(task.get("verified_candidate_sha")
                               or task.get("candidate_sha") or "")
                if verified.lower() == frozen.lower():
                    settled_nodes.setdefault(node_id, (task.get("task_id"), "settled_gate_pass"))
            elif not verdict:
                # Non-gate node (e.g. implementation): deliverable settled
                # 交付必须落在当前冻结 candidate 上；历史终态任务（candidate
                # 不同）不是本义务的交付证据，不得替替代任务清账
                # (PR #360 评审遗留)。
                candidate = str(task.get("candidate_sha") or task.get("integrated_commit") or task.get("commit") or "")
                if candidate and candidate.lower() == frozen.lower():
                    settled_nodes.setdefault(node_id, (task.get("task_id"), "settled_node_delivery"))
    if not settled_nodes:
        return False
    resolved = False
    for task in node_tasks:
        if (task.get("status") != "superseded"
                or task.get("replacement_pending") is not True
                or task.get("superseded_by")):
            continue
        node_id = task.get("node") or task.get("stage")
        if node_id not in settled_nodes:
            continue
        task_id = task.get("task_id")
        resolved_by, settle_reason = settled_nodes[node_id]
        try:
            ports._get_store().update_task_metadata(
                task_id, {"replacement_pending": False}
            )
        except (OSError, RuntimeError, ValueError, AttributeError) as exc:
            print(f"[OBLIGATION CONVERGE ERROR] workflow={workflow_id} task={task_id}: {exc}")
            continue
        try:
            ports._get_store().record_event(
                "replacement_obligation_auto_resolved",
                {
                    "workflow_id": workflow_id,
                    "task_id": task_id,
                    "node": node_id,
                    "resolved_by": resolved_by,
                    "candidate_sha": frozen,
                    "reason": f"dangling replacement obligation (superseded, pending, "
                              f"no successor) converged by {settle_reason}",
                },
                workflow_id=workflow_id,
                task_id=task_id,
                source="herdr-controller",
            )
        except (OSError, RuntimeError, ValueError, AttributeError):
            pass
        resolved = True
        print(
            f"[OBLIGATION CONVERGED] workflow={workflow_id} node={node_id} "
            f"task={task_id} -> fulfilled by {resolved_by} "
            f"on candidate {frozen[:8]}"
        )
    return resolved


def recover_infra_failed_tasks(ports, workflow_id, tasks=None):
    """基础设施失败任务自动作废,交给 sweep 重新派发替代任务。

    此前 failed 任务只能等人工 relaunch(--supersedes),实测造成 28 分钟级
    空等;这里复用既有 superseded->replace 派发管线:作废后清掉该节点的
    stage-advance 闩,下一轮 sweep 的 direct dispatch 会自动补派 -rN。
    谱系级次数上限由纯函数 select_infra_failures_for_recovery 保证,
    达到上限后不再自动处置(只留日志与告警)。

    作废本身不是恢复:每次作废都登记 attention 期限,由
    verify_auto_recover_redispatch 在 SLA 内核验继任者真的出现,否则升级
    人工(wf-haflow-1022-01 实测:sweep 补派契约对已完成节点的修复谱系
    不成立时,曾静默停滞 8 小时)。
    """
    if not workflow_id:
        return False

    if ports.workflow_closed(workflow_id):
        return False

    if tasks is None:
        tasks = ports.load_tasks()

    verify_auto_recover_redispatch(ports, workflow_id, tasks)

    wf_st = ports._workflow_entry(workflow_id).get("status")
    if wf_st in ("completed", "paused"):
        return False

    candidates = ports.liveness.select_infra_failures_for_recovery(
        [t for t in tasks if t.get("workflow_id") == workflow_id],
        ports.AUTO_RECOVER_REASONS,
        max_attempts=ports.AUTO_RECOVER_MAX_ATTEMPTS,
    )

    recovered = False
    for task in candidates:
        task_id = task.get("task_id")
        node_id = task.get("node") or task.get("stage")

        result = ports.subprocess.run(
            [
                ports.TASK_MANAGER, "supersede", task_id,
                "--allow-pending",
                "--reason", "auto-recover: infrastructure failure",
            ],
            text=True,
            capture_output=True,
        )

        output = result.stderr.strip() or result.stdout.strip()
        # 幂等短路:另一执行者(人工 relaunch/并行 sweep)已完成同一作废,
        # "superseded -> superseded" 是终态已达成,不是需要重试的错误。
        already_superseded = (
            result.returncode != 0 and "-> superseded" in output
        )
        if result.returncode != 0 and not already_superseded:
            print(
                f"[AUTO RECOVER ERROR] supersede {task_id}: {output}"
            )
            continue

        if node_id:
            ports.clear_stage_advance(workflow_id, node_id)

        ports.attention_note(
            f"{workflow_id}:auto_recover:{task_id}",
            {"task_id": task_id, "workflow_id": workflow_id, "node": node_id},
            "auto_recover",
            reason="auto_recover_awaiting_redispatch",
            attempts=1,
            next_retry_at=ports.time.time() + _redispatch_sla_seconds(),
            detail="superseded by auto-recover; successor dispatch expected",
        )

        recovered = True
        print(
            f"[AUTO RECOVER{' IDEMPOTENT' if already_superseded else ''}] workflow={workflow_id} "
            f"node={node_id} task={task_id} "
            "superseded -> pending redispatch by sweep"
        )

    return recovered

def process_crash_observations(ports):
    """Consume durable Sentinel crash observations through the task CAS."""
    store = ports._get_store()
    try:
        events = store.list_events(event_type="agent_process_crash_observed")
    except (AttributeError, OSError, RuntimeError, ValueError):
        return 0
    processed = 0
    for event in events:
        task_id = event.get("task_id")
        if not task_id:
            continue
        task = store.get_task(task_id)
        if not task or task.get("status") not in {
            "dispatched", "working", "rework", "blocked"
        }:
            continue
        payload = event.get("payload") or {}
        try:
            expected_status = payload.get("observed_status") or task.get("status")
            expected_version = int(
                payload.get("observed_version", task.get("version"))
            )
        except (TypeError, ValueError):
            continue
        try:
            result = store.compare_and_set_task_transition(
                task_id,
                to_status="failed",
                reason="agent_process_crash",
                source="herdr-controller",
                metadata={
                    "sentinel_reason": "agent_process_crash",
                    "crash_event_id": event.get("id"),
                },
                expected_status=expected_status,
                expected_version=expected_version,
            )
        except (AttributeError, OSError, RuntimeError, ValueError):
            continue
        if result.get("accepted", False):
            processed += 1
    return processed

def recover_router_isolation_tasks(ports, workflow_id, tasks=None):
    """Retry a router-rejected task only after an eligible pool exists.

    wf-haflow-1022-01 test-auto-r5: the refusal row pinned its originally
    requested agent, and this retry re-asked for that same agent forever
    (it stays isolation-rejected by its own stage verdicts) while eligible
    alternatives existed. The requested agent is tried first for intent
    fidelity; when it is still isolation-rejected, ``auto`` re-resolves the
    pool before giving up.
    """
    from herdr.agent_router import choose_agent

    if not workflow_id or ports.workflow_closed(workflow_id):
        return False
    if tasks is None:
        tasks = ports.load_tasks()
    recovered = False
    for task in tasks or []:
        if (
            task.get("workflow_id") != workflow_id
            or task.get("status") != "failed"
            or task.get("failure_reason") != "router_isolation_rejected"
            or task.get("superseded_by")
        ):
            continue
        requested = task.get("agent") or "auto"
        eligible = False
        try:
            choose_agent(
                workflow_id,
                task.get("node") or task.get("stage"),
                task.get("task_type") or "test",
                requested=requested,
            )
            eligible = True
        except (OSError, RuntimeError, ValueError, AttributeError):
            eligible = False
        if not eligible and requested != "auto":
            try:
                choose_agent(
                    workflow_id,
                    task.get("node") or task.get("stage"),
                    task.get("task_type") or "test",
                    requested="auto",
                )
                eligible = True
            except (OSError, RuntimeError, ValueError, AttributeError):
                eligible = False
        if not eligible:
            continue
        result = ports.subprocess.run(
            [
                ports.TASK_MANAGER, "supersede", task.get("task_id"),
                "--allow-pending",
                "--reason", "router pool recovered; replacement eligible",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            recovered = True
            print(
                f"[ROUTER RECOVERY] workflow={workflow_id} "
                f"task={task.get('task_id')} eligible pool restored"
            )
        else:
            try:
                fresh = ports.get_task(task.get("task_id"))
            except (OSError, RuntimeError, ValueError, AttributeError):
                fresh = None
            if (fresh or {}).get("status") == "superseded" or (fresh or {}).get("superseded_by"):
                recovered = True
                print(
                    f"[ROUTER RECOVERY] workflow={workflow_id} "
                    f"task={task.get('task_id')} already superseded "
                    f"by={(fresh or {}).get('superseded_by')}; nothing to do"
                )
                continue
            print(
                f"[ROUTER RECOVERY ERROR] task={task.get('task_id')}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
    return recovered


DEPENDENCIES = {'should_retry_finalize': ('FINALIZE_RETRY_MAX',),
 'verify_auto_recover_redispatch': ('_attention_store', '_get_store', 'get_task',
                                    'liveness', 'notify_attention', 'time',
                                    'workflow_closed'),
 'converge_dangling_replacement_obligations': ('_get_store', 'load_tasks',
                                               '_scheduler_current_frozen_candidate_sha',
                                               'workflow_closed'),
 'recover_infra_failed_tasks': ('AUTO_RECOVER_MAX_ATTEMPTS', 'AUTO_RECOVER_REASONS', 'TASK_MANAGER',
                                '_attention_store', '_get_store', '_workflow_entry',
                                'attention_note', 'clear_stage_advance', 'get_task',
                                'liveness', 'load_tasks', 'notify_attention',
                                'subprocess', 'time', 'workflow_closed'),
 'process_crash_observations': ('_get_store',),
 'recover_router_isolation_tasks': ('TASK_MANAGER', 'get_task', 'load_tasks', 'subprocess',
                                    'workflow_closed')}

__all__ = tuple(DEPENDENCIES)
