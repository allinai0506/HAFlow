"""Invalidate affected task lineages and preserve eligible tasks during rework.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def _record_invalidation_note(ports, workflow_id, gate_node_id, retry_node, nodes, invalidated):
    """fix-loop 作废时写下 controller 机器证据,供 stale 计算与下游阅读。"""
    try:
        from herdr import workflow_docs as wd
        wd.append_note(
            workflow_id,
            kind="invalidation",
            title=(
                f"fix-loop 作废: gate={gate_node_id} "
                f"retry={retry_node or gate_node_id}"
            ),
            body="被作废任务: " + ", ".join(sorted(invalidated)),
            node=gate_node_id,
            source=wd.SOURCE_CONTROLLER,
            invalidates=sorted(node for node in nodes if node),
        )
    except Exception as exc:
        print(f"[FIX LOOP NOTE WARN] {exc}")

def _collect_downstream_nodes(ports, nodes_by_id, root_id):
    """root 节点自身 + 传递闭包的全部下游节点。"""
    dependents = {}

    for node in nodes_by_id.values():
        for dep in node.get("depends_on", []):
            dependents.setdefault(dep, set()).add(node["id"])

    seen = {root_id}
    frontier = [root_id]

    while frontier:
        current = frontier.pop()
        for nxt in dependents.get(current, ()):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)

    return seen

def _fix_loop_keepable(ports, task):
    """verdict=pass 的任务可否保留:需已落定或无需 Git 集成。"""
    status = task.get("status")
    if status in ("cleaned", "cleanup_ready", "integrated", "committed"):
        return True
    if status == "completed" and (task.get("integration_mode") or "none") != "git":
        return True
    return False

def _rework_retry_task(ports, task, gate_node_id, request_id=None, failure_facts=None):
    """Use the CLI ownership/CAS/transport contract; failure retains the Task."""
    target_candidate_shas = {
        task.get("candidate_sha"),
        task.get("integrated_commit"),
        (task.get("wrapup_delivery") or {}).get("integrated_sha"),
    } - {None, ""}
    if failure_facts:
        target_candidate_shas.update(
            f.get("candidate_sha") for f in failure_facts if f.get("candidate_sha")
        )
    gate_tasks = [t for t in (failure_facts if failure_facts is not None else ports.load_tasks())
                  if t.get("workflow_id") == task.get("workflow_id")
                  and t.get("status") != "superseded" and not t.get("superseded_by")
                  and (not target_candidate_shas or t.get("candidate_sha") in target_candidate_shas)
                  and (t.get("node") or t.get("stage")) in gate_node_id.split("+")
                  and t.get("stage_verdict") == "blocked"]
    if request_id is None:
        identity = sorted((t.get("task_id", ""), t.get("version", 0),
                           t.get("stage_verdict_note") or t.get("note") or "") for t in gate_tasks)
        token = ports.hashlib.sha256(ports.json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:20]
        request_id = f"fix-loop:{gate_node_id}:{task['task_id']}:{token}"
        if task.get("rework_delivery") == "pending" and str(task.get("rework_request_id") or "").startswith(f"fix-loop:{gate_node_id}:"):
            request_id = task["rework_request_id"]
    from herdr.supervisor.state import redact_text
    details = "\n".join(redact_text(f"{t.get('task_id')}: {t.get('stage_verdict_note') or t.get('note') or '读取门禁输出'}")[:2000]
                        for t in gate_tasks[:20])
    result = ports.subprocess.run(
        [ports.TASK_MANAGER, "rework", task["task_id"], "--request-id", request_id,
         "--reason", f"fix-loop: gate {gate_node_id} blocked",
         "--prompt", f"就地修复 gate {gate_node_id} 的阻断项，保留 Task 和 Pane，重新自测。\n{details}"],
        text=True, capture_output=True, timeout=30,
    )
    return result.returncode == 0, result.stderr.strip() or result.stdout.strip()

def _preserve_same_gate_task(ports, task, retry_node, gate_ids):
    return (retry_node in gate_ids
            and (task.get("node") or task.get("stage")) == retry_node
            and task.get("status") in ports.REWORKABLE_STATUSES
            and task.get("stage_verdict") != "blocked"
            and task.get("rework_delivery") != "pending")

def _rework_legacy_retry(ports, workflow_id, retry_node, gate_node_id):
    if not retry_node:
        return []
    reused = []
    for task in ports.load_tasks():
        if ports._preserve_same_gate_task(task, retry_node, gate_node_id.split("+")):
            continue
        if (task.get("workflow_id") == workflow_id
                and (task.get("node") or task.get("stage")) == retry_node
                and task.get("status") in ports.REWORKABLE_STATUSES):
            try:
                ok, error = ports._rework_retry_task(task, gate_node_id)
            except (OSError, ports.subprocess.TimeoutExpired) as exc:
                ok, error = False, str(exc)
            if not ok:
                print(f"[FIX LOOP REWORK DEFERRED] {task['task_id']}: {error}")
                return None
            reused.append(task["task_id"])
    return reused

def _invalidate_single_task(ports, task_id, gate_node_id, reuse=False):
    """finalize(如需)+supersede 单个任务,返回 (ok, error)。"""
    task = ports.get_task(task_id) or {}
    status = task.get("status")
    if reuse and status in ports.REWORKABLE_STATUSES:
        try:
            return ports._rework_retry_task(task, gate_node_id)
        except (OSError, ports.subprocess.TimeoutExpired) as exc:
            return False, str(exc)
    if status in ("completed", "cleanup_ready"):
        result = ports.subprocess.run(
            [ports.TASK_MANAGER, "finalize", task_id],
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            print(f"[FIX LOOP INVALIDATE ERROR] finalize {task_id}: {err}")
            return False, f"finalize:{err}"
        status = (ports.get_task(task_id) or {}).get("status")
    if status not in ports.FIX_LOOP_SUPERSEDEABLE:
        print(
            f"[FIX LOOP INVALIDATE SKIP] task={task_id} "
            f"status={status} cannot be superseded"
        )
        return False, f"status_not_supersedeable:{status}"
    result = ports.subprocess.run(
        [
            ports.TASK_MANAGER, "supersede", task_id,
            "--allow-pending",
            "--reason", f"fix-loop: gate {gate_node_id} blocked",
        ],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip()
        print(f"[FIX LOOP INVALIDATE ERROR] supersede {task_id}: {err}")
        return False, f"supersede:{err}"
    return True, ""

def invalidate_for_fix_loop(ports, workflow_id, gate_node_id, workflow_cfg, retry_node=None, selective_target_task_ids=None, preserve_task_ids=None, before_effect=None):
    """作废 gate 节点及其全部下游、以及 retry_node 到 gate 间全部中间节点的非 superseded 任务(fix-loop 回流前提)。

    completed/cleanup_ready 中间态先 finalize 规范化到 cleaned——
    completed→superseded 会被状态机拒绝;pending 不可作废,跳过。

    selective_target_task_ids(PR #110):非 None 时启用选择性返工——
    retry_node 节点进入扫描范围,但其中只有被点名的 Task 会被作废,
    其余实现任务零写入(状态/版本/分支/元数据全部不动);
    retry_node 之外的节点行为与 legacy 完全一致。
    None = legacy 精确原行为(retry_node 自身不进扫描范围)。

    P1-1 两阶段提交: selective 模式先作废全部目标谱系,任一目标
    未落定即整体返回(门禁/下游一律不动,无 latch、无通知),
    下轮 sweep 基于已持久化 Fact 重试剩余目标。返回
    SelectiveInvalidationResult(仍是 list),携带
    applied/pending/failed/all_targets_applied。
    """
    nodes_by_id = {
        n["id"]: n for n in workflow_cfg.get("nodes", [])
    }

    if not nodes_by_id and workflow_cfg.get("stages"):
        # normalize_workflow 总会合成 nodes;此处兜底纯 stages 配置:
        # 用 next 链重建线性依赖,保证下游作废闭包仍然成立。
        prev = None
        for stage in workflow_cfg.get("stages", []):
            stage_id = stage.get("key") or stage.get("id")
            nodes_by_id[stage_id] = {
                "id": stage_id,
                "depends_on": [prev] if prev else [],
            }
            prev = stage_id

    node_ids = set(ports._collect_downstream_nodes(nodes_by_id, gate_node_id))
    if retry_node and retry_node in nodes_by_id:
        downstream_retry = ports._collect_downstream_nodes(nodes_by_id, retry_node) - {retry_node}
        node_ids = node_ids | downstream_retry
        if selective_target_task_ids is not None:
            # 选择性返工:retry_node 自身进入扫描范围(由下方逐任务过滤点名目标)。
            node_ids.add(retry_node)

    selective_targets = None
    if selective_target_task_ids is not None:
        selective_targets = {
            str(t).strip() for t in selective_target_task_ids if str(t).strip()
        }

    # P1-1: selective 目标两阶段提交。阶段一先处理 retry_node 点名目标;
    # 任一目标 pending 即返回,不碰门禁/下游(门禁仍 blocked,下轮可重试)。
    reused = []
    if selective_targets is None:
        reused = ports._rework_legacy_retry(workflow_id, retry_node, gate_node_id)
        if reused is None:
            return []

    if selective_targets is not None and retry_node:
        ordered_targets = sorted(selective_targets)
        applied: list = []
        failed: dict = {}
        snapshot = {
            str(t.get("task_id") or ""): t
            for t in ports.load_tasks()
            if isinstance(t, dict)
            and t.get("workflow_id") == workflow_id
        }
        for target_id in ordered_targets:
            record = snapshot.get(target_id)
            if record is None:
                failed[target_id] = "target_task_unknown"
                continue
            status = record.get("status")
            if status == "superseded" or record.get("superseded_by"):
                # 崩溃前已作废:视为已落定,不重复 supersede。
                applied.append(target_id)
                continue
            ok, err = ports._invalidate_single_task(target_id, gate_node_id, reuse=True)
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
                "all_targets_applied": len(failed) == 0
                and len(applied) == len(ordered_targets),
            }
        if not outcome["all_targets_applied"]:
            print(
                f"[SELECTIVE REPLAN PARTIAL] workflow={workflow_id} "
                f"gate={gate_node_id} retry={retry_node} "
                f"applied={','.join(outcome['applied'])} "
                f"pending={','.join(outcome['pending'])} "
                f"failed={','.join(sorted(outcome['failed']))}"
            )
            return ports.SelectiveInvalidationResult(
                list(outcome["applied"]),
                applied=list(outcome["applied"]),
                pending=list(outcome["pending"]),
                failed=dict(outcome["failed"]),
            )
        # 阶段一全落定,进入阶段二:门禁/下游/中间节点(仍跳过 preserved)。
        # 目标已返工或归档，阶段二显式跳过，不能再次退役。

    supersedeable = ports.FIX_LOOP_SUPERSEDEABLE
    invalidated = list(reused)
    invalidated_nodes = set()

    for task in ports.load_tasks():
        if task.get("workflow_id") != workflow_id:
            continue
        if (
            task.get("node") not in node_ids
            and task.get("stage") not in node_ids
        ):
            continue

        status = task.get("status")
        task_id = task["task_id"]
        task_node = task.get("node") or task.get("stage")

        if selective_targets is None and ports._preserve_same_gate_task(task, retry_node, [gate_node_id]):
            continue
        if task_id in reused or status == "superseded" or (selective_targets is not None and task_id in selective_targets) or (preserve_task_ids is not None and task_id in preserve_task_ids):
            continue

        if (
            selective_targets is not None
            and task_node == retry_node
            and task_id not in selective_targets
        ):
            # PR #110 preserved:未被点名的实现任务保持完全不动。
            print(
                f"[SELECTIVE REPLAN PRESERVE] task={task_id} "
                f"node={task_node} untouched"
            )
            continue

        # 返工只重跑受影响子集:仅在门禁自身重跑(not retry_node 或 retry_node == gate_node_id)时,
        # 门禁节点内 verdict=pass 且已落定的任务保留;
        # 若跨阶段回流(如 review 回流 implementation),上游已变更,门禁与中间节点任务不可复用。
        if (
            task_node == gate_node_id
            and (not retry_node or retry_node == gate_node_id)
            and task.get("stage_verdict") == "pass"
            and ports._fix_loop_keepable(task)
        ):
            print(
                f"[FIX LOOP SUBSET KEEP] task={task_id} "
                f"verdict=pass preserved"
            )
            continue

        if status in ("completed", "cleanup_ready"):
            if before_effect:
                before_effect()
            result = ports.subprocess.run(
                [ports.TASK_MANAGER, "finalize", task_id],
                text=True,
                capture_output=True,
            )
            if result.returncode != 0:
                print(
                    f"[FIX LOOP INVALIDATE ERROR] finalize {task_id}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
                continue
            status = (ports.get_task(task_id) or {}).get("status")

        if status not in supersedeable:
            print(
                f"[FIX LOOP INVALIDATE SKIP] task={task_id} "
                f"status={status} cannot be superseded"
            )
            continue

        if before_effect:
            before_effect()
        result = ports.subprocess.run(
            [
                ports.TASK_MANAGER, "supersede", task_id,
                "--allow-pending",
                "--reason", f"fix-loop: gate {gate_node_id} blocked",
            ],
            text=True,
            capture_output=True,
        )

        if result.returncode == 0:
            invalidated.append(task_id)
            if task_node:
                invalidated_nodes.add(task_node)
        else:
            print(
                f"[FIX LOOP INVALIDATE ERROR] supersede {task_id}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

    if invalidated:
        void_nodes = invalidated_nodes | {gate_node_id}
        if retry_node:
            void_nodes.add(retry_node)
        ports._record_invalidation_note(
            workflow_id, gate_node_id, retry_node, void_nodes, invalidated
        )

    if selective_targets is not None:
        # P1-1:阶段一目标已全落定,最终返回需包含目标+门禁/下游。
        # 阶段二扫描跳过已 superseded 的目标,故在此补回。
        phase1_applied = []
        try:
            phase1_applied = list(ordered_targets)
        except NameError:
            phase1_applied = sorted(selective_targets)
        combined = list(phase1_applied) + [
            tid for tid in invalidated if tid not in phase1_applied
        ]
        return ports.SelectiveInvalidationResult(
            combined,
            applied=list(phase1_applied),
            pending=[],
            failed={},
        )

    return invalidated


DEPENDENCIES = {'_record_invalidation_note': (),
 '_collect_downstream_nodes': (),
 '_fix_loop_keepable': (),
 '_rework_retry_task': ('TASK_MANAGER', 'hashlib', 'json', 'load_tasks', 'subprocess'),
 '_preserve_same_gate_task': ('REWORKABLE_STATUSES',),
 '_rework_legacy_retry': ('REWORKABLE_STATUSES', '_preserve_same_gate_task', '_rework_retry_task',
                          'load_tasks', 'subprocess'),
 '_invalidate_single_task': ('FIX_LOOP_SUPERSEDEABLE', 'REWORKABLE_STATUSES', 'TASK_MANAGER',
                             '_rework_retry_task', 'get_task', 'subprocess'),
 'invalidate_for_fix_loop': ('FIX_LOOP_SUPERSEDEABLE', 'SelectiveInvalidationResult',
                             'TASK_MANAGER', '_collect_downstream_nodes', '_fix_loop_keepable',
                             '_invalidate_single_task', '_preserve_same_gate_task',
                             '_record_invalidation_note', '_rework_legacy_retry', 'get_task',
                             'load_tasks', 'selective_replan_core', 'subprocess')}

__all__ = tuple(DEPENDENCIES)
