"""Read selective-replan facts and identify missing lineage redispatch coverage.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def _latest_selective_fact(ports, workflow_id, node_id):
    """该 retry_node 最新的 selective 事实;无/异常/模块缺失 → None。

    legacy_fallback 事实由核心层跳过(它们不带 targets,不隐含待补派)。
    """
    if ports.selective_replan_core is None or ports.scheduler_facts_store is None:
        return None
    try:
        fact = ports.scheduler_facts_store.latest_selective_replan_for_node(
            workflow_id, node_id
        )
    except Exception as exc:
        print(
            f"[SELECTIVE REPLAN WARN] workflow={workflow_id} "
            f"node={node_id}: {exc}"
        )
        return None
    return fact if isinstance(fact, dict) else None

def _all_selective_facts(ports, workflow_id, node_id):
    """该 retry_node 全部 selective 事实(chronological);异常 → []。

    P1-2: test ∥ review 同轮 blocked 会留下两条单门禁事实。
    只读最新一条会丢目标,必须取并集。
    """
    if ports.selective_replan_core is None or ports.scheduler_facts_store is None:
        return []
    try:
        if hasattr(ports.scheduler_facts_store, "list_selective_replan_for_node"):
            facts = ports.scheduler_facts_store.list_selective_replan_for_node(
                workflow_id, node_id
            )
        else:
            facts = []
            for event in ports.scheduler_facts_store.list_selective_replan_decisions(
                workflow_id,
                limit=ports.scheduler_facts_store.REPLAN_LOOKUP_SCAN_LIMIT,
            ):
                payload = event.get("payload") or {}
                if str(payload.get("mode") or "") != "selective":
                    continue
                if str(payload.get("retry_node") or "") != str(node_id or ""):
                    continue
                facts.append({
                    "event_id": event.get("id"),
                    "timestamp": event.get("timestamp"),
                    **payload,
                })
    except Exception as exc:
        print(
            f"[SELECTIVE REPLAN WARN] workflow={workflow_id} "
            f"node={node_id}: {exc}"
        )
        return []
    return [f for f in (facts or []) if isinstance(f, dict)]

def _selective_replan_awaiting_redispatch(ports, workflow_id, node_id, tasks=None):
    """PR #110:该节点是否存在「已作废、待补派」的 selective 目标谱系。

    以持久化 selective_replan_decision 事实为唯一依据(无事实 → False)。
    P1-2:同一节点多条 selective 事实取目标并集( test→[B] + review→[C]
    = [B,C] );已落定谱系无补派候选,自然不再触发,历史滞留无害。
    判定刻意复用补派管线自己的谓词 `lineage_redispatch_candidates`:
    「该谱系当前有补派候选」≡「它能被补派」。两处若各写一套定义,一旦
    分歧(典型:任务被标 superseded_by 指向一个从未创建的替代者),
    这里会永远判 True、节点永远不完成,而补派管线永远给不出候选——
    工作流永久卡死。用同一个谓词,这种状态自动被视为「无事可等」。
    """
    facts = ports._all_selective_facts(workflow_id, node_id)
    if not facts:
        return False
    roots = {
        str(r).strip()
        for fact in facts
        for r in (fact.get("target_lineage_roots") or [])
        if str(r).strip()
    }
    if not roots:
        return False
    if tasks is None:
        tasks = ports.load_tasks()
    node_tasks = [
        task
        for task in tasks or []
        if isinstance(task, dict)
        and str(task.get("workflow_id") or "") == workflow_id
        and str(task.get("node") or task.get("stage") or "") == node_id
    ]
    try:
        from herdr import direct_dispatch as direct_dispatch_core

        pending = direct_dispatch_core.lineage_redispatch_candidates(node_tasks)
    except Exception as exc:
        print(
            f"[SELECTIVE REPLAN WARN] workflow={workflow_id} "
            f"node={node_id}: redispatch candidates unavailable: {exc}"
        )
        return False
    return any(
        direct_dispatch_core.lineage_key(task.get("task_id"))[0] in roots
        for task in pending
    )

def _selective_gate_inventory_block(ports, workflow_id, workflow_cfg):
    """门禁派发时注入的可归因 Task 清单(PR #110);未开启策略 → None。

    清单只含 retry_node 当前谱系头(历史已作废任务绝不出现在清单里),
    由纯核心构建+渲染;这里只做 I/O 装配。
    """
    if ports.selective_replan_core is None:
        return None
    policy = ports.selective_replan_core.policy_from_workflow(workflow_cfg)
    if policy is None:
        return None
    inventory = ports.selective_replan_core.build_task_inventory(
        ports.load_tasks(), workflow_id=workflow_id, retry_node=policy["retry_node"]
    )
    return ports.selective_replan_core.render_inventory_block(inventory)

def _selective_redispatch_blocker_notes(ports, workflow_id, node_id):
    """补派 replacement 时注入的 blocker 上下文(PR #110);无事实 → None。

    返回 {被作废 task_id: 上下文文本};上下文只进 dispatch prompt,
    旧 Task 本身(状态/元数据)不被改写。
    P1-2:多条 selective 事实按目标取并集,每条目标沿用其所属事实的
    gate note( test→B 用 test 的 blocker, review→C 用 review 的)。
    """
    facts = ports._all_selective_facts(workflow_id, node_id)
    if not facts:
        return None
    notes = {}
    for fact in facts:
        try:
            render = ports.selective_replan_core.render_replacement_blocker_note
        except Exception:
            continue
        for raw_id in fact.get("target_task_ids") or []:
            task_id = str(raw_id).strip()
            if task_id and task_id not in notes:
                notes[task_id] = render(fact, task_id)
    return notes or None

def _blocked_gate_tasks_for(ports, workflow_id, gate_node_id):
    """该门禁节点当前存活的 blocked 结论任务(快照用,不含 superseded)。"""
    matched = []
    for task in ports.load_tasks():
        if task.get("workflow_id") != workflow_id:
            continue
        if gate_node_id not in (task.get("node"), task.get("stage")):
            continue
        if task.get("status") == "superseded":
            continue
        if task.get("stage_verdict") == "blocked":
            matched.append(task)
    return matched

def _retry_node_for_gate(ports, workflow_cfg, gate_cfg, gate_node_id):
    """解析 fix-loop 的 retry_node(含历史 fallback,与 handle 一致)。"""
    retry_node = (gate_cfg or {}).get("retry_node", "implementation")
    nodes_by_id = {n["id"]: n for n in (workflow_cfg or {}).get("nodes", [])}
    if nodes_by_id and retry_node not in nodes_by_id:
        gate_deps = nodes_by_id.get(gate_node_id, {}).get("depends_on", [])
        fallback = next(
            (dep for dep in gate_deps if dep in nodes_by_id),
            None,
        ) or next(iter(nodes_by_id))
        retry_node = fallback
    return retry_node


DEPENDENCIES = {'_latest_selective_fact': ('scheduler_facts_store', 'selective_replan_core'),
 '_all_selective_facts': ('scheduler_facts_store', 'selective_replan_core'),
 '_selective_replan_awaiting_redispatch': ('_all_selective_facts', 'load_tasks'),
 '_selective_gate_inventory_block': ('load_tasks', 'selective_replan_core'),
 '_selective_redispatch_blocker_notes': ('_all_selective_facts', 'selective_replan_core'),
 '_blocked_gate_tasks_for': ('load_tasks',),
 '_retry_node_for_gate': ()}

__all__ = tuple(DEPENDENCIES)
