"""Deliver stage notifications under the admitted dispatch lease, preserving receipt order.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def _stage_advance_next_attempts(ports, episode):
    last_attempt = float(episode.get("last_attempt_at") or 0)
    if last_attempt > 0 and (ports.time.time() - last_attempt) > ports.liveness.task_stall_after():
        return 1
    return int(episode.get("attempts") or 0) + 1

def _handle_stage_advance_item(ports, item):
    """Admit, wait, deliver and acknowledge one stage dispatch."""
    node = item.get("node")
    target_node_id = item.get("node_id") or item.get("next_stage")
    if not node:
        config = ports.workflow_config_for(item["workflow_id"])
        if config:
            node = ports.find_node(config, target_node_id)
    if node:
        item = dict(item, node=node)
        if node.get("node_type", "agent") != "agent":
            workflow_id = item["workflow_id"]
            key = f"{workflow_id}:stage_advance:{target_node_id}"
            episode = ports.attention_get(key) or {}
            workflow = ports._get_store().get_workflow(workflow_id) or {}
            attention_task = {
                "task_id": f"stage_advance:{target_node_id}",
                "workflow_id": workflow_id,
                "execution_id": workflow.get("execution_id"),
            }
            ports.attention_note(
                key,
                attention_task,
                "stage_advance",
                reason="human_decision_required",
                attempts=int(episode.get("attempts") or 0) + 1,
                detail=f"node_type={node.get('node_type')}; native executor unavailable",
            )
            ports.clear_stage_advance(workflow_id, target_node_id)
            ports.notify_attention(
                "Herdr Factory · 需要人工决策",
                attention_task,
                f"节点 {target_node_id} 类型为 {node.get('node_type')}，"
                "没有确定性执行器；请完成决策后清除 attention 以从原节点继续。",
                "human_decision_required",
            )
            print(
                f"[STAGE ADVANCE BLOCKED] workflow={workflow_id} "
                f"node={target_node_id} node_type={node.get('node_type')}: "
                "native executor unavailable; manual handling required"
            )
            return
    from herdr.coordinator_stage_admission import admit_stage_dispatch

    admission = admit_stage_dispatch(
        item,
        node,
        target_node_id,
        get_store=ports._get_store,
        coordinator_intake_enabled=ports.coordinator_intake_enabled,
        reconcile_node_dispatches=ports.reconcile_node_dispatches,
        find_node=ports.find_node,
        scheduler_join_gate_allows=ports._scheduler_join_gate_allows,
        load_tasks=ports.load_tasks,
        clear_stage_advance=ports.clear_stage_advance,
    )
    if admission is None:
        return
    item, node, dispatch_op, dispatch_owner = admission
    # Deterministic agent nodes use the persisted direct-dispatch contract for
    # roots and downstream stages alike. Coordinator intake remains the
    # compatibility fallback when the planner cannot prove a safe launch.
    if ports.try_direct_stage_advance(item):
        return

    if item.get("stage") in (None, "", "start") and ports.coordinator_intake_enabled():
        item = dict(item, intake=True)
        print(
            f"[COORDINATOR INTAKE] "
            f"workflow={item['workflow_id']} "
            f"node={target_node_id} "
            "-> dispatch via coordinator"
        )
    workflow_id = item["workflow_id"]
    coord_pane = ports.coordinator_pane_for_workflow(workflow_id)
    if not coord_pane:
        fallback_reason = item.get("direct_dispatch_fallback_reason") or "coordinator_missing"
        if dispatch_op:
            from herdr.node_dispatch_store import defer
            defer(ports._get_store().db_path, dispatch_op['id'], fallback_reason, owner=dispatch_owner)
            ports.clear_stage_advance(workflow_id, target_node_id)
        else:
            ports.clear_stage_advance(workflow_id, target_node_id)
        key = f"{workflow_id}:stage_advance:{target_node_id}"
        episode = ports.attention_get(key) or {}
        attempts = ports._stage_advance_next_attempts(episode)
        ports.attention_note(
            key,
            {"task_id": f"stage_advance:{target_node_id}", "workflow_id": workflow_id},
            "stage_advance",
            reason=fallback_reason,
            attempts=attempts,
            next_retry_at=ports.time.time() + ports.liveness.attention_retry_interval(),
            detail=f"coordinator pane missing after {fallback_reason}",
        )
        print(
            f"[STAGE ADVANCE SKIP] "
            f"no coordinator pane for workflow={workflow_id} "
            f"attempts={attempts}"
        )
        return

    stage = item["stage"]
    next_stage = item["next_stage"]
    target_node_id = item.get("node_id") or next_stage

    project_ctx = ports.project_for_workflow(
        workflow_id
    ) or {}

    project_name = project_ctx.get(
        "project_name",
        "legacy/unknown"
    )

    project_root = project_ctx.get(
        "project_root",
        ""
    )

    base_branch = project_ctx.get(
        "base_branch",
        ""
    )

    node = item.get("node")
    if not node:
        wf_cfg = ports.workflow_config_for(workflow_id)
        if wf_cfg:
            node = ports.find_node(wf_cfg, next_stage)

    policy = ports.get_stage_policy(
        next_stage
    )

    wait_started = ports.time.time()

    prompt_tasks = ports.load_tasks()

    # Candidate identity for the coordinator path. Direct dispatch binds
    # the frozen candidate through --candidate-sha/--onto; when it falls
    # back here that binding must be passed through verbatim, otherwise
    # the coordinator re-derives a revision and the same scheduler
    # decision gets two execution semantics.
    frozen_sha, frozen_branch = ports._scheduler_frozen_candidate_identity(
        workflow_id, project_ctx, (node or {}).get("depends_on") or [],
    )
    from herdr.coordinator_messages import build_stage_advance_context
    stage_prompt_context = build_stage_advance_context(
        item=item,
        stage=stage,
        project_ctx=project_ctx,
        project_name=project_name,
        project_root=project_root,
        base_branch=base_branch,
        node=node,
        next_stage=next_stage,
        policy=policy,
        tasks=prompt_tasks,
        frozen_candidate=(frozen_sha, frozen_branch),
    )

    try:
        while True:
            # Re-validate on every wait iteration: the workflow may be
            # closed or deregistered while this event sat in the queue.
            if ports.workflow_closed(workflow_id) or not ports.project_for_workflow(workflow_id):
                print(
                    f"[STAGE ADVANCE DROP] "
                    f"workflow={workflow_id} "
                    f"closed/deregistered while queued"
                )
                return

            startup_record = ports.project_for_workflow(workflow_id) or {}
            if startup_record.get("startup_ready") is False:
                print(
                    f"[STARTUP WAIT] workflow={workflow_id} "
                    "queued event held until request is ready"
                )
                ports.time.sleep(1)
                continue

            status = ports.coordinator_status(workflow_id)

            if status in ("idle", "done"):
                intake = bool(item.get("intake"))
                event_header = (
                    "HERDR_WORKFLOW_INTAKE_EVENT"
                    if intake
                    else "HERDR_STAGE_ADVANCE_EVENT"
                )
                intake_note = (
                    "这是新工作流的接单(总指挥接单机制):\n"
                    "请先完整阅读下方用户需求,理解目标、范围与约束,\n"
                    "再严格按节点职责创建第一个节点的 Task。\n\n"
                    if intake
                    else ""
                )
                docs_block = ports.shared_docs_block(
                    workflow_id,
                    next_stage,
                    related_nodes=(node or {}).get("depends_on") or [],
                    project_ctx=project_ctx,
                )
                message_context = {
                    **stage_prompt_context,
                    "event_header": event_header,
                    "intake_note": intake_note,
                    "docs_block": docs_block,
                    "task_manager": ports.TASK_MANAGER,
                    "dispatch_op": dispatch_op,
                }
                message = ports.build_stage_advance_message(message_context)
                if dispatch_op:
                    from herdr.node_dispatch_store import claim, start, transport_finished
                    import uuid
                    if dispatch_owner is None:
                        dispatch_owner = uuid.uuid4().hex
                        claimed = claim(ports._get_store().db_path, dispatch_op['id'], dispatch_owner)
                        if claimed is None:
                            return
                    if start(ports._get_store().db_path, dispatch_op['id'], dispatch_owner) is None:
                        ports.clear_stage_advance(workflow_id, target_node_id)
                        return
                try:
                    result = ports.subprocess.run(
                        ["herdr", "agent", "prompt", coord_pane, message,
                         "--wait", "--timeout", "600000"],
                        text=True, capture_output=True, timeout=600,
                    )
                except Exception as exc:
                    result = None
                    exc_err = str(exc)
                if dispatch_op:
                    receipt = transport_finished(ports._get_store().db_path, dispatch_op['id'], dispatch_owner,
                        reason='dispatch_awaiting_task' if result and result.returncode == 0 else 'dispatch_delivery_unknown')
                    ports.mark_stage_advance_notified(workflow_id, target_node_id)
                    if receipt['status'] == 'resolved':
                        print(f"[STAGE ADVANCED] workflow={workflow_id} {stage} -> {next_stage}")
                        ports.maybe_compact_coordinator(workflow_id, reason=f"stage_advance:{next_stage}")
                    else:
                        print(f"[STAGE DISPATCH AWAITING_TASK] workflow={workflow_id} "
                              f"node={target_node_id} operation={receipt['id']} "
                              f"status={receipt['status']} deadline={receipt['detail'].get('deadline_at')}")
                elif result and result.returncode == 0:
                    ports.mark_stage_advance_notified(
                        workflow_id,
                        target_node_id
                    )

                    print(
                        f"[STAGE ADVANCED] "
                        f"workflow={workflow_id} "
                        f"{stage} -> {next_stage}"
                    )

                    ports.maybe_compact_coordinator(
                        workflow_id,
                        reason=f"stage_advance:{next_stage}",
                    )
                else:
                    ports.clear_stage_advance(
                        workflow_id,
                        target_node_id
                    )
                    err_detail = (result.stderr.strip() or result.stdout.strip()) if result else (exc_err if 'exc_err' in locals() and exc_err else "prompt execution failed")
                    key = f"{workflow_id}:stage_advance:{target_node_id}"
                    episode = ports.attention_get(key) or {}
                    attempts = ports._stage_advance_next_attempts(episode)
                    ports.attention_note(
                        key,
                        {"task_id": f"stage_advance:{target_node_id}", "workflow_id": workflow_id},
                        "stage_advance",
                        reason="coordinator_stalled",
                        attempts=attempts,
                        next_retry_at=ports.time.time() + ports.liveness.attention_retry_interval(),
                        detail=f"coordinator prompt failed: {err_detail[:200]}",
                    )

                    print(
                        f"[STAGE ADVANCE ERROR] "
                        f"workflow={workflow_id}: "
                        f"{err_detail} "
                        f"attempts={attempts}"
                    )

                break

            elapsed = ports.time.time() - wait_started
            if elapsed >= ports.liveness.stage_advance_sla():
                if dispatch_op:
                    from herdr.node_dispatch_store import defer
                    defer(ports._get_store().db_path, dispatch_op['id'], 'coordinator_busy', owner=dispatch_owner)
                # 有界等待:总指挥长期不可用(僵尸 pane / 状态僵死)时,
                # 释放阶段闩并记录 attention,绝不占用 workflow 调度锁空转。
                ports.clear_stage_advance(
                    workflow_id,
                    target_node_id
                )
                key = f"{workflow_id}:stage_advance:{target_node_id}"
                episode = ports.attention_get(key) or {}
                attempts = ports._stage_advance_next_attempts(episode)
                ports.attention_note(
                    key,
                    {"task_id": f"stage_advance:{target_node_id}", "workflow_id": workflow_id},
                    "stage_advance",
                    reason="coordinator_stalled",
                    attempts=attempts,
                    next_retry_at=ports.time.time() + ports.liveness.attention_retry_interval(),
                    detail=f"coordinator status={status} waited={int(elapsed)}s",
                )
                print(
                    f"[STAGE ADVANCE STALLED] "
                    f"workflow={workflow_id} "
                    f"{stage} -> {next_stage} "
                    f"coordinator={status} waited={int(elapsed)}s "
                    f"attempts={attempts} -> deferred"
                )
                ports.notify_attention(
                    "Herdr Factory · 总指挥停滞",
                    {"task_id": f"stage_advance:{target_node_id}", "workflow_id": workflow_id},
                    f"阶段 {stage} -> {next_stage} 等待总指挥超过 {int(elapsed)}s，已延迟重试。请检查总指挥 pane 状态。",
                    "stage_advance_stalled",
                )
                return

            print(
                f"[STAGE ADVANCE WAIT] "
                f"coordinator={status} "
                f"workflow={workflow_id}"
            )

            ports.time.sleep(1)

    finally:
        pass  # task_done is called by coordinator_worker dispatcher

    return


DEPENDENCIES = {'_stage_advance_next_attempts': ('liveness', 'time'),
 '_handle_stage_advance_item': ('TASK_MANAGER', '_get_store',
                                '_scheduler_frozen_candidate_identity',
                                '_scheduler_join_gate_allows', '_stage_advance_next_attempts',
                                'attention_get', 'attention_note', 'build_stage_advance_message',
                                'clear_stage_advance', 'coordinator_intake_enabled',
                                'coordinator_pane_for_workflow', 'coordinator_status', 'find_node',
                                'get_stage_policy', 'liveness', 'load_tasks',
                                'mark_stage_advance_notified', 'maybe_compact_coordinator',
                                'notify_attention', 'project_for_workflow',
                                'reconcile_node_dispatches', 'shared_docs_block', 'subprocess',
                                'time', 'try_direct_stage_advance', 'workflow_closed',
                                'workflow_config_for')}

__all__ = tuple(DEPENDENCIES)
