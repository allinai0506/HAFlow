"""Invalidate and recover blocked gates whose implementation was already merged.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def invalidate_for_merged_fix_loop(ports,
    workflow_id, gate_node_ids, workflow_cfg, retry_node=None,
    selective_target_task_ids=None,
):
    """多门禁共享 retry_node 时的单次原子作废(P1-2)。

    node 闭包取各门禁下游的并集 + retry 下游 + retry 自身(selective);
    目标两阶段提交与单门禁完全一致(任一目标 pending 即返回,
    各门禁/下游一律不动)。返回 SelectiveInvalidationResult 或普通 list。
    """
    gate_ids = sorted({str(g).strip() for g in (gate_node_ids or []) if str(g).strip()})
    if not gate_ids:
        return []
    primary = gate_ids[0]
    nodes_by_id = {n["id"]: n for n in (workflow_cfg or {}).get("nodes", [])}
    if not nodes_by_id and (workflow_cfg or {}).get("stages"):
        prev = None
        for stage in (workflow_cfg or {}).get("stages", []):
            stage_id = stage.get("key") or stage.get("id")
            nodes_by_id[stage_id] = {
                "id": stage_id,
                "depends_on": [prev] if prev else [],
            }
            prev = stage_id
    node_ids: set = set()
    for gid in gate_ids:
        node_ids |= set(ports._collect_downstream_nodes(nodes_by_id, gid))
    if retry_node and retry_node in nodes_by_id:
        node_ids |= ports._collect_downstream_nodes(nodes_by_id, retry_node) - {retry_node}
        if selective_target_task_ids is not None:
            node_ids.add(retry_node)

    selective_targets = None
    if selective_target_task_ids is not None:
        selective_targets = {
            str(t).strip() for t in selective_target_task_ids if str(t).strip()
        }

    reused = []
    if selective_targets is None:
        reused = ports._rework_legacy_retry(workflow_id, retry_node, "+".join(gate_ids))
        if reused is None:
            return []

    if selective_targets is not None and retry_node:
        ordered_targets = sorted(selective_targets)
        applied: list = []
        failed: dict = {}
        snapshot = {
            str(t.get("task_id") or ""): t
            for t in ports.load_tasks()
            if isinstance(t, dict) and t.get("workflow_id") == workflow_id
        }
        for target_id in ordered_targets:
            record = snapshot.get(target_id)
            if record is None:
                failed[target_id] = "target_task_unknown"
                continue
            if record.get("status") == "superseded" or record.get("superseded_by"):
                applied.append(target_id)
                continue
            ok, err = ports._invalidate_single_task(target_id, "+".join(gate_ids), reuse=True)
            if ok:
                applied.append(target_id)
            else:
                failed[target_id] = err
        if ports.selective_replan_core is not None:
            outcome = ports.selective_replan_core.selective_invalidation_outcome(
                ordered_targets, applied, failed
            )
        else:
            outcome = {
                "applied": applied,
                "pending": [t for t in ordered_targets if t not in applied],
                "failed": failed,
                "all_targets_applied": not failed and len(applied) == len(ordered_targets),
            }
        if not outcome["all_targets_applied"]:
            print(
                f"[SELECTIVE REPLAN PARTIAL] workflow={workflow_id} "
                f"gates={'+'.join(gate_ids)} retry={retry_node} "
                f"applied={','.join(outcome['applied'])} "
                f"pending={','.join(outcome['pending'])}"
            )
            return ports.SelectiveInvalidationResult(
                list(outcome["applied"]),
                applied=list(outcome["applied"]),
                pending=list(outcome["pending"]),
                failed=dict(outcome["failed"]),
            )

    invalidated = list(reused)
    invalidated_nodes = set()
    gate_id_set = set(gate_ids)
    for task in ports.load_tasks():
        if task.get("workflow_id") != workflow_id:
            continue
        if task.get("node") not in node_ids and task.get("stage") not in node_ids:
            continue
        status = task.get("status")
        task_id = task["task_id"]
        task_node = task.get("node") or task.get("stage")
        if selective_targets is None and ports._preserve_same_gate_task(task, retry_node, gate_ids):
            continue
        if task_id in reused or status == "superseded" or (selective_targets is not None and task_id in selective_targets):
            continue
        if (
            selective_targets is not None
            and task_node == retry_node
            and task_id not in selective_targets
        ):
            continue
        if (
            task_node in gate_id_set
            and (not retry_node or retry_node not in gate_id_set)
            and task.get("stage_verdict") == "pass"
            and ports._fix_loop_keepable(task)
        ):
            continue
        if status in ("completed", "cleanup_ready"):
            result = ports.subprocess.run(
                [ports.TASK_MANAGER, "finalize", task_id],
                text=True, capture_output=True,
            )
            if result.returncode != 0:
                continue
            status = (ports.get_task(task_id) or {}).get("status")
        if status not in ports.FIX_LOOP_SUPERSEDEABLE:
            continue
        result = ports.subprocess.run(
            [ports.TASK_MANAGER, "supersede", task_id,
             "--allow-pending",
             "--reason", f"fix-loop: gates {'+'.join(gate_ids)} blocked"],
            text=True, capture_output=True,
        )
        if result.returncode == 0:
            invalidated.append(task_id)
            if task_node:
                invalidated_nodes.add(task_node)
    if invalidated:
        void_nodes = set(invalidated_nodes) | gate_id_set
        if retry_node:
            void_nodes.add(retry_node)
        ports._record_invalidation_note(
            workflow_id, "+".join(gate_ids), retry_node, void_nodes, invalidated
        )
    if selective_targets is not None:
        try:
            phase1 = list(ordered_targets)
        except NameError:
            phase1 = sorted(selective_targets)
        combined = list(phase1) + [t for t in invalidated if t not in phase1]
        return ports.SelectiveInvalidationResult(
            combined, applied=list(phase1), pending=[], failed={})
    void_primary = sorted(invalidated_nodes | {primary})
    if retry_node:
        void_primary.append(retry_node)
    return invalidated

def _handle_merged_fix_loops(ports, workflow_id, retry_node, gates, workflow_cfg):
    """同一 retry_node 多门禁 blocked 的单次合并处理(P1-2)。

    gates: [(gate_node_id, gate_cfg)] 已按 gate id 排序,确定性。
    流程:快照全部 blocked 任务 → 逐门禁 resolve(各 persist 其事实)
    → 任一 fallback 即整组回退 legacy 逐个处理 → 全 selective 则
    union targets 单次原子作废 + 单次 latch/通知。
    """
    from herdr import fix_loop as fix_loop_core

    gate_ids = [str(gid) for gid, _ in gates]
    # 快照:各门禁存活 blocked 任务(后续作废不得影响快照)。
    snapshots: dict = {}
    for gid, _gcfg in gates:
        snapshots[gid] = ports._blocked_gate_tasks_for(workflow_id, gid)
    # 任一门禁无存活 blocked 任务 → 整组无事可做(幂等空转)。
    if not any(snapshots.values()):
        return
    blockers = []
    for gid in sorted(snapshots):
        for task in sorted(snapshots[gid], key=lambda t: str(t.get("task_id") or "")):
            blockers.append({
                "task_id": task.get("task_id"),
                "note": task.get("stage_verdict_note", ""),
            })
    suggested_branch = ports.latest_branch_for_node(workflow_id, retry_node)
    if ports.selective_replan_core is not None:
        affected_union = ports.selective_replan_core.merge_affected_task_ids([
            task.get("stage_verdict_affected_task_ids")
            for tasks in snapshots.values() for task in tasks
        ])
    else:
        affected_union = []
    fingerprint = fix_loop_core.verdict_fingerprint(
        suggested_branch, blockers, affected_task_ids=affected_union
    )
    try:
        max_loops = min(int((gcfg or {}).get("max_loops", ports.FIX_LOOP_MAX)) for _, gcfg in gates)
    except (TypeError, ValueError):
        max_loops = ports.FIX_LOOP_MAX
    with ports.lock:
        _state = ports.load_stage_state()
        try:
            prior_count = int(_state.get(f"{workflow_id}|fixloop|{retry_node}", 0))
        except (TypeError, ValueError):
            prior_count = 0
        stored_fp = _state.get(f"{workflow_id}|fixloop|{retry_node}|fp")
    # 任一门禁已有 exhaustion 记录即整组升级( fail-closed,不重试已升级门禁)。
    for gid in gate_ids:
        if ports._fix_loop_exhaustion_episode(workflow_id, gid):
            reason = "max_loops_exhausted"
            summary = fix_loop_core.summarize_fix_loop_item({
                "workflow_id": workflow_id, "gate_stage": "+".join(sorted(gate_ids)),
                "retry_node": retry_node, "loop_count": prior_count,
                "max_loops": max_loops, "suggested_branch": suggested_branch,
                "blockers": blockers, "exhausted": True,
            })
            ports._record_fix_loop_exhaustion(
                workflow_id, gid, retry_node, reason, summary, fingerprint)
            with ports.lock:
                _state = ports.load_stage_state()
                _state[f"{workflow_id}|fixloop|{retry_node}|pending_redo"] = {
                    "ts": ports.time.time(), "gate": "+".join(sorted(gate_ids)),
                    "gates": sorted(gate_ids),
                }
                ports.save_stage_state(_state)
            ports.coordinator_queue.put({
                "kind": "fix_loop", "workflow_id": workflow_id,
                "gate_stage": "+".join(sorted(gate_ids)), "retry_node": retry_node,
                "blockers": blockers, "invalidated": [], "loop_count": prior_count,
                "max_loops": max_loops, "suggested_branch": suggested_branch,
                "exhausted": True, "escalation_reason": reason,
            })
            return
    if fix_loop_core.fix_loop_exhausted(prior_count, max_loops) or \
            fix_loop_core.is_repeat_verdict(fingerprint, stored_fp):
        reason = ("max_loops_exhausted" if fix_loop_core.fix_loop_exhausted(
            prior_count, max_loops) else "repeat_verdict")
        summary = fix_loop_core.summarize_fix_loop_item({
            "workflow_id": workflow_id, "gate_stage": "+".join(sorted(gate_ids)),
            "retry_node": retry_node, "loop_count": prior_count,
            "max_loops": max_loops, "suggested_branch": suggested_branch,
            "blockers": blockers, "exhausted": True,
        })
        for gid in gate_ids:
            ports._record_fix_loop_exhaustion(
                workflow_id, gid, retry_node, reason, summary, fingerprint)
        with ports.lock:
            _state = ports.load_stage_state()
            _state[f"{workflow_id}|fixloop|{retry_node}|pending_redo"] = {
                "ts": ports.time.time(), "gate": "+".join(sorted(gate_ids)),
                "gates": sorted(gate_ids),
            }
            ports.save_stage_state(_state)
        ports.coordinator_queue.put({
            "kind": "fix_loop", "workflow_id": workflow_id,
            "gate_stage": "+".join(sorted(gate_ids)), "retry_node": retry_node,
            "blockers": blockers, "invalidated": [], "loop_count": prior_count,
            "max_loops": max_loops, "suggested_branch": suggested_branch,
            "exhausted": True, "escalation_reason": reason,
        })
        print(f"[FIX LOOP EXHAUSTED] workflow={workflow_id} gates={'+'.join(sorted(gate_ids))} reason={reason}")
        return

    # 逐门禁 resolve(各 persist 其事实)。任一非 selective 即整组回退 legacy。
    per_gate_targets: dict = {}
    per_gate_plans: dict = {}
    all_selective = True
    for gid, gcfg in gates:
        _rn = ports._retry_node_for_gate(workflow_cfg, gcfg, gid)
        tids, plan = ports._resolve_selective_replan(
            workflow_id, gid, _rn, workflow_cfg, snapshots[gid], ports.load_tasks())
        if tids is None:
            all_selective = False
            break
        per_gate_targets[gid] = list(tids)
        per_gate_plans[gid] = plan
    if not all_selective:
        # 整组回退 legacy:逐门禁按原单门禁路径处理(legacy  sequential 安全,
        # 无 targets 可丢;首个作废后其余自然空转)。
        for gid, gcfg in gates:
            # 避免递归进合并:直接走单门禁 legacy?调用 handle 会再次检测
            # sibling 并合并 → 无限递归。改为临时只处理单门禁 legacy:
            # 此时 selective 已整体放弃,按 legacy invalidate 单门禁。
            _legacy_blocked = ports._blocked_gate_tasks_for(workflow_id, gid)
            if not _legacy_blocked:
                continue
            _inv = ports.invalidate_for_fix_loop(
                workflow_id, gid, workflow_cfg,
                retry_node=ports._retry_node_for_gate(workflow_cfg, gcfg, gid),
                selective_target_task_ids=None,
            )
            if _inv:
                _lc = ports._bump_fix_loop_count(
                    workflow_id, ports._retry_node_for_gate(workflow_cfg, gcfg, gid))
                with ports.lock:
                    _st = ports.load_stage_state()
                    _st[f"{workflow_id}|fixloop|{ports._retry_node_for_gate(workflow_cfg, gcfg, gid)}|pending_redo"] = {
                        "ts": ports.time.time(), "gate": gid}
                    ports.save_stage_state(_st)
                ports.coordinator_queue.put({
                    "kind": "fix_loop", "workflow_id": workflow_id,
                    "gate_stage": gid,
                    "retry_node": ports._retry_node_for_gate(workflow_cfg, gcfg, gid),
                    "blockers": [{"task_id": t.get("task_id"),
                                  "note": t.get("stage_verdict_note", "")}
                                 for t in _legacy_blocked],
                    "invalidated": list(_inv), "loop_count": _lc,
                    "max_loops": (gcfg or {}).get("max_loops", ports.FIX_LOOP_MAX),
                    "suggested_branch": ports.latest_branch_for_node(
                        workflow_id, ports._retry_node_for_gate(workflow_cfg, gcfg, gid)),
                })
        return

    union_targets = ports.selective_replan_core.merge_affected_task_ids(
        list(per_gate_targets.values())) if ports.selective_replan_core else sorted(
        {t for v in per_gate_targets.values() for t in v})
    union_roots = sorted({
        str(r).strip()
        for plan in per_gate_plans.values()
        for r in ((plan or {}).get("target_lineage_roots") or [])
        if str(r).strip()})
    # 单次原子作废(目标两阶段,门禁取并集)。
    invalidated = ports.invalidate_for_merged_fix_loop(
        workflow_id, gate_ids, workflow_cfg, retry_node=retry_node,
        selective_target_task_ids=union_targets)
    if isinstance(invalidated, ports.SelectiveInvalidationResult) and not invalidated.all_targets_applied:
        print(f"[SELECTIVE REPLAN DEFERRED] workflow={workflow_id} gates={'+'.join(sorted(gate_ids))} pending={','.join(invalidated.pending)} awaiting retry")
        return
    if not invalidated:
        return
    ports.clear_stage_advance(workflow_id, retry_node)
    loop_count = ports._bump_fix_loop_count(workflow_id, retry_node)
    # latch 取各事实最早创建时间(恒早于一切 replacement,避免重试 race)。
    fact_ts = 0.0
    for plan in per_gate_plans.values():
        try:
            cand = float((plan or {}).get("created_at") or 0)
        except (TypeError, ValueError):
            cand = 0
        if cand and (not fact_ts or cand < fact_ts):
            fact_ts = cand
    with ports.lock:
        _state = ports.load_stage_state()
        _pending = {"ts": fact_ts or ports.time.time(),
                    "gate": "+".join(sorted(gate_ids)),
                    "gates": sorted(gate_ids),
                    "mode": "selective",
                    "target_lineage_roots": union_roots}
        _state[f"{workflow_id}|fixloop|{retry_node}|pending_redo"] = _pending
        _state[f"{workflow_id}|fixloop|{retry_node}|fp"] = fingerprint
        ports.save_stage_state(_state)
    ports.coordinator_queue.put({
        "kind": "fix_loop", "workflow_id": workflow_id,
        "gate_stage": "+".join(sorted(gate_ids)), "retry_node": retry_node,
        "blockers": blockers, "invalidated": list(invalidated),
        "loop_count": loop_count,
        "max_loops": max_loops, "suggested_branch": suggested_branch,
        "mode": "selective", "target_lineage_roots": union_roots,
    })
    print(f"[FIX LOOP QUEUED] workflow={workflow_id} gates={'+'.join(sorted(gate_ids))} retry={retry_node} loop={loop_count} invalidated={len(invalidated)}")


DEPENDENCIES = {'invalidate_for_merged_fix_loop': ('FIX_LOOP_SUPERSEDEABLE', 'SelectiveInvalidationResult',
                                    'TASK_MANAGER', '_collect_downstream_nodes',
                                    '_fix_loop_keepable', '_invalidate_single_task',
                                    '_preserve_same_gate_task', '_record_invalidation_note',
                                    '_rework_legacy_retry', 'get_task', 'load_tasks',
                                    'selective_replan_core', 'subprocess'),
 '_handle_merged_fix_loops': ('FIX_LOOP_MAX', 'SelectiveInvalidationResult',
                              '_blocked_gate_tasks_for', '_bump_fix_loop_count',
                              '_fix_loop_exhaustion_episode', '_record_fix_loop_exhaustion',
                              '_resolve_selective_replan', '_retry_node_for_gate',
                              'clear_stage_advance', 'coordinator_queue', 'invalidate_for_fix_loop',
                              'invalidate_for_merged_fix_loop', 'latest_branch_for_node',
                              'load_stage_state', 'load_tasks', 'lock', 'save_stage_state',
                              'selective_replan_core', 'time')}

__all__ = tuple(DEPENDENCIES)
