"""Evaluate workflow node readiness and enqueue the next dispatch.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def enqueue_stage_advance(ports, task):
    workflow_id = task.get("workflow_id")
    if workflow_id:
        ports.check_workflow_stage_advance(workflow_id)

def _scheduler_resolve_candidate_and_plan(ports, workflow_id, workflow_cfg):
    """Freeze the current candidate and, on a real rotation, plan re-verification.

    Returns ``(frozen_sha, deferred)``. ``deferred`` is True only when a freeze
    was actually attempted and failed to produce an identity, which is the one
    case the caller must not latch on. Returns the frozen SHA, or "" when
    unprovable. Runs once per sweep, before readiness is computed, so that a
    rotation and the decision it implies are both visible to the same pass.

    The freeze is idempotent against the newest freeze (PR #107), so calling it
    on every sweep costs one ledger read when nothing changed. When the newest
    freeze differs from the one before it, that is a genuine A -> B rotation and
    the only place a reverification episode may begin. The plan is written
    before anything is latched or dispatched, so a crash replays from the facts
    instead of re-deriving a different decision.

    A workflow with a real project but no resolvable identity returns "" so the
    caller can defer rather than latch: verifiers would otherwise be refused at
    preflight or run against whatever happens to be checked out. A workflow with
    no project_root has no repository to resolve a candidate from at all, which
    is not a transient gap, so it keeps the legacy behaviour.
    """
    if ports.scheduler_core is None or ports.scheduler_facts_store is None:
        return "", False
    if not ports.is_node_complete(workflow_id, "implementation"):
        # Not our business yet: the candidate is frozen when implementation
        # completes, not before. Reporting a deferral here would stall a
        # workflow that has not reached the point where a candidate exists.
        return "", False
    project_ctx = ports.project_for_workflow(workflow_id) or {}
    project_root = project_ctx.get("project_root") or ""
    if not project_root:
        # No repository to resolve a candidate from at all: not a transient
        # identity gap, so keep the legacy behaviour rather than deadlocking.
        return "", False
    frozen_sha = ports._scheduler_freeze_candidate(
        workflow_id, project_root, "implementation", ["implementation"])
    if not frozen_sha:
        return "", True
    rotated_from = ports._scheduler_previous_candidate_sha(workflow_id)
    if rotated_from and rotated_from != frozen_sha:
        # The episode is the freeze event just recorded (or reused, when the
        # SHA did not change). Facts are bound to it, not to the SHA, so a
        # rollback onto an already-frozen SHA starts a new episode and cannot
        # inherit the previous round's reuse.
        freezes = []
        try:
            freezes = ports.scheduler_facts_store.list_candidate_frozen_events(
                workflow_id)
            episode_id = str(freezes[-1].get("id") or "") if freezes else ""
        except Exception:
            episode_id = ""
        if freezes and (freezes[-1].get('payload') or {}).get('source_node') == 'wrapup':
            # Post-wrapup certification always executes both independent gates.
            workflow_cfg = dict(workflow_cfg, reverification={})
        if episode_id and not ports._reverification_episode_settled(
                workflow_id, rotated_from, frozen_sha, episode_id):
            ports._reverification_plan_for_rotation(
                workflow_id, workflow_cfg, project_root, rotated_from,
                frozen_sha, episode_id)
    from herdr.wrapup_delivery import reconcile_verifiers
    if reconcile_verifiers(ports._get_store(), workflow_id):
        ports.reconcile_node_dispatches(workflow_id)
    return frozen_sha, False

def check_workflow_stage_advance(ports, workflow_id):
    if not workflow_id:
        return

    # Closed workflows must never advance: a zero-task closed workflow is
    # vacuously "complete" at every stage and would ghost-advance forever.
    if ports.workflow_closed(workflow_id):
        return

    # Discovery precedes Pane lookup and DAG readiness. Recovery execution runs
    # in the bounded background sweep; the database owns the obligation.
    from herdr import recovery_store
    from herdr.workflow_progress import assess_workflow
    record = ports._get_store().get_workflow(workflow_id)
    if record and record.get('status') == 'running':
        recovery_store.reconcile(ports._get_store().db_path, workflow_id)
        record, snapshot_config, snapshot_tasks = recovery_store.read_snapshot(ports._get_store().db_path, workflow_id)
        ports.reconcile_stage_advance_states(workflow_id, snapshot_config)
        ports.reconcile_node_dispatches(workflow_id)
        assessment = assess_workflow(record, snapshot_config, snapshot_tasks)
        if not assessment['can_advance']:
            return

    pane = ports.coordinator_pane_for_workflow(workflow_id)
    if not pane:
        from herdr.node_dispatch_store import defer
        for op in recovery_store.list_operations(ports._get_store().db_path, workflow_id):
            if op['payload'].get('kind') == 'node_dispatch':
                defer(ports._get_store().db_path, op['id'], 'coordinator_missing')
        return

    workflow_cfg = ports.workflow_config_for(workflow_id)
    if not workflow_cfg:
        return

    workflow_record = ports.project_for_workflow(workflow_id) or {}
    # New factory starts persist the requirement and keep this gate closed
    # until Deep Preflight has completed. This prevents the controller from
    # delivering a stage event before the startup request is ready.
    if workflow_record.get("startup_ready") is False:
        print(f"[STARTUP WAIT] workflow={workflow_id} preflight/request not ready")
        return

    # 已关闭(含 abandoned)的 workflow 不再参与任何推进/回流判定;
    # 否则 abandoned 残留的 blocked verdict 会被 sweep 反复回流。
    wf_st = ports._workflow_entry(workflow_id).get("status")
    if wf_st in ("completed", "paused"):
        return

    if workflow_cfg.get("nodes"):
        # Revoke stale 'notified' locks before computing ready nodes,
        # so that regressed stages can be re-triggered.
        ports.reconcile_stage_advance_states(workflow_id, workflow_cfg)

        # PR #108: resolve the candidate and its reverification plan BEFORE
        # completed_nodes is computed.
        #
        # Order is load-bearing. A reused verifier counts as complete only for
        # the candidate its fact is bound to, so a candidate rotation has to
        # happen first — otherwise a node satisfied by a now-stale A -> B fact
        # still looks complete in this sweep, no Task is created for C, and the
        # verifier would be skipped for C without ever having run. Running the
        # freeze first makes the rotation visible to the same sweep that acts
        # on it.
        deferred = False
        try:
            _frozen_sha, deferred = ports._scheduler_resolve_candidate_and_plan(
                workflow_id, workflow_cfg)
        except Exception as exc:
            print(f"[SCHEDULER SWEEP WARN] workflow={workflow_id}: {exc}")
            deferred = True
        # Reset AFTER the plan, not before: the plan writes the reuse facts, and
        # the episode check above reads the ledger first. Clearing afterwards
        # guarantees the memo is built from a ledger that already contains this
        # sweep's writes, so a fact recorded a moment ago is visible to the
        # readiness computation in the same pass.
        ports._reset_reverification_memo(workflow_id)

        completed_nodes = {
            n["id"]
            for n in workflow_cfg.get("nodes", [])
            if ports.is_node_complete(workflow_id, n["id"])
        }

        from herdr.node_dispatch_store import operation_for_node
        for root in workflow_cfg.get('nodes', []):
            if root.get('depends_on'):
                continue
            dispatch = operation_for_node(ports._get_store().db_path, workflow_id, root['id'])
            if dispatch and dispatch['status'] not in ('resolved', 'superseded'):
                completed_nodes.discard(root['id'])

        # PR #110:选择性返工只作废 retry_node 内被点名的谱系,其余任务
        # 仍 completed-like——is_node_complete 会把节点误判为完成,
        # 替代任务(-rN)永远没有补派窗口。以持久化 selective fact 为准:
        # 目标谱系仍有补派候选时节点视为未完成,并自愈式清除阶段推进闩
        # (覆盖 invalidate 与 clear 之间的崩溃窗口)。未声明策略的流程
        # 整段跳过,legacy 行为零变化;任务表每轮最多加载一次。
        if ports.selective_replan_core is not None and (
            ports.selective_replan_core.policy_from_workflow(workflow_cfg)
        ):
            _await_tasks = None
            for _n in workflow_cfg.get("nodes", []):
                _nid = _n.get("id")
                if _await_tasks is None:
                    _await_tasks = ports.load_tasks()
                if not ports._selective_replan_awaiting_redispatch(
                    workflow_id, _nid, _await_tasks
                ):
                    continue
                completed_nodes.discard(_nid)
                ports.clear_stage_advance(workflow_id, _nid)
                print(
                    f"[SELECTIVE REPLAN AWAIT] workflow={workflow_id} "
                    f"node={_nid} target lineage pending redispatch"
                )

        if ports.is_workflow_completed(workflow_cfg, completed_nodes):
            # reopen 闩在先:重开现场(sweep 每 2s 一次)不得刷
            # [WORKFLOW COMPLETE] 噪音,更不得触发 close。
            if ports._workflow_entry(workflow_id).get("suppress_auto_close"):
                return

            if ports._workflow_entry(workflow_id).get("status") == "completed":
                return

            # 交付终态门禁:任一门禁节点 verdict=blocked 时不得关闭,
            # 回流 fix-loop(由 handle_fix_loop 原子作废并派发事件)。
            nodes_by_id = {
                n["id"]: n for n in workflow_cfg.get("nodes", [])
            }
            blocked_gates = []

            for node_id in sorted(completed_nodes):
                gate_cfg = ports.resolve_gate_config(
                    nodes_by_id.get(node_id), node_id
                )
                if (
                    gate_cfg
                    and ports.gate_verdict(workflow_id, node_id) == "blocked"
                ):
                    blocked_gates.append((node_id, gate_cfg))

            if blocked_gates:
                for gate_node_id, gate_cfg in blocked_gates:
                    ports.handle_fix_loop(
                        workflow_id, gate_node_id, gate_cfg, workflow_cfg
                    )
                return

            # 完成日志闩:close 可能失败或需要多轮,日志只允许出现一次,
            # 否则 sweep 会把 [WORKFLOW COMPLETE] 刷成日志风暴。
            if workflow_id not in ports._workflow_complete_logged:
                ports._workflow_complete_logged.add(workflow_id)
                print(
                    f"[WORKFLOW COMPLETE] "
                    f"workflow={workflow_id}"
                )

            ports.maybe_close_completed_workflow(workflow_id)
            return

        ready_nodes = ports.get_ready_nodes(workflow_cfg, completed_nodes)
        for ready_node in ready_nodes:
            latched_dep = next(
                (
                    dep
                    for dep in (ready_node.get("depends_on") or [])
                    if ports._fix_loop_latch_blocks(workflow_id, dep)
                ),
                None,
            )
            if latched_dep:
                latch_key = f"{workflow_id}:{latched_dep}"
                if latch_key not in ports._fix_latch_logged:
                    ports._fix_latch_logged.add(latch_key)
                    print(
                        f"[FIX LOOP LATCH] "
                        f"workflow={workflow_id} "
                        f"{ready_node['id']} waits for redo of {latched_dep}"
                    )
                continue
            blocked_dep = ports.blocked_gate_dependency(
                workflow_id, ready_node, workflow_cfg
            )
            if blocked_dep:
                gate_node_id, gate_cfg = blocked_dep
                ports.handle_fix_loop(
                    workflow_id, gate_node_id, gate_cfg, workflow_cfg
                )
                continue

            ready_id = ready_node["id"]
            verdict_dep = ports.blocked_verdict_dep(workflow_id, ready_node)
            if verdict_dep:
                # 有门禁结论无门禁配置的依赖:不销毁下游,只暂停自动推进
                # 并 funnel 给总指挥裁决;作废过期结论后自动恢复。
                verdict_key = f"{workflow_id}:upstream_blocked:{ready_id}"
                if not ports.attention_blocks_retry(verdict_key):
                    episode = ports.attention_get(verdict_key) or {}
                    attempts = int(episode.get("attempts") or 0) + 1
                    ports.attention_note(
                        verdict_key,
                        {"task_id": f"stage_advance:{ready_id}",
                         "workflow_id": workflow_id},
                        "stage_advance",
                        reason="upstream_blocked",
                        attempts=attempts,
                        next_retry_at=ports.time.time() + ports.liveness.attention_retry_interval(),
                        detail=f"blocked_dep={verdict_dep}",
                    )
                    ports.notify_attention(
                        "Herdr Factory · 上游门禁阻断",
                        {"task_id": f"stage_advance:{ready_id}",
                         "workflow_id": workflow_id},
                        f"节点 {ready_id} 的上游 {verdict_dep} 存在 blocked 验收结论，"
                        "已暂停自动推进等待裁决（作废过期结论或返工后自动恢复）。",
                        "upstream_blocked",
                    )
                    print(
                        f"[STAGE ADVANCE BLOCKED] "
                        f"workflow={workflow_id} "
                        f"{ready_id} blocked_dep={verdict_dep} "
                        "-> awaiting adjudication"
                    )
                continue
            if ports.attention_blocks_retry(f"{workflow_id}:stage_advance:{ready_id}"):
                continue

            deps = ready_node.get("depends_on", [])
            source_stage = deps[-1] if deps else "start"

            # Scheduler v1:join 判定先于 queued 闩。
            # join 未满足 -> continue(无闩),下轮 sweep 重估,修正证据后自动恢复。
            # implementation 完成 -> 冻结候选 SHA(+补 delivery note)供下游绑定。
            try:
                if not ports._scheduler_join_gate_allows(
                    workflow_id, ready_node, ports.load_tasks()
                ):
                    continue
            except Exception as exc:
                print(
                    f"[SCHEDULER SWEEP WARN] workflow={workflow_id} "
                    f"node={ready_id}: {exc}"
                )
                continue

            # No candidate identity on a workflow that has a real project and a
            # completed implementation -> do not latch and do not dispatch this
            # node. Verifiers would either be refused at preflight (no identity
            # to prove) or run against whatever revision happens to be checked
            # out, and the stage latch would suppress the natural retry once the
            # branch/SHA becomes resolvable. Skipping the latch keeps the node
            # re-evaluated on the next sweep.
            #
            # Scoped to the ready-node loop. Verified against `3a84659`: the
            # freeze failure there also skipped every ready node in that pass,
            # so this is behaviour-preserving rather than an improvement — the
            # point of the change is only that it must not run *before*
            # `is_workflow_completed`, where it would stop a finished workflow
            # from ever closing.
            if deferred:
                print(
                    f"[SCHEDULER FREEZE DEFERRED] workflow={workflow_id} "
                    f"node={ready_id} no candidate identity resolvable; "
                    "stage not latched, dispatch deferred to next sweep"
                )
                continue

            dispatch_claim = None
            if not deps or ports._get_store().get_workflow(workflow_id):
                from herdr.node_dispatch_store import operation_for_node, claim
                op = operation_for_node(ports._get_store().db_path, workflow_id, ready_id)
                if (deps or ports.coordinator_intake_enabled()) and not op and ports._get_store().get_workflow(workflow_id):
                    continue
                if op and not deps and not ports.coordinator_intake_enabled() and op['status'] not in ('resolved', 'superseded'):
                    continue
                if op and (deps or ports.coordinator_intake_enabled()):
                    import uuid
                    dispatch_claim = claim(ports._get_store().db_path, op['id'], uuid.uuid4().hex)
                    if dispatch_claim is None:
                        continue
                    # SQLite reservation owns queued delivery; a lost JSON latch cannot suppress recovery.
                    ports.clear_stage_advance(workflow_id, ready_id)

            if not ports.mark_stage_advance_queued(workflow_id, ready_id):
                continue

            ports.coordinator_queue.put(
                {
                    "kind": "stage_advance",
                    "workflow_id": workflow_id,
                    "stage": source_stage,
                    "node_id": ready_id,
                    "next_stage": ready_id,
                    "stage_label": ready_node.get("label", ready_id),
                    "node": ready_node,
                    "dispatch_operation_id": dispatch_claim['id'] if dispatch_claim else None,
                    "dispatch_owner": dispatch_claim['owner'] if dispatch_claim else None,
                }
            )

            print(
                f"[STAGE ADVANCE QUEUED] "
                f"workflow={workflow_id} "
                f"{source_stage} -> {ready_id}"
            )
        return

    # Fallback to legacy single-step stage advance
    for stage in workflow_cfg.get("stages", []):
        stage_key = stage.get("key") or stage.get("id")
        if not ports.is_node_complete(workflow_id, stage_key):
            continue

        next_stage = stage.get("next")
        if not next_stage:
            continue

        gate_cfg = ports.resolve_gate_config(None, stage_key)
        if gate_cfg and ports.gate_verdict(workflow_id, stage_key) == "blocked":
            ports.handle_fix_loop(workflow_id, stage_key, gate_cfg, workflow_cfg)
            continue

        if ports.attention_blocks_retry(f"{workflow_id}:stage_advance:{next_stage}"):
            continue

        if ports._fix_loop_latch_blocks(workflow_id, stage_key):
            latch_key = f"{workflow_id}:{stage_key}"
            if latch_key not in ports._fix_latch_logged:
                ports._fix_latch_logged.add(latch_key)
                print(
                    f"[FIX LOOP LATCH] "
                    f"workflow={workflow_id} "
                    f"{next_stage} waits for redo of {stage_key}"
                )
            continue

        if not ports.mark_stage_advance_queued(workflow_id, next_stage):
            continue

        ports.coordinator_queue.put(
            {
                "kind": "stage_advance",
                "workflow_id": workflow_id,
                "stage": stage_key,
                "next_stage": next_stage,
                "stage_label": stage.get("label", stage_key),
            }
        )

        print(
            f"[STAGE ADVANCE QUEUED] "
            f"workflow={workflow_id} "
            f"{stage_key} -> {next_stage}"
        )


DEPENDENCIES = {'enqueue_stage_advance': ('check_workflow_stage_advance',),
 '_scheduler_resolve_candidate_and_plan': ('_get_store', 'reconcile_node_dispatches', '_reverification_episode_settled',
                                           '_reverification_plan_for_rotation',
                                           '_scheduler_freeze_candidate',
                                           '_scheduler_previous_candidate_sha', 'is_node_complete',
                                           'project_for_workflow', 'scheduler_core',
                                           'scheduler_facts_store'),
 'check_workflow_stage_advance': ('_fix_latch_logged', '_fix_loop_latch_blocks', '_get_store',
                                  '_reset_reverification_memo', '_scheduler_join_gate_allows',
                                  '_scheduler_resolve_candidate_and_plan',
                                  '_selective_replan_awaiting_redispatch',
                                  '_workflow_complete_logged', '_workflow_entry',
                                  'attention_blocks_retry', 'attention_get', 'attention_note',
                                  'blocked_gate_dependency', 'blocked_verdict_dep',
                                  'clear_stage_advance', 'coordinator_intake_enabled',
                                  'coordinator_pane_for_workflow', 'coordinator_queue',
                                  'gate_verdict', 'get_ready_nodes', 'handle_fix_loop',
                                  'is_node_complete', 'is_workflow_completed', 'liveness',
                                  'load_tasks', 'mark_stage_advance_queued',
                                  'maybe_close_completed_workflow', 'notify_attention',
                                  'project_for_workflow', 'reconcile_node_dispatches',
                                  'reconcile_stage_advance_states', 'resolve_gate_config',
                                  'selective_replan_core', 'time', 'workflow_closed',
                                  'workflow_config_for')}

__all__ = tuple(DEPENDENCIES)
