"""Launch rule-planned stage tasks and reconcile their transport receipts.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def try_direct_stage_advance(ports, item):
    """常规推进会:按节点模板规则化直接派发,失败回落总指挥。

    返回 True 表示事件已被处理(含 wait),False 表示必须走总指挥原路径。
    """
    def fallback(reason):
        item["direct_dispatch_fallback_reason"] = str(reason or "direct_dispatch_unavailable")
        return False

    if ports.direct_dispatch_planner is None or not ports.direct_stage_dispatch_enabled():
        return fallback("direct_dispatch_disabled")

    node = item.get("node")
    workflow_id = item.get("workflow_id")
    ready_id = item.get("node_id") or item.get("next_stage")

    if not workflow_id or not ready_id:
        return fallback("dispatch_identity_missing")

    project_ctx = ports.project_for_workflow(workflow_id) or {}
    if project_ctx.get("startup_ready") is False:
        return fallback("workflow_startup_not_ready")

    project_root = project_ctx.get("project_root") or ""
    if not project_root:
        return fallback("workflow_configuration_missing")

    requirement = (project_ctx.get("requirement") or "").strip()
    all_tasks = ports.load_tasks() or []
    tasks = all_tasks
    execution_id = project_ctx.get("execution_id")
    if execution_id:
        tasks = [
            task for task in tasks
            if task.get("workflow_id") != workflow_id
            or not task.get("execution_id")
            or task.get("execution_id") == execution_id
        ]

    # 历史 workflow.json 的节点字段可能为空(旧模板快照),
    # 与总指挥路径一致回退 stage-policies 后再做规则化决策。
    node = ports.direct_dispatch_planner.merge_node_policy(
        node if node is not None else {"id": ready_id},
        ports.get_stage_policy(ready_id),
    )
    if not node.get("id"):
        node["id"] = ready_id

    # 上游存在 blocked 验收结论时不得自动派发(与 sweep 对称),
    # 回退总指挥裁决。依赖未知时保持原行为(fail-open)。
    dep_ids = (item.get("node") or {}).get("depends_on")
    if dep_ids is None:
        try:
            wf_cfg = ports.workflow_config_for(workflow_id) or {}
            found = ports.find_node(wf_cfg, ready_id) if wf_cfg.get("nodes") else None
            dep_ids = (found or {}).get("depends_on") or []
        except ValueError:
            dep_ids = []
    verdict_dep = ports.blocked_verdict_dep(workflow_id, {"depends_on": dep_ids})
    if verdict_dep:
        print(
            f"[DIRECT DISPATCH BLOCKED] "
            f"workflow={workflow_id} "
            f"node={ready_id} "
            f"blocked_dep={verdict_dep} "
            "-> fallback to coordinator for adjudication"
        )
        return fallback("blocked_verdict_requires_decision")

    # Scheduler v1 汇聚门禁:join 判定先于一切闩与派发。
    # - 非 join 形状:直接放行,走原语义(零行为变化);
    # - join 未满足:返回 True(事件已处理)但不写任何闩,下轮 sweep 重估;
    # - 判定异常:Fail-Closed,回落总指挥(原 fallback 语义)。
    try:
        join_node = node if isinstance(node, dict) else {"id": ready_id}
        if not ports._scheduler_join_gate_allows(workflow_id, join_node, tasks):
            return True
    except Exception as exc:
        print(
            f"[JOIN GATE ERROR] workflow={workflow_id} "
            f"node={ready_id}: {exc}"
        )
        return fallback("join_gate_evaluation_failed")

    gate_task = ports.node_is_gate(workflow_id, ready_id)

    docs_block = ports.shared_docs_block(
        workflow_id,
        ready_id,
        related_nodes=dep_ids,
        project_ctx=project_ctx,
    )

    # 门禁节点在派发时注入结论契约(状态目录 gate-verdicts/<task_id>.json +
    # 终端标记),由 try_auto_verdict 直接采纳,免除总指挥裁决回合。
    #
    # delivered_in_base=True:已 git 交付(已 integrate 进 base)的依赖,其交付物
    # 已在 base 上,onto 不应再指向本地任务分支 —— `herdr-task launch` 要求
    # `--onto` 存在于 refs/remotes/origin/,而任务分支通常从不推送。实测事故
    # (wf-project-0929-01):test 节点反复 "Onto branch not found on origin:
    # agent/opencode/feat-impl-t7-integration-gates-r2",任务在 pending 阶段
    # 即被判 router_isolation_rejected,从未真正执行。详见 lessons §110。
    candidate_branch = ports.direct_dispatch_planner.candidate_branch_for_node(
        tasks, workflow_id, ready_id, dep_ids, delivered_in_base=True
    )
    candidate_sha = ports._scheduler_expected_candidate_sha(
        workflow_id, project_root, dep_ids, candidate_branch
    )
    # P1-3: selective replacement 基线必须来自当前冻结 Candidate,
    # 禁止从普通 Task branch 猜。存在 selective 事实时 fail-closed:
    # 冻结 sha/branch 不可证明,或任一待补派谱系所属事实与当前冻结
    # 不 identical(轮换后旧事实),一律回落总指挥,不派发。
    try:
        _sel_facts = ports._all_selective_facts(workflow_id, ready_id)
    except Exception:
        _sel_facts = []
    if _sel_facts and ports.selective_replan_core is not None:
        _frozen_sha = ports._scheduler_current_frozen_candidate_sha(workflow_id)
        _frozen_branch = ports._scheduler_current_frozen_branch(workflow_id)
        if not _frozen_sha or not _frozen_branch:
            print(
                f"[SELECTIVE REPLAN BASELINE REFUSED] workflow={workflow_id} "
                f"node={ready_id} reason=frozen_unproven -> fallback to coordinator"
            )
            return fallback("frozen_candidate_unproven")
        try:
            from herdr import direct_dispatch as _dd
            _node_tasks = [
                t for t in tasks
                if isinstance(t, dict)
                and str(t.get("workflow_id") or "") == workflow_id
                and str(t.get("node") or t.get("stage") or "") == ready_id
            ]
            _pending_roots = {
                _dd.lineage_key(t.get("task_id"))[0]
                for t in _dd.lineage_redispatch_candidates(_node_tasks)
            }
        except Exception:
            _pending_roots = set()
        _need_baseline = False
        for _fact in _sel_facts:
            _roots = {str(r).strip() for r in (_fact.get("target_lineage_roots") or []) if str(r).strip()}
            if _pending_roots and not (_roots & _pending_roots):
                continue
            _need_baseline = True
            _base = ports.selective_replan_core.selective_replacement_baseline(
                _fact, _frozen_sha, _frozen_branch)
            if _base is None:
                print(
                    f"[SELECTIVE REPLAN BASELINE REFUSED] workflow={workflow_id} "
                    f"node={ready_id} reason=baseline_unproven "
                    f"fact={str(_fact.get('replan_id') or '')[:16]} -> fallback to coordinator"
                )
                return fallback("selective_replacement_baseline_unproven")
        if _need_baseline:
            candidate_branch = _frozen_branch
            candidate_sha = _frozen_sha
            print(
                f"[SELECTIVE REPLAN BASELINE] workflow={workflow_id} "
                f"node={ready_id} onto={_frozen_branch} "
                f"candidate={str(_frozen_sha)[:12]}"
            )
    # PR #110:门禁派发携带可归因 Task 清单;retry_node 补派携带
    # blocker 上下文。两者均只在策略显式开启时非 None。
    gate_inventory_block = None
    if gate_task:
        try:
            gate_inventory_block = ports._selective_gate_inventory_block(
                workflow_id, ports.workflow_config_for(workflow_id) or {}
            )
        except Exception as exc:
            print(
                f"[SELECTIVE REPLAN WARN] workflow={workflow_id} "
                f"inventory injection skipped: {exc}"
            )
    try:
        from herdr.task_resources import outstanding_launch_task_ids
        reserved_task_ids = {task.get("task_id") for task in all_tasks if task.get("task_id")}
        reserved_task_ids.update(outstanding_launch_task_ids(ports._get_store(), workflow_id))
        plan = ports.direct_dispatch_planner.plan_stage_dispatch(
            workflow_id,
            node,
            tasks,
            requirement,
            context_branch=candidate_branch,
            gate_contract=gate_task,
            docs_block=docs_block,
            candidate_sha=candidate_sha,
            gate_inventory_block=gate_inventory_block,
            redispatch_blocker_notes=ports._selective_redispatch_blocker_notes(
                workflow_id, ready_id
            ),
            reserved_task_ids=reserved_task_ids,
            project_root=project_root,
        )
    except ValueError as exc:
        print(
            f"[DIRECT DISPATCH CONTRACT INVALID] workflow={workflow_id} "
            f"node={ready_id}: {exc}"
        )
        return fallback("dispatch_contract_invalid")

    if gate_task and plan.get("mode") == "dispatch":
        # 状态目录必须先于 Agent 写入存在(结论文件落 clone 外,交付零污染)。
        try:
            ports.direct_dispatch_planner.gate_verdict_dir().mkdir(
                parents=True, exist_ok=True
            )
        except Exception as exc:
            print(f"[GATE VERDICT DIR WARN] {exc}")

    mode = plan.get("mode")

    # PR #108: a reused verifier must not be dispatched at all.
    #
    # This is defence in depth, not the primary mechanism. In the normal path
    # the reuse fact already makes the node complete, so readiness filtering
    # means a reused node never reaches here and the ledger and the scheduler
    # cannot disagree. It stays because the two are computed from different
    # reads at different times, and if they ever did disagree the safe
    # direction is unambiguous: re-running a verifier wastes work, while
    # dispatching a verifier that an immutable fact already covers is the
    # dangerous one.
    if ports._reverification_reused_node(workflow_id, ready_id):
        ports.mark_stage_advance_notified(workflow_id, ready_id)
        print(
            f"[REVERIFICATION REUSE] workflow={workflow_id} "
            f"node={ready_id} no task created; "
            f"verification carried by a reuse fact bound to the current candidate"
        )
        return True

    if mode in ("fallback", "refusal"):
        print(
            f"[DIRECT DISPATCH {mode.upper()}] "
            f"workflow={workflow_id} "
            f"node={ready_id} "
            f"reason={plan.get('reason')}"
        )
        return fallback(plan.get("reason") or "planner_refused")

    if mode == "wait":
        # 节点已有活跃任务:推进会自然由状态机完成,不再唤醒总指挥。
        ports.mark_stage_advance_notified(workflow_id, ready_id)
        print(
            f"[DIRECT DISPATCH WAIT] "
            f"workflow={workflow_id} "
            f"node={ready_id} "
            f"reason={plan.get('reason')}"
        )
        return True

    specs = plan.get("specs") or []
    if not specs:
        return fallback("dispatch_specs_missing")

    if not ports._dispatch_candidate_ready(
        project_root, project_ctx.get("base_branch"), specs, workflow_id=workflow_id
    ):
        return fallback("worker_candidate_unavailable")

    dispatch_operation_id = item.get('dispatch_operation_id')
    dispatch_owner = item.get('dispatch_owner')
    dispatch_attempt = None
    if dispatch_operation_id is not None:
        from herdr.node_dispatch_store import start
        dispatch = start(ports._get_store().db_path, dispatch_operation_id, dispatch_owner,
                         expected_task_ids=[spec['task_id'] for spec in specs])
        if dispatch is None:
            return True
        dispatch_attempt = dispatch['attempts']
    launched = []

    for spec in specs:
        # Backfill enterprise attribution from the superseded record when the
        # planner did not carry it, so redispatch never drops mandatory policy.
        try:
            _sup_id = str(spec.get("supersedes") or spec.get("redispatch_of") or "").strip()
            if _sup_id and (not spec.get("customer") or not spec.get("business_task_type")
                            or not spec.get("mandatory_experience_rules")):
                _old_task = ports._get_store().get_task(_sup_id) or {}
                if not spec.get("customer") and str(_old_task.get("customer") or "").strip():
                    spec["customer"] = str(_old_task.get("customer")).strip()
                if not spec.get("business_task_type") and str(_old_task.get("business_task_type") or "").strip():
                    spec["business_task_type"] = str(_old_task.get("business_task_type")).strip()
                if not spec.get("mandatory_experience_rules") and _old_task.get("mandatory_experience_rules"):
                    spec["mandatory_experience_rules"] = True
        except Exception:
            pass
        cmd = [
            ports.TASK_MANAGER,
            "launch",
            "--task-id", spec["task_id"],
            "--workflow-id", workflow_id,
            "--node", ready_id,
            "--source", project_root,
            "--agent", "auto",
            "--task-type", spec["task_type"],
            "--integration-mode", spec["integration_mode"],
            "--dispatch-role", spec.get("dispatch_role") or "worker",
            "--dispatch-round", str(spec.get("dispatch_round") or 1),
            "--goal", spec["goal"],
            "--prompt", spec["prompt"],
            # Workflow execution identity: sibling launches keep distinct
            # run_ids but share one execution_id for the same execution.
            "--execution-id", project_ctx.get("execution_id") or workflow_id,
        ]

        # Enterprise experience attribution from the dispatch spec (absent = no flags).
        try:
            from herdr.direct_dispatch import experience_launch_flags
            cmd += experience_launch_flags(spec)
        except Exception:
            pass

        if dispatch_operation_id is not None:
            cmd += ["--dispatch-operation-id", str(dispatch_operation_id),
                    "--dispatch-attempt", str(dispatch_attempt)]

        if spec.get("artifact_mode"):
            cmd += ["--artifact-mode", spec["artifact_mode"]]

        if spec.get("onto_branch"):
            cmd += ["--onto", spec["onto_branch"]]

        # 补派必须把谱系前驱连起来：direct_dispatch 已经算出 redispatch_of，
        # 但历史上 launch argv 没带 --supersedes，导致旧任务的 superseded_by
        # 永远为空。required_task_ids 的链式解析（scheduler.node_is_complete）
        # 因此断在第一跳，实现节点永远判不出完成，日志只剩
        # [STAGE ADVANCE WAIT] coordinator=working。
        _redispatch_of = str(spec.get("redispatch_of") or spec.get("supersedes") or "").strip()
        if _redispatch_of:
            cmd += ["--supersedes", _redispatch_of,
                    "--supersede-reason", "controller redispatch after explicit invalidation"]

        if spec.get("candidate_sha"):
            cmd += ["--candidate-sha", spec["candidate_sha"]]

        for line in spec["acceptance"]:
            cmd += ["--acceptance", line]

        try:
            result = ports.subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=ports.DIRECT_DISPATCH_LAUNCH_TIMEOUT,
            )
        except ports.subprocess.TimeoutExpired:
            print(
                f"[DIRECT DISPATCH TIMEOUT] "
                f"task={spec['task_id']} "
                f"after={ports.DIRECT_DISPATCH_LAUNCH_TIMEOUT}s"
            )
            result = None

        if result is None or result.returncode != 0:
            output = ""
            if result is not None:
                output = result.stderr.strip() or result.stdout.strip()
                print(
                    f"[DIRECT DISPATCH ERROR] "
                    f"task={spec['task_id']}: {output}"
                )
            if dispatch_operation_id is not None:
                from herdr.node_dispatch_store import direct_finished
                receipt = direct_finished(ports._get_store().db_path, dispatch_operation_id,
                    dispatch_owner, [*launched, spec['task_id']])
                refusal = ports.direct_dispatch_planner.permanent_refusal_escalation(
                    output, spec["task_id"], workflow_id, ready_id)
                if refusal and not ports.attention_get(refusal["key"]):
                    ports.attention_note(**refusal["note"])
                    print(refusal["log"])
                ports.mark_stage_advance_notified(workflow_id, ready_id)
                print(f"[STAGE DISPATCH AWAITING_TASK] workflow={workflow_id} "
                      f"node={ready_id} operation={receipt['id']} status={receipt['status']}")
                return True  # A started launch must never fall through to a second transport.
            failed_task = ports.get_task(spec["task_id"])
            if failed_task and failed_task.get("status") == "failed" and (
                failed_task.get("failure_reason") == "router_isolation_rejected"
                or "ROUTER REJECTED" in output
                or "ROUTER OPT-OUT AUDIT FAILED" in output
            ):
                key = f"{spec['task_id']}:dispatch_failure"
                if not ports.attention_get(key):
                    ports.attention_note(
                        key,
                        failed_task,
                        "dispatch_failure",
                        reason="router_isolation_rejected",
                        attempts=1,
                        detail=output[:500],
                    )
                ports._record_blocked_event(
                    failed_task,
                    "dispatch_lifecycle_failed",
                    {
                        "task_id": spec["task_id"],
                        "actionable": True,
                        "pane_dispatched": False,
                        "error": output[:500],
                    },
                )
                ports.mark_stage_advance_notified(workflow_id, ready_id)
                return True
            # Legacy events may not carry a durable node-dispatch operation.
            # Once the launch subprocess has run, timeout/non-zero cannot prove
            # absence of side effects. Persist an explicit intervention and
            # consume the event; falling through to coordinator intake could
            # send the same node through a second transport.
            if dispatch_operation_id is None:
                key = f"{workflow_id}:stage_advance:{ready_id}"
                episode = ports.attention_get(key) or {}
                ports.attention_note(
                    key,
                    {
                        "task_id": f"stage_advance:{ready_id}",
                        "workflow_id": workflow_id,
                        "execution_id": project_ctx.get("execution_id"),
                    },
                    "stage_advance",
                    reason="direct_dispatch_delivery_unknown",
                    attempts=int(episode.get("attempts") or 0) + 1,
                    detail=(output or "launch transport timed out")[:500],
                )
                ports.mark_stage_advance_notified(
                    workflow_id, ready_id, clear_attention=False
                )
                print(
                    f"[DIRECT DISPATCH UNKNOWN] workflow={workflow_id} "
                    f"node={ready_id}: manual reconciliation required"
                )
                return True
            if launched:
                print(
                    f"[DIRECT DISPATCH PARTIAL] "
                    f"launched={','.join(launched)} "
                    "-> fallback to coordinator for reconciliation"
                )
            return fallback("partial_launch_requires_reconciliation" if launched else "worker_launch_failed")

        launched.append(spec["task_id"])

    ports.mark_stage_advance_notified(workflow_id, ready_id)
    if dispatch_operation_id is not None:
        from herdr.node_dispatch_store import transport_finished
        receipt = transport_finished(ports._get_store().db_path, dispatch_operation_id,
            dispatch_owner, reason='dispatch_awaiting_task')
        if receipt['status'] != 'resolved':
            print(f"[STAGE DISPATCH AWAITING_TASK] workflow={workflow_id} "
                  f"node={ready_id} operation={receipt['id']} status={receipt['status']}")
            return True

    print(
        f"[STAGE ADVANCED DIRECT] "
        f"workflow={workflow_id} "
        f"node={ready_id} "
        f"tasks={','.join(launched)}"
    )

    # Collaboration accelerator: deterministic edges emit one HANDOFF each.
    # Best-effort only: the launched tasks already carry their own prompts,
    # so a handoff failure must never roll back the stage advance.
    try:
        for handoff in ports.maybe_dispatch_node_handoffs(
            workflow_id=workflow_id, ready_id=ready_id,
            dep_ids=dep_ids, launched=launched,
        ):
            print(f"[COLLABORATION HANDOFF] {handoff}")
    except Exception as exc:
        print(
            f"[COLLABORATION HANDOFF SKIPPED] "
            f"workflow={workflow_id} node={ready_id}: {type(exc).__name__}"
        )

    try:
        from herdr.stage_skills import is_staged_dev_flow_workflow
        if not is_staged_dev_flow_workflow(ports.workflow_config_for(workflow_id) or {}):
            ports.maybe_compact_coordinator(workflow_id, reason=f"stage_advance:{ready_id}")
    except Exception:
        ports.maybe_compact_coordinator(workflow_id, reason=f"stage_advance:{ready_id}")

    return True


DEPENDENCIES = {'try_direct_stage_advance': (
    'DIRECT_DISPATCH_LAUNCH_TIMEOUT', 'TASK_MANAGER', '_all_selective_facts', '_dispatch_candidate_ready', '_get_store', '_record_blocked_event', '_reverification_reused_node', '_scheduler_current_frozen_branch', '_scheduler_current_frozen_candidate_sha', '_scheduler_expected_candidate_sha', '_scheduler_join_gate_allows', '_selective_gate_inventory_block', '_selective_redispatch_blocker_notes', 'attention_get', 'attention_note',
    'blocked_verdict_dep', 'direct_dispatch_planner', 'direct_stage_dispatch_enabled', 'find_node', 'get_stage_policy', 'get_task', 'load_tasks', 'mark_stage_advance_notified', 'maybe_compact_coordinator', 'maybe_dispatch_node_handoffs', 'node_is_gate', 'project_for_workflow', 'selective_replan_core', 'shared_docs_block', 'subprocess', 'workflow_config_for')}

__all__ = tuple(DEPENDENCIES)
