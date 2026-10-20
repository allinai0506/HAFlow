"""Read candidate-scoped verifier facts and plan reuse after candidate rotation.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def _reset_reverification_memo(ports, workflow_id):
    ports._REVERIFICATION_MEMO.pop(workflow_id, None)

def _reverification_context_and_facts(ports, workflow_id):
    """Resolve (expected_sha, policy_identity, branch_node_ids, reuse_facts) once.

    Returned as a single memoised tuple because they all come from the same two
    reads, and because the join gate and the readiness computation must agree on
    them exactly.
    """
    if ports.scheduler_facts_store is None or ports.reverification_mod is None:
        return "", "", [], []
    memo = ports._REVERIFICATION_MEMO.get(workflow_id)
    if memo is not None:
        return memo
    try:
        cfg = ports.workflow_config_for(workflow_id) or {}
        freezes = ports.scheduler_facts_store.list_candidate_frozen_events(workflow_id)
        expected = str(
            (freezes[-1].get("payload") or {}).get("candidate_sha") or ""
        ) if freezes else ""
        # The episode is the freeze event, not the SHA. A rollback re-freezes a
        # SHA that was already frozen; keying reuse on the SHA would let the
        # earlier round's fact resurrect and mark a candidate covered that was
        # never verified.
        episode = str(freezes[-1].get("id") or "") if freezes else ""
        policy_fp = str(ports.reverification_mod.policy_identity(
            ports.reverification_mod.policy_from_workflow(cfg)) or "")
    except Exception as exc:
        print(f"[REVERIFICATION POLICY WARN] workflow={workflow_id}: {exc}")
        return "", "", "", [], []
    if not expected or not policy_fp or not episode:
        ports._REVERIFICATION_MEMO[workflow_id] = ("", "", "", [], [])
        return ports._REVERIFICATION_MEMO[workflow_id]
    try:
        node_ids = ports.scheduler_core.verifier_branch_node_ids(cfg)
    except Exception as exc:
        print(f"[REVERIFICATION BRANCHES WARN] workflow={workflow_id}: {exc}")
        return "", "", "", [], []
    found = []
    for node_id in node_ids:
        try:
            fact = ports.scheduler_facts_store.find_reuse_fact(
                workflow_id, node_id, expected, policy_identity=policy_fp,
                episode_id=episode)
        except Exception as exc:
            print(f"[REVERIFICATION FACT WARN] workflow={workflow_id} "
                  f"node={node_id}: {exc}")
            return "", "", "", [], []
        if fact:
            found.append(fact)
    if len(ports._REVERIFICATION_MEMO) >= ports._REVERIFICATION_MEMO_LIMIT:
        ports._REVERIFICATION_MEMO.clear()
    ports._REVERIFICATION_MEMO[workflow_id] = (
        expected, policy_fp, episode, node_ids, found)
    return ports._REVERIFICATION_MEMO[workflow_id]

def _reverification_gate_facts(ports, workflow_id):
    """当前候选冻结 + 当前策略下的 reuse 事实(只读)。

    事实按 (verifier, to_candidate_sha, policy_identity, 候选冻结 episode)
    精确绑定,所以这里读到的每一条都只对**当前这一轮**候选有效:候选再次
    轮换、回滚到曾冻结过的 SHA、或策略被收窄/删除,旧事实都失效。
    读取失败一律返回空列表——门禁侧 fail-closed,绝不会因为「查不到」而把
    复用当成通过。
    """
    return ports._reverification_context_and_facts(workflow_id)[4]

def _reverification_effective(ports, workflow_id, node_id):
    """The verification that currently counts for one node.

    Delegates to the scheduler core rather than re-deriving it here. The core
    owns the precedence rule (fresh evidence outranks a reuse fact, and a reuse
    fact only applies when the branch has no live task), and the join gate
    applies exactly the same rule. Two implementations of one load-bearing
    predicate is how the ledger ends up claiming a verification the gate
    refuses, so there is deliberately only one.
    """
    if ports.scheduler_core is None or ports.reverification_mod is None:
        return {"status": "none", "source": "none"}
    try:
        context = ports._reverification_context_and_facts(workflow_id)
        return ports.scheduler_core.resolve_effective_verification(
            ports.load_tasks(), workflow_id, node_id, context[0],
            context[4])
    except Exception as exc:
        print(f"[REVERIFICATION EFFECTIVE WARN] workflow={workflow_id} "
              f"node={node_id}: {exc}")
        return {"status": "none", "source": "none"}

def _reverification_satisfies_node(ports, workflow_id, node_id):
    """该 verifier 节点是否已由 reuse 事实满足(仅对当前候选与当前策略)。

    复用事实只对汇聚门禁真正会追问的分支有意义,而 ``_reverification_gate_facts``
    本身就只按分支节点取事实,因此这里不需要再单独做一次 DAG 查询。

    ``scheduler_core`` 的存在检查不能省:本函数在 ``is_node_complete`` 的热
    路径上,而那个函数在 #107 里只依赖任务状态、在 scheduler 缺失时不会崩。
    直接访问 ``scheduler_core.EFFECTIVE_REUSE`` 会在 scheduler 导入失败时抛
    AttributeError,把一个可选组件的缺失升级成控制面崩溃。
    """
    if not node_id or ports.scheduler_core is None:
        return False
    return ports._reverification_effective(workflow_id, node_id).get("source") == \
        ports.scheduler_core.EFFECTIVE_REUSE

def _reverification_episode_settled(ports, workflow_id, from_sha, to_sha, episode_id):
    """Whether every verifier of this episode already has a recorded decision.

    A rotation stays "the current rotation" until a *third* candidate is frozen,
    so without this check the whole plan — canonicalise, ancestor check, diff,
    and a full task scan — would be recomputed on every 2-second sweep, forever,
    for a decision that is already immutable on disk. The facts are the record;
    re-deriving them cannot change the answer, it can only cost subprocesses.

    Scoped by the freeze episode as well as the SHA pair, so a repeated
    (from, to) in a later round is treated as a fresh episode and re-planned.
    """
    if ports.scheduler_facts_store is None or ports.scheduler_core is None:
        return False
    try:
        expected = ports._reverification_context_and_facts(workflow_id)[3]
    except Exception:
        return False
    if not expected:
        return False
    try:
        recorded = ports.scheduler_facts_store.list_reverification_decisions(
            workflow_id, limit=ports.scheduler_facts_store.REUSE_LOOKUP_SCAN_LIMIT)
    except Exception:
        return False
    seen = set()
    for event in recorded:
        payload = event.get("payload") or {}
        if (str(payload.get("from_candidate_sha") or "")
                != str(from_sha or "")):
            continue
        if str(payload.get("to_candidate_sha") or "") != str(to_sha or ""):
            continue
        if str(payload.get("candidate_frozen_event_id") or "") != str(
                episode_id or ""):
            continue
        verifier = str(payload.get("verifier") or "")
        if verifier:
            seen.add(verifier)
    return set(expected).issubset(seen)

def _reverification_source_verifications(ports, workflow_id, from_sha):
    """Candidate ``from_sha`` 上可作为复用来源的既有验证(供举证)。

    三条硬性过滤,任一不满足即不作为来源:

    1. 必须带 ``verified_candidate_sha``(claim / baseline 都不是完成证据,§14);
    2. 必须**精确绑定** ``from_sha``——不是「在附近某个版本跑过」;
    3. 必须是真实跑出来的任务(reuse 不创建 Task,因此任何 Task 都是 fresh)。

    第 1 条刻意**不**走 ``scheduler.extract_task_verified_sha``:该函数为兼容
    pre-Scheduler workflow,会对没有 candidate claim 的任务回退到
    ``baseline_commit``。复用来源的要求比那个回退严格得多——它断言的是
    「这个 verifier 真的验证了 A」,而 launch baseline 只能说明「它从 A 出发」,
    正是 #107 引入完成证据要消灭的那个说法(中途 pull/rebase 的 Agent 仍会
    报告 baseline=A,却验证了别的树)。所以这里读字面字段。

    刻意**不**排除 superseded 任务:fix-loop 返工时会把上一轮的 test 任务
    作废,但「作废」只表示它不再是当前结论,不表示抹掉它验证过 A 这一事实。
    排除掉它们会让所有真实返工都无法复用,等于功能永远不生效。
    """
    if ports.scheduler_core is None:
        return []
    wanted = str(from_sha or "").strip()
    if not wanted:
        return []
    sources = []
    for task in ports.load_tasks():
        if not isinstance(task, dict):
            continue
        if task.get("workflow_id") != workflow_id:
            continue
        # A verdict is only meaningful on a task that actually reached one. A
        # task still running cannot have proved anything, and a stale
        # stage_verdict left on a non-terminal record must not be read as a
        # PASS. Superseded counts: fix-loop supersedes the previous round's
        # verifier, and excluding those would make reuse unreachable on every
        # real rework.
        if str(task.get("status") or "") not in ports._SETTLED_VERIFICATION_STATUSES:
            continue
        node_id = str(task.get("node") or task.get("stage") or "")
        if not node_id:
            continue
        verified = str(task.get("verified_candidate_sha") or "").strip()
        if not verified:
            continue
        if not ports.scheduler_core.shas_identical(verified, wanted):
            continue
        try:
            updated = float(task.get("updated_at") or 0)
        except (TypeError, ValueError):
            updated = 0.0
        sources.append({
            "verifier": node_id,
            "task_id": str(task.get("task_id") or ""),
            "verdict": str(task.get("stage_verdict") or "").strip().lower(),
            "candidate_sha": ports.scheduler_core.extract_task_candidate_claim(task),
            "verified_candidate_sha": verified,
            "source": ports.reverification_mod.SOURCE_FRESH,
            "updated_at": updated,
        })
    return sources

def _reverification_plan_for_rotation(ports, workflow_id, workflow_cfg, project_root,
                                      from_sha, to_sha, episode_id):
    """候选轮换时构建并落盘一次重新验证计划(best-effort,失败即全部 RERUN)。

    返回 ``(plan, reused_node_ids)``。任何异常都退化为「没有复用」,即所有
    verifier 照常重跑:漏跑一个 verifier 远好过跳过一个本该跑的 verifier。
    """
    empty = ({}, set())
    if (
        ports.reverification_mod is None
        or ports.scheduler_facts_store is None
        or ports.scheduler_core is None
    ):
        return empty
    if not project_root or not from_sha or not to_sha or from_sha == to_sha:
        return empty
    if not episode_id:
        # Without the freeze that authorises it, a decision could not be bound to
        # a round, so nothing may be recorded as covered.
        print(f"[REVERIFICATION PLAN SKIPPED] workflow={workflow_id} "
              "no candidate freeze episode; treating as full re-verification")
        return empty
    try:
        policy = ports.reverification_mod.policy_from_workflow(workflow_cfg)
        entries, diff_reason = ports.reverification_mod.collect_candidate_changes(
            project_root, from_sha, to_sha)
        sources = ports._reverification_source_verifications(workflow_id, from_sha)
        plan = ports.reverification_mod.build_reverification_plan(
            workflow_id, from_sha, to_sha, entries, sources,
            policy, diff_reason=diff_reason,
            verifiers=ports.scheduler_core.verifier_branch_node_ids(workflow_cfg),
            episode_id=episode_id,
        )
    except Exception as exc:
        print(f"[REVERIFICATION PLAN WARN] workflow={workflow_id}: {exc}")
        return empty

    reused = set()
    for decision in plan["verifiers"].values():
        node_id = str(decision.get("verifier") or "")
        try:
            recorded = ports.scheduler_facts_store.record_reverification_decision(
                workflow_id, decision)
        except Exception as exc:
            print(f"[REVERIFICATION WRITE WARN] workflow={workflow_id} "
                  f"node={node_id}: {exc}")
            continue
        if decision.get("decision") == ports.reverification_mod.DECISION_REUSE:
            if recorded.get("status") == "rejected":
                print(
                    f"[REVERIFICATION REJECTED] workflow={workflow_id} "
                    f"node={node_id} reason="
                    f"{recorded.get('reason')}"
                )
                continue
            reused.add(node_id)
        print(
            f"[REVERIFICATION {str(decision.get('decision') or '').upper()}] "
            f"workflow={workflow_id} node={node_id} "
            f"{from_sha[:8]} -> {to_sha[:8]} "
            f"reason={decision.get('reason')} "
            f"changed={','.join(decision.get('changed_paths') or []) or '-'}"
        )
    return plan, reused

def _reverification_reused_node(ports, workflow_id, node_id):
    """该节点本次是否应由 reuse 事实满足、从而**不创建 Task**。

    复用是调度决策,不是 Agent 决策(§19):控制器不会先创建 test(B) Task
    再让 Agent 自己决定不跑。
    """
    if not node_id:
        return False
    return ports._reverification_satisfies_node(workflow_id, node_id)


DEPENDENCIES = {'_reset_reverification_memo': ('_REVERIFICATION_MEMO',),
 '_reverification_context_and_facts': ('_REVERIFICATION_MEMO', '_REVERIFICATION_MEMO_LIMIT',
                                       'reverification_mod', 'scheduler_core',
                                       'scheduler_facts_store', 'workflow_config_for'),
 '_reverification_gate_facts': ('_reverification_context_and_facts',),
 '_reverification_effective': ('_reverification_context_and_facts', 'load_tasks',
                               'reverification_mod', 'scheduler_core'),
 '_reverification_satisfies_node': ('_reverification_effective', 'scheduler_core'),
 '_reverification_episode_settled': ('_reverification_context_and_facts', 'scheduler_core',
                                     'scheduler_facts_store'),
 '_reverification_source_verifications': ('_SETTLED_VERIFICATION_STATUSES', 'load_tasks',
                                          'reverification_mod', 'scheduler_core'),
 '_reverification_plan_for_rotation': ('_reverification_source_verifications', 'reverification_mod',
                                       'scheduler_core', 'scheduler_facts_store'),
 '_reverification_reused_node': ('_reverification_satisfies_node',)}

__all__ = tuple(DEPENDENCIES)
