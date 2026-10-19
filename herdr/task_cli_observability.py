"""Read-only observation and routing evaluation CLI handlers."""
import json
import sys
import time
from herdr.agent_router import DEFAULT_ALLOWED

KNOWN_AGENTS = [agent for agent in DEFAULT_ALLOWED if agent]

def cmd_observe(args, *, get_store):
    """Trajectory Observer 只读入口: 观察一个 Run 并输出结构化 Finding。

    旁路能力:只读 Trajectory/Runtime/日志,只写 trajectory_findings 分析表,
    绝不改变 Task/Workflow/Runtime 状态;没有任何证据时输出空 findings。
    """
    try:
        from herdr.observer import harness as observer_harness
        from herdr.trajectory import run_id_for_task
    except Exception as exc:
        print(f"[OBSERVE] observer unavailable: {exc}", file=sys.stderr)
        sys.exit(1)

    store = get_store()
    task = None
    run_id = args.run_id

    if args.task_id:
        task = store.get_task(args.task_id)
        if not task:
            print(f"[OBSERVE] task not found: {args.task_id}", file=sys.stderr)
            sys.exit(1)
        if not run_id:
            run_id = run_id_for_task(task)
    # --run-id alone is enough: observe_run resolves the task (runtime/logs)
    # from the run's own trajectory facts.

    if not run_id:
        print("[OBSERVE] --run-id or --task-id is required", file=sys.stderr)
        sys.exit(2)

    findings = observer_harness.observe_run(
        run_id,
        task=task,
        store=store,
        use_model=not getattr(args, "no_model", False),
    )
    payload = [finding.to_mapping() for finding in findings]
    if getattr(args, "json", False):
        print(json.dumps({"run_id": run_id, "findings": payload}, ensure_ascii=False, indent=2))
        return
    if not findings:
        print(f"[OBSERVE] run={run_id} findings=0 (no evidence-backed problem detected)")
        return
    for finding in findings:
        print(
            f"[{finding.severity.upper()}] {finding.finding_type} "
            f"node={finding.node or '-'} action={finding.recommended_action} "
            f"confidence={finding.confidence:.2f}"
        )
        print(f"  {finding.summary}")
        for item in finding.evidence[:3]:
            print(f"  evidence: {json.dumps(item, ensure_ascii=False)}")

def cmd_compact(args, *, get_store):
    """Create a bounded ContextPack without mutating execution state."""
    from herdr.context_compact import compact_run

    store = get_store()
    task = None
    if getattr(args, "task_id", None):
        task = store.get_task(args.task_id)
        if not task:
            print(f"[COMPACT] task not found: {args.task_id}", file=sys.stderr)
            sys.exit(1)
    try:
        provider = None
        if not getattr(args, "no_model", False):
            try:
                from herdr.observer.harness import get_provider
                provider = get_provider()
            except Exception as exc:
                print(f"[COMPACT] provider unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        pack = compact_run(args.run_id, task=task, store=store, provider=provider)
    except Exception as exc:
        print(f"[COMPACT] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
    if getattr(args, "json", False):
        print(json.dumps(pack.to_mapping(), ensure_ascii=False, indent=2))
        return
    print(f"ContextPack {pack.context_id} run={pack.run_id} sequence={pack.source_event_sequence}")
    print(f"Goal: {pack.goal or '-'}")
    print(f"State: {json.dumps(pack.current_state, ensure_ascii=False)}")
    print(f"Verified facts: {len(pack.verified_facts)} | Findings: {len(pack.important_findings)}")
    print(f"Evidence refs: {len(pack.evidence_refs)} | Artifact refs: {len(pack.artifact_refs)}")

def cmd_route_shadow(args, *, get_store):
    """Read-only Adaptive Router v1 shadow ranking (never dispatches)."""
    from herdr import adaptive_router

    try:
        store = get_store()
        db_path = getattr(store, "db_path", None)
        candidates = [
            part.strip()
            for part in str(getattr(args, "agents", "") or "").split(",")
            if part.strip()
        ]
        if not candidates:
            try:
                from herdr.agent_router import _candidate_order, ensure_pool_for_project
                from herdr.projects import project_for_workflow
                project = project_for_workflow(args.workflow_id)
                pool = ensure_pool_for_project(project.get("project_id"))
                candidates = _candidate_order(pool, args.node, args.task_type)
            except Exception:
                candidates = []
        if not candidates:
            try:
                from herdr.agent_router import DEFAULT_ALLOWED
            except Exception:
                DEFAULT_ALLOWED = list(KNOWN_AGENTS)
            candidates = list(DEFAULT_ALLOWED)
        rankings = adaptive_router.rank_candidates(
            candidates,
            db_path=db_path,
            node=args.node,
            task_type=args.task_type,
            cutoff=time.time(),
            active_loads={},
            reserved_loads={},
        )
    except ValueError as exc:
        print(f"[ROUTE-SHADOW] {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:  # noqa: BLE001 -- CLI boundary reports and exits nonzero
        print(f"[ROUTE-SHADOW] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)

    if getattr(args, "json", False):
        print(json.dumps(
            {"node": args.node, "task_type": args.task_type,
             "rankings": rankings,
             "algorithm_version": adaptive_router.ALGORITHM_VERSION},
            ensure_ascii=False, indent=2))
        return
    print(adaptive_router.explain_ranking(rankings))

def _parse_shadow_since(raw):
    """Parse --since: Nd / Nh suffix or an epoch timestamp."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    import re as _re
    match = _re.fullmatch(r"(\d+(?:\.\d+)?)\s*([dh])", text.lower())
    if not match:
        raise ValueError(
            f"invalid --since {raw!r}: use Nd/Nh (e.g. 7d) or epoch seconds")
    amount = float(match.group(1))
    window = amount * 86400.0 if match.group(2) == "d" else amount * 3600.0
    return time.time() - window

def cmd_shadow_eval(args):
    """Read-only Adaptive Router shadow evaluation (never mutates)."""
    from herdr import shadow_evaluation, state_db

    # Validate every CLI input BEFORE touching the database: invalid args
    # exit 2 without creating/migrating anything (a StateStore must never
    # be opened here: its init runs schema init on open).
    try:
        since = _parse_shadow_since(getattr(args, "since", None))
    except ValueError as exc:
        print(f"[SHADOW-EVAL] {exc}", file=sys.stderr)
        sys.exit(2)
    try:
        limit = int(getattr(args, "limit", None) or 1000)
    except (TypeError, ValueError):
        print("[SHADOW-EVAL] --limit must be an int",
              file=sys.stderr)
        sys.exit(2)
    if limit <= 0:
        print("[SHADOW-EVAL] --limit must be a positive int",
              file=sys.stderr)
        sys.exit(2)

    # Pure path resolution: no StateStore, no init_db, no migration. The
    # DB must already exist; read-only mode never creates it.
    db_path = state_db.resolve_state_db_path()
    if not db_path.is_file():
        print(f"[SHADOW-EVAL] state database not found: {db_path}",
              file=sys.stderr)
        sys.exit(1)

    try:
        bundle = shadow_evaluation.run_shadow_evaluation(
            db_path,
            filters=shadow_evaluation.ShadowEvaluationFilters(
                node=getattr(args, "node", None),
                task_type=getattr(args, "task_type", None),
                agent=getattr(args, "agent", None),
                since=since,
                limit=limit,
            ),
        )
    except SystemExit:
        raise
    except state_db.ReadonlySchemaError as exc:
        print(f"[SHADOW-EVAL] {exc}", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(f"[SHADOW-EVAL] {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:  # noqa: BLE001 -- CLI boundary reports and exits nonzero
        print(f"[SHADOW-EVAL] failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        sys.exit(1)

    report = bundle["report"]
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2,
                         sort_keys=True))
        return
    print(shadow_evaluation.render_shadow_report(report))

def cmd_canary_eval(args):
    """Read-only Adaptive Router canary evaluation (never mutates).

    Compares observed outcomes of canary-diverted (adaptive arm) vs
    non-diverted (legacy arm) executions per bucket. Same validation
    discipline as shadow-eval: every CLI input is checked BEFORE any
    database touch, and the database is only ever opened read-only.
    """
    from herdr import canary_evaluation, state_db

    try:
        since = _parse_shadow_since(getattr(args, "since", None))
    except ValueError as exc:
        print(f"[CANARY-EVAL] {exc}", file=sys.stderr)
        sys.exit(2)
    try:
        limit = int(getattr(args, "limit", None) or 1000)
    except (TypeError, ValueError):
        print("[CANARY-EVAL] --limit must be an int",
              file=sys.stderr)
        sys.exit(2)
    if limit <= 0:
        print("[CANARY-EVAL] --limit must be a positive int",
              file=sys.stderr)
        sys.exit(2)

    # Pure path resolution: no StateStore, no init_db, no migration.
    db_path = state_db.resolve_state_db_path()
    if not db_path.is_file():
        print(f"[CANARY-EVAL] state database not found: {db_path}",
              file=sys.stderr)
        sys.exit(1)

    try:
        bundle = canary_evaluation.run_canary_evaluation(
            db_path,
            filters=canary_evaluation.CanaryEvaluationFilters(
                node=getattr(args, "node", None),
                task_type=getattr(args, "task_type", None),
                agent=getattr(args, "agent", None),
                since=since,
                limit=limit,
            ),
        )
    except state_db.ReadonlySchemaError as exc:
        print(f"[CANARY-EVAL] {exc}", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(f"[CANARY-EVAL] {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:  # noqa: BLE001 -- CLI boundary reports and exits nonzero
        print(f"[CANARY-EVAL] failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        sys.exit(1)

    report = bundle["report"]
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2,
                         sort_keys=True))
        return
    print(canary_evaluation.render_canary_report(report))
