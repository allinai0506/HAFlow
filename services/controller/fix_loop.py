"""Arbitrate blocked gates and apply the existing retry-node policy.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def handle_fix_loop(ports, workflow_id, gate_node_id, gate_cfg, workflow_cfg):
    """原子作废 + 计数 + 投递 fix_loop 事件;幂等(无作废即不重发)。"""
    retry_node = gate_cfg.get("retry_node", "implementation")

    nodes_by_id = {
        n["id"]: n for n in workflow_cfg.get("nodes", [])
    }
    if nodes_by_id and retry_node not in nodes_by_id:
        gate_deps = nodes_by_id.get(gate_node_id, {}).get("depends_on", [])
        fallback = next(
            (dep for dep in gate_deps if dep in nodes_by_id),
            None,
        ) or next(iter(nodes_by_id))
        print(
            f"[FIX LOOP RETRY NODE FALLBACK] "
            f"{retry_node} not in workflow nodes, using {fallback}"
        )
        retry_node = fallback

    # P1-2:同一 retry_node 的多门禁并行 blocked 时,先冻结本轮全部
    # blocked gate facts,再进入 invalidation。否则先处理的 gate 会把
    # 另一个 blocked gate Task 一并作废,后者的 affected_task_ids 丢失。
    # 仅 selective 策略开启时合并;legacy 工作流保持逐门禁原行为。
    try:
        _merge_enabled = (
            ports.selective_replan_core is not None
            and ports.selective_replan_core.policy_from_workflow(workflow_cfg) is not None
            and ports.selective_replan_core.policy_from_workflow(
                workflow_cfg)["retry_node"] == str(retry_node or "")
        )
    except Exception:
        _merge_enabled = False
    if _merge_enabled:
        try:
            _sibling_gates: dict = {}
            for _task in ports.load_tasks():
                if not isinstance(_task, dict):
                    continue
                if _task.get("workflow_id") != workflow_id:
                    continue
                if _task.get("status") == "superseded":
                    continue
                if _task.get("stage_verdict") != "blocked":
                    continue
                _node = str(_task.get("node") or _task.get("stage") or "")
                if not _node:
                    continue
                try:
                    _gcfg = ports.resolve_gate_config(
                        (workflow_cfg.get("nodes") and
                         {n["id"]: n for n in workflow_cfg.get("nodes", [])}.get(_node)),
                        _node,
                    )
                except Exception:
                    _gcfg = None
                if not _gcfg:
                    continue
                _rn = ports._retry_node_for_gate(workflow_cfg, _gcfg, _node)
                if _rn != retry_node:
                    continue
                _sibling_gates.setdefault(_node, _gcfg)
            if len(_sibling_gates) > 1 and str(gate_node_id) in _sibling_gates:
                _ordered = sorted(_sibling_gates)
                # P1-2 round-2:组内任一门禁触发都必须进入合并处理,不得再要求
                # 调用方恰好是字典序首位。真实 sweep 经 blocked_gate_dependency
                # 按 depends_on 顺序永远先返回 test,而 sorted 首位是 review,
                # 旧限制会让合并入口永久不可达。_handle_merged_fix_loops 对
                # 已处理过的组自然空转(快照无存活 blocked 即返回),故任意入口
                # 触发都是幂等的。
                _merged = [(gid, _sibling_gates[gid]) for gid in _ordered]
                ports._handle_merged_fix_loops(workflow_id, retry_node, _merged, workflow_cfg)
                return
        except Exception as exc:
            print(
                f"[SELECTIVE REPLAN WARN] workflow={workflow_id} "
                f"sibling gate scan skipped: {exc}"
            )

    blockers = []
    blocked_gate_tasks = []

    for task in ports.load_tasks():
        if task.get("workflow_id") != workflow_id:
            continue
        if gate_node_id not in (task.get("node"), task.get("stage")):
            continue
        if task.get("status") == "superseded":
            continue
        if task.get("stage_verdict") == "blocked":
            blockers.append(
                {
                    "task_id": task.get("task_id"),
                    "note": task.get("stage_verdict_note", ""),
                }
            )
            blocked_gate_tasks.append(task)

    from herdr import fix_loop as fix_loop_core

    try:
        max_loops = int((gate_cfg or {}).get("max_loops", ports.FIX_LOOP_MAX))
    except (TypeError, ValueError):
        max_loops = ports.FIX_LOOP_MAX
    suggested_branch = ports.latest_branch_for_node(workflow_id, retry_node)
    # PR #110:同一 blocker 指向不同目标集合是两个不同的 verdict,
    # 显式 affected_task_ids 并入指纹(为空时与 legacy 逐字节一致)。
    affected_union = []
    for task in blocked_gate_tasks:
        for raw_id in (task.get("stage_verdict_affected_task_ids") or []):
            text = str(raw_id).strip()
            if text and text not in affected_union:
                affected_union.append(text)
    fingerprint = fix_loop_core.verdict_fingerprint(
        suggested_branch, blockers, affected_task_ids=affected_union
    )

    with ports.lock:
        _state = ports.load_stage_state()
        prior_count = _state.get(f"{workflow_id}|fixloop|{retry_node}", 0)
        try:
            prior_count = int(prior_count)
        except (TypeError, ValueError):
            prior_count = 0
        stored_fp = _state.get(f"{workflow_id}|fixloop|{retry_node}|fp")

    if ports._exhaustion_fingerprint(
        ports._fix_loop_exhaustion_episode(workflow_id, gate_node_id)
    ) == fingerprint:
        return

    if fix_loop_core.fix_loop_exhausted(
        prior_count, max_loops
    ) or fix_loop_core.is_repeat_verdict(fingerprint, stored_fp):
        reason = (
            "max_loops_exhausted"
            if fix_loop_core.fix_loop_exhausted(prior_count, max_loops)
            else "repeat_verdict"
        )
        summary = fix_loop_core.summarize_fix_loop_item(
            {
                "workflow_id": workflow_id,
                "gate_stage": gate_node_id,
                "retry_node": retry_node,
                "loop_count": prior_count,
                "max_loops": max_loops,
                "suggested_branch": suggested_branch,
                "blockers": blockers,
                "exhausted": True,
            }
        )
        ports._record_fix_loop_exhaustion(
            workflow_id, gate_node_id, retry_node, reason, summary,
            fingerprint,
        )
        with ports.lock:
            _state = ports.load_stage_state()
            _state[f"{workflow_id}|fixloop|{retry_node}|pending_redo"] = {
                "ts": ports.time.time(),
                "gate": gate_node_id,
            }
            ports.save_stage_state(_state)
        ports.coordinator_queue.put(
            {
                "kind": "fix_loop",
                "workflow_id": workflow_id,
                "gate_stage": gate_node_id,
                "retry_node": retry_node,
                "blockers": blockers,
                "invalidated": [],
                "loop_count": prior_count,
                "max_loops": max_loops,
                "suggested_branch": suggested_branch,
                "exhausted": True,
                "escalation_reason": reason,
            }
        )
        print(
            f"[FIX LOOP EXHAUSTED] "
            f"workflow={workflow_id} "
            f"gate={gate_node_id} "
            f"retry={retry_node} "
            f"reason={reason} "
            f"loops={prior_count}/{max_loops}"
        )
        return

    # PR #110:选择性返工决策先于一切作废——先持久化事实,再定向作废;
    # legacy(None) 优先继续原任务；精确候选终态保持历史替换契约。
    selective_ids, replan_plan = ports._resolve_selective_replan(
        workflow_id, gate_node_id, retry_node, workflow_cfg,
        blocked_gate_tasks, ports.load_tasks(),
    )

    invalidated = ports.invalidate_for_fix_loop(
        workflow_id, gate_node_id, workflow_cfg, retry_node=retry_node,
        selective_target_task_ids=selective_ids,
    )

    if selective_ids is not None and isinstance(
        invalidated, ports.SelectiveInvalidationResult
    ) and not invalidated.all_targets_applied:
        # P1-1:部分作废 fail-closed。门禁/下游一律未动(两阶段提交),
        # 无 latch、无计数、无通知;门禁仍 blocked,下轮 sweep 基于
        # 已持久化 Fact 重试剩余目标,绝不带着全量 roots 去等 C-r2。
        print(
            f"[SELECTIVE REPLAN DEFERRED] workflow={workflow_id} "
            f"gate={gate_node_id} retry={retry_node} "
            f"pending={','.join(invalidated.pending)} "
            "awaiting retry"
        )
        return

    if not invalidated:
        return

    if selective_ids is not None:
        # 选择性作废了 retry_node 内部谱系:清除阶段推进闩,
        # 下一轮 sweep 只补派已作废谱系；rework 目标保留原执行。
        ports.clear_stage_advance(workflow_id, retry_node)

    loop_count = ports._bump_fix_loop_count(workflow_id, retry_node)

    with ports.lock:
        _state = ports.load_stage_state()
        pending_redo = {
            "ts": ports.time.time(),
            "gate": gate_node_id,
        }
        if selective_ids is not None:
            pending_redo["mode"] = "selective"
            pending_redo["target_lineage_roots"] = list(
                (replan_plan or {}).get("target_lineage_roots") or []
            )
            # P1-1: latch 边界取 Fact 创建时间(首轮持久化时刻),
            # 而非本轮作废完成时刻。部分重试场景下 B-r2 可能在首轮
            # 部分作废后提前派发;若 latch 取第二轮时间,会把已完成
            # 的 B-r2 判为 stale 而永久等待。Fact 时间恒早于一切
            # replacement,旧完成( gated 前)又恒早于 Fact,故安全。
            try:
                _fact_ts = float((replan_plan or {}).get("created_at") or 0)
            except (TypeError, ValueError):
                _fact_ts = 0
            if _fact_ts > 0:
                pending_redo["ts"] = _fact_ts
        _state[f"{workflow_id}|fixloop|{retry_node}|pending_redo"] = pending_redo
        _state[f"{workflow_id}|fixloop|{retry_node}|fp"] = fingerprint
        ports.save_stage_state(_state)

    fix_loop_item = {
        "kind": "fix_loop",
        "workflow_id": workflow_id,
        "gate_stage": gate_node_id,
        "retry_node": retry_node,
        "blockers": blockers,
        "invalidated": list(invalidated),
        "loop_count": loop_count,
        "max_loops": gate_cfg.get("max_loops", ports.FIX_LOOP_MAX),
        "suggested_branch": suggested_branch,
    }
    if selective_ids is not None:
        # 目标上下文随通知持久化,coordinator 补投判断保持 target-aware。
        fix_loop_item["mode"] = "selective"
        fix_loop_item["target_lineage_roots"] = list(
            (replan_plan or {}).get("target_lineage_roots") or []
        )
    ports.coordinator_queue.put(fix_loop_item)

    print(
        f"[FIX LOOP QUEUED] "
        f"workflow={workflow_id} "
        f"gate={gate_node_id} "
        f"retry={retry_node} "
        f"loop={loop_count} "
        f"invalidated={len(invalidated)}"
    )


DEPENDENCIES = {'handle_fix_loop': ('FIX_LOOP_MAX', 'SelectiveInvalidationResult', '_bump_fix_loop_count',
                     '_exhaustion_fingerprint', '_fix_loop_exhaustion_episode',
                     '_handle_merged_fix_loops', '_record_fix_loop_exhaustion',
                     '_resolve_selective_replan', '_retry_node_for_gate', 'clear_stage_advance',
                     'coordinator_queue', 'invalidate_for_fix_loop', 'latest_branch_for_node',
                     'load_stage_state', 'load_tasks', 'lock', 'resolve_gate_config',
                     'save_stage_state', 'selective_replan_core', 'time')}

__all__ = tuple(DEPENDENCIES)
