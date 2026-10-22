"""Resolve gate-attributed selective replan decisions against current task evidence.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def _resolve_selective_replan(ports, workflow_id, gate_node_id, retry_node,
                              workflow_cfg, blocked_gate_tasks, tasks):
    """PR #110:把 blocked 门禁的显式 affected_task_ids 解析为选择性返工目标。

    返回 (target_task_ids or None, plan or None):
    - target_task_ids is None → legacy fix-loop 精确原行为(策略未开启、
      模块缺失、或任一环节无法证明时均如此);
    - 非 None → 选择性事实已持久化,只允许作废这些目标。

    Fail-Closed 顺序:先构建 plan,再持久化 immutable fact,
    最后才允许调用方作废——「无持久化 selective 事实,无选择性作废」。
    崩溃重放时 record 返回 exists,以已存储 payload 为权威(不重算)。
    """
    if ports.selective_replan_core is None or ports.scheduler_facts_store is None:
        return None, None
    policy = ports.selective_replan_core.policy_from_workflow(workflow_cfg)
    if policy is None:
        # legacy workflow(未声明 selective_replan 策略):完全无感知。
        return None, None
    if policy["retry_node"] != str(retry_node or ""):
        if retry_node == "wrapup":
            policy = dict(policy, retry_node="wrapup")
        else:
            # 目标按策略声明的 retry_node 校验,作废却按门禁解析出的 retry_node 执行:
            # 两者不一致时「保留」过滤器永不命中,会把未被点名的任务一并作废,
            # 同时留下一条自称 selective 的事实。证明不了同一节点即整体回退。
            print(
                f"[SELECTIVE REPLAN FALLBACK] workflow={workflow_id} "
                f"gate={gate_node_id} reason=retry_node_policy_mismatch "
                f"policy={policy['retry_node']} gate_config={retry_node}"
            )
            return None, None

    # P1-1 Resolve-once-persist-once-read-many:已有当前 gate episode 的
    # Fact 时直接作为 authority 逐个 reconcile,不再根据已变化的 Task
    # 状态重新决定 targets(否则 B 已 supersede 会导致整体 fallback,
    # C 永远得不到 supersede)。
    if len(blocked_gate_tasks) == 1 and isinstance(
        blocked_gate_tasks[0], dict
    ):
        try:
            _gate_task = blocked_gate_tasks[0]
            _frozen_sha = ports._scheduler_current_frozen_candidate_sha(workflow_id)
            _policy_fp = ports.selective_replan_core.policy_identity(policy)
            try:
                _gate_version = int(_gate_task.get("version"))
            except (TypeError, ValueError):
                _gate_version = None
            if _gate_version is not None and _frozen_sha and _policy_fp:
                try:
                    if hasattr(
                        ports.scheduler_facts_store,
                        "list_selective_replan_for_node",
                    ):
                        _prior_facts = (
                            ports.scheduler_facts_store.list_selective_replan_for_node(
                                workflow_id, retry_node
                            )
                        )
                    else:
                        _prior_facts = (
                            ports.scheduler_facts_store.list_selective_replan_decisions(
                                workflow_id,
                                limit=ports.scheduler_facts_store.REPLAN_LOOKUP_SCAN_LIMIT,
                            )
                        )
                except Exception:
                    _prior_facts = []
                _reused = ports.selective_replan_core.find_reusable_selective_fact(
                    _prior_facts,
                    retry_node=str(retry_node or ""),
                    gate_task_id=str(_gate_task.get("task_id") or ""),
                    gate_task_version=_gate_version,
                    frozen_candidate_sha=_frozen_sha,
                    policy_identity=_policy_fp,
                )
                if isinstance(_reused, dict) and _reused.get("mode") == (
                    ports.selective_replan_core.MODE_SELECTIVE
                ):
                    print(
                        f"[SELECTIVE REPLAN REUSE] workflow={workflow_id} "
                        f"gate={gate_node_id} retry={retry_node} "
                        f"targets={','.join(_reused.get('target_task_ids') or [])} "
                        f"replan_id={_reused.get('replan_id')}"
                    )
                    return list(_reused.get("target_task_ids") or []), _reused
        except Exception as exc:
            print(
                f"[SELECTIVE REPLAN WARN] workflow={workflow_id} "
                f"gate={gate_node_id} reuse lookup skipped: {exc}"
            )

    requested_raw = None
    if len(blocked_gate_tasks) == 1:
        requested_raw = blocked_gate_tasks[0].get(
            "stage_verdict_affected_task_ids"
        )
    plan = ports.selective_replan_core.build_selective_replan_plan(
        workflow_id=workflow_id,
        gate_node=gate_node_id,
        gate_tasks=blocked_gate_tasks,
        policy=policy,
        requested_raw=requested_raw,
        tasks=tasks,
        frozen_candidate_sha=ports._scheduler_current_frozen_candidate_sha(
            workflow_id
        ),
        supersedeable_statuses=ports.FIX_LOOP_SUPERSEDEABLE,
    )
    # P1-3:把决策时刻冻结候选的可证明分支绑进事实,供 replacement
    # 派发时 fail-closed 校验基线(不进 identity,不影响 exists/mismatch)。
    try:
        plan["source_candidate_branch"] = ports._scheduler_current_frozen_branch(
            workflow_id
        )
    except Exception:
        pass

    authority = plan
    if plan.get("replan_id"):
        try:
            record = ports.scheduler_facts_store.record_selective_replan_decision(
                workflow_id, plan
            )
        except Exception as exc:
            record = {"status": "error", "error": str(exc)}
        record_status = str((record or {}).get("status") or "error")
        if record_status == "exists":
            # 幂等重放:已落库的事实是权威,绝不用本次重算结果顶替。
            stored = ports.scheduler_facts_store.find_selective_replan_decision(
                workflow_id, plan["replan_id"]
            )
            if not isinstance(stored, dict):
                print(
                    f"[SELECTIVE REPLAN FALLBACK] workflow={workflow_id} "
                    f"gate={gate_node_id} fact_status=exists "
                    "reason=stored_fact_unreadable"
                )
                return None, plan
            authority = stored
        elif record_status != "created":
            # 持久化被拒/冲突/异常:无事实 → 绝不选择性作废。
            # record_event_if_absent 用 reason 报告拒绝(如
            # identity_content_mismatch),只有异常分支才带 error。
            reject_reason = (
                (record or {}).get("error")
                or (record or {}).get("reason")
                or plan.get("reason")
            )
            print(
                f"[SELECTIVE REPLAN FALLBACK] workflow={workflow_id} "
                f"gate={gate_node_id} fact_status={record_status} "
                f"reason={reject_reason}"
            )
            return None, plan
    else:
        # 无法构成 episode 身份(门禁任务/版本不可证明):事实不可持久化。
        print(
            f"[SELECTIVE REPLAN FALLBACK] workflow={workflow_id} "
            f"gate={gate_node_id} reason={plan.get('reason')} "
            "fact=unpersistable"
        )
        return None, plan

    if authority.get("mode") != ports.selective_replan_core.MODE_SELECTIVE:
        print(
            f"[SELECTIVE REPLAN FALLBACK] workflow={workflow_id} "
            f"gate={gate_node_id} reason={authority.get('reason')}"
        )
        return None, authority

    print(
        f"[SELECTIVE REPLAN] workflow={workflow_id} "
        f"gate={gate_node_id} retry={retry_node} "
        f"targets={','.join(authority.get('target_task_ids') or [])} "
        f"preserved={','.join(authority.get('preserved_task_ids') or [])} "
        f"replan_id={authority.get('replan_id')}"
    )
    return list(authority.get("target_task_ids") or []), authority


DEPENDENCIES = {'_resolve_selective_replan': ('FIX_LOOP_SUPERSEDEABLE', '_scheduler_current_frozen_branch',
                               '_scheduler_current_frozen_candidate_sha', 'scheduler_facts_store',
                               'selective_replan_core')}

__all__ = tuple(DEPENDENCIES)
