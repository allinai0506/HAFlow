"""Persist fix-loop counters/latches and redeliver pending fix notifications.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def _bump_fix_loop_count(ports, workflow_id, retry_node):
    with ports.lock:
        state = ports.load_stage_state()
        key = f"{workflow_id}|fixloop|{retry_node}"
        count = int(state.get(key, 0)) + 1
        state[key] = count
        ports.save_stage_state(state)
    return count

def _fix_loop_count(ports, workflow_id, retry_node):
    with ports.lock:
        state = ports.load_stage_state()
        try:
            return int(state.get(f"{workflow_id}|fixloop|{retry_node}", 0))
        except (TypeError, ValueError):
            return 0

def _fix_loop_exhaustion_episode(ports, workflow_id, gate_node_id):
    return ports.attention_get(f"{workflow_id}:fix_loop_exhausted:{gate_node_id}")

def _exhaustion_fingerprint(ports, episode):
    if not isinstance(episode, dict):
        return None
    try:
        detail = ports.json.loads(episode.get("detail") or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(detail, dict):
        return None
    return detail.get("fingerprint")

def _record_fix_loop_exhaustion(ports,
    workflow_id, gate_node_id, retry_node, reason, summary, fingerprint
):
    import json as _json

    record = dict(summary)
    record["fingerprint"] = fingerprint
    ports.attention_note(
        f"{workflow_id}:fix_loop_exhausted:{gate_node_id}",
        {"task_id": f"fix_loop:{gate_node_id}:{retry_node}",
         "workflow_id": workflow_id},
        "fix_loop",
        reason=reason,
        attempts=ports._fix_loop_count(workflow_id, retry_node),
        detail=_json.dumps(record, ensure_ascii=False)[:2000],
    )

def _fix_loop_latch_info(ports, workflow_id, node_id):
    """Return (latch_ts, gate_node_id); accepts legacy float values."""
    with ports.lock:
        state = ports.load_stage_state()
        raw = state.get(f"{workflow_id}|fixloop|{node_id}|pending_redo")
    gate = None
    ts = 0.0
    if isinstance(raw, dict):
        gate = raw.get("gate")
        try:
            ts = float(raw.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
    else:
        try:
            ts = float(raw or 0)
        except (TypeError, ValueError):
            ts = 0.0
    return ts, gate

def _fix_loop_latch_gates(ports, workflow_id, node_id):
    """闩关联的全部 gate id(P1-2 合并闩存 gates 列表,单门禁回退 gate)。"""
    with ports.lock:
        state = ports.load_stage_state()
        raw = state.get(f"{workflow_id}|fixloop|{node_id}|pending_redo")
    if not isinstance(raw, dict):
        return []
    gates = [str(g).strip() for g in (raw.get("gates") or []) if str(g).strip()]
    if gates:
        return gates
    gate = str(raw.get("gate") or "").strip()
    # 合并闩的 gate 形如 "test+review",拆回单门禁以便逐个清除升级记录。
    if "+" in gate:
        return [g.strip() for g in gate.split("+") if g.strip()]
    return [gate] if gate else []

def _fix_loop_latch_targets(ports, workflow_id, node_id):
    """Selective replan (PR #110) 的目标谱系根;legacy 闩返回 []。"""
    with ports.lock:
        state = ports.load_stage_state()
        raw = state.get(f"{workflow_id}|fixloop|{node_id}|pending_redo")
    if not isinstance(raw, dict):
        return []
    return [
        str(r).strip()
        for r in (raw.get("target_lineage_roots") or [])
        if str(r).strip()
    ]

def _fix_loop_latch_blocks(ports, workflow_id, node_id, tasks=None):
    """作废闩:被回流作废的节点在出现真正重做完成前挡住自动推进。

    有重做完成时顺带清除闩、计数与升级记录,下一轮阻断重新计数;
    指纹保留,后继任务重记录同一结论时按 repeat_verdict 升级而不是
    用新预算再开一轮。Selective 闩(PR #110)只有全部目标谱系重做
    完成才解除;被保留任务的任何更新都不会放行。
    """
    latch_ts, latch_gate = ports._fix_loop_latch_info(workflow_id, node_id)
    if not latch_ts:
        return False
    if tasks is None:
        tasks = ports.load_tasks()
    from herdr import fix_loop as fix_loop_core

    if not fix_loop_core.latch_blocks_advance(
        tasks, node_id, latch_ts,
        target_lineage_roots=ports._fix_loop_latch_targets(workflow_id, node_id),
    ):
        _gates_to_clear = ports._fix_loop_latch_gates(workflow_id, node_id) or (
            [latch_gate] if latch_gate else [])
        with ports.lock:
            state = ports.load_stage_state()
            state.pop(
                f"{workflow_id}|fixloop|{node_id}|pending_redo", None
            )
            # |fp 故意保留:计数归零给 redo 新预算,但指纹必须留存,
            # 否则后继代任务重记录同一结论会逃过 repeat 检测,
            # fix-loop 对已终结 Task 反复下发并逐轮扣预算。
            state.pop(f"{workflow_id}|fixloop|{node_id}", None)
            ports.save_stage_state(state)
        for _gate in _gates_to_clear:
            if _gate:
                ports.attention_clear(f"{workflow_id}:fix_loop_exhausted:{_gate}")
        ports._fix_latch_logged.discard(f"{workflow_id}:{node_id}")
        return False
    return True

def redeliver_pending_fix_loop(ports, workflow_id):
    """补投因总指挥忙而持久化的 fix-loop 通知(sweep 每轮调用,有界)。"""
    if ports.workflow_closed(workflow_id):
        return False
    from herdr import fix_loop as fix_loop_core

    try:
        episodes = ports._attention_store.all()
    except Exception:
        return False
    now = ports.time.time()
    redelivered = False
    for key, episode in episodes.items():
        if not key.startswith(f"{workflow_id}:fix_loop:"):
            continue
        if not isinstance(episode, dict):
            continue
        if (episode.get("event_type") or "") != "fix_loop":
            continue
        if (episode.get("reason") or "") != "coordinator_busy":
            continue
        try:
            summary = ports.json.loads(episode.get("detail") or "{}")
        except (TypeError, ValueError):
            ports.attention_clear(key)
            continue
        if not isinstance(summary, dict) or not summary.get("retry_node"):
            ports.attention_clear(key)
            continue
        tasks = ports.load_tasks()
        if fix_loop_core.redelivery_handled(
            tasks, summary["retry_node"], episode.get("first_seen_at"),
            target_lineage_roots=summary.get("target_lineage_roots"),
            rework_requests=summary.get("rework_requests"),
        ):
            ports.attention_clear(key)
            print(
                f"[FIX LOOP REDELIVERY SKIP] "
                f"workflow={workflow_id} "
                f"retry={summary['retry_node']} "
                "coordinator already dispatched follow-up"
            )
            continue
        if not fix_loop_core.redelivery_due(episode, now):
            continue
        if ports.coordinator_status(workflow_id) not in ("idle", "done"):
            continue
        redelivered_item = {
            "kind": "fix_loop",
            "workflow_id": workflow_id,
            "gate_stage": summary.get("gate_stage", ""),
            "retry_node": summary["retry_node"],
            "blockers": summary.get("blockers") or [],
            "invalidated": [],
            "loop_count": summary.get("loop_count", 0),
            "max_loops": summary.get("max_loops", ports.FIX_LOOP_MAX),
            "suggested_branch": summary.get("suggested_branch"),
            "exhausted": bool(summary.get("exhausted")),
            "redelivered": True,
        }
        # PR #110:选择性返工通知在补投时保留 target 上下文。
        if summary.get("target_lineage_roots"):
            redelivered_item["mode"] = summary.get("mode") or "selective"
            redelivered_item["target_lineage_roots"] = summary[
                "target_lineage_roots"
            ]
        ports.coordinator_queue.put(redelivered_item)
        ports.attention_note(
            key,
            {"task_id": f"fix_loop:{summary.get('gate_stage')}",
             "workflow_id": workflow_id},
            "fix_loop",
            reason="coordinator_busy",
            attempts=int(episode.get("attempts") or 0),
            next_retry_at=now + ports.liveness.attention_retry_interval(),
            detail=episode.get("detail"),
        )
        print(
            f"[FIX LOOP REDELIVERED] "
            f"workflow={workflow_id} "
            f"gate={summary.get('gate_stage')} "
            f"retry={summary['retry_node']}"
        )
        redelivered = True
    return redelivered


DEPENDENCIES = {'_bump_fix_loop_count': ('load_stage_state', 'lock', 'save_stage_state'),
 '_fix_loop_count': ('load_stage_state', 'lock'),
 '_fix_loop_exhaustion_episode': ('attention_get',),
 '_exhaustion_fingerprint': ('json',),
 '_record_fix_loop_exhaustion': ('_fix_loop_count', 'attention_note'),
 '_fix_loop_latch_info': ('load_stage_state', 'lock'),
 '_fix_loop_latch_gates': ('load_stage_state', 'lock'),
 '_fix_loop_latch_targets': ('load_stage_state', 'lock'),
 '_fix_loop_latch_blocks': ('_fix_latch_logged', '_fix_loop_latch_gates', '_fix_loop_latch_info',
                            '_fix_loop_latch_targets', 'attention_clear', 'load_stage_state',
                            'load_tasks', 'lock', 'save_stage_state'),
 'redeliver_pending_fix_loop': ('FIX_LOOP_MAX', '_attention_store', 'attention_clear',
                                'attention_note', 'coordinator_queue', 'coordinator_status', 'json',
                                'liveness', 'load_tasks', 'time', 'workflow_closed')}

__all__ = tuple(DEPENDENCIES)
