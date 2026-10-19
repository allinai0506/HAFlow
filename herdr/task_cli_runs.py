"""Run-scoped evaluation and replay CLI handlers."""
import json
import sys
from pathlib import Path

def cmd_metrics(args, *, get_store):
    """Read-only, run-scoped Harness Metrics summary."""
    from herdr.metrics import get_run_metrics

    try:
        store = get_store()
        metrics = get_run_metrics(args.run_id, db_path=getattr(store, "db_path", None))
    except ValueError as exc:
        print(f"[METRICS] {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"[METRICS] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)

    if getattr(args, "json", False):
        print(json.dumps(metrics.to_mapping(), ensure_ascii=False, indent=2, sort_keys=True))
        return

    print(f"Harness Metrics run={metrics.run_id}")
    print(f"Status: {metrics.final_status or '-'} | completed={metrics.task_completed}")
    print(f"Wall time: {metrics.wall_time_seconds if metrics.wall_time_seconds is not None else '-'}s")
    print(f"Trajectory: {metrics.trajectory_events} | Observations: {metrics.observations_created} ({metrics.observation_bytes} bytes)")
    print(f"Findings: {metrics.findings_created} | Context packs: {metrics.context_packs_created}")
    print(f"Verification: {metrics.verification_passed}/{metrics.verification_total} passed, {metrics.verification_failed} failed")
    print(f"Model requests: {metrics.model_requests if metrics.model_requests is not None else 'unsupported'}")

def cmd_working_context(args, *, get_store):
    """Read-only WorkingContext loader for agents holding a context ref."""
    from herdr.context_compiler import get_working_context

    try:
        store = get_store()
        context = get_working_context(
            args.context_id, db_path=getattr(store, "db_path", None)
        )
    except ValueError as exc:
        print(f"[WORKING-CONTEXT] {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"[WORKING-CONTEXT] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
    if context is None:
        print(f"[WORKING-CONTEXT] not found: {args.context_id}", file=sys.stderr)
        sys.exit(2)

    print(json.dumps(context.to_mapping(), ensure_ascii=False, indent=2, sort_keys=True))

def cmd_eval(args, *, get_store):
    """Evaluate one run; --record persists the fact via eval_store."""
    from herdr.eval_engine import evaluate_run, record_run_eval

    try:
        store = get_store()
        db_path = getattr(store, "db_path", None)
        if getattr(args, "record", False):
            stored = record_run_eval(
                args.run_id, revision=getattr(args, "revision", None),
                db_path=db_path,
            )
            payload = stored
            label = "EVAL_RECORDED"
        else:
            payload = evaluate_run(args.run_id, db_path=db_path)
            label = "EVAL"
    except ValueError as exc:
        print(f"[EVAL] {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:  # noqa: BLE001 -- CLI boundary reports and exits nonzero
        print(f"[EVAL] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)

    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    warnings = payload.get("warnings") or []
    print(f"[{label}] run={payload.get('run_id')} "
          f"final_status={payload.get('final_status')} "
          f"requirements_satisfied={payload.get('requirements_satisfied')} "
          f"verification_passed={payload.get('verification_passed')} "
          f"human_intervention_count={payload.get('human_intervention_count')}")
    if warnings:
        print(f"Warnings: {', '.join(str(w) for w in warnings)}")

def cmd_replay(args, *, get_store):
    """Replay a historical run; --dry-run plans without any write."""
    import json as _json

    from herdr.replay_engine import replay_run

    definition = None
    if getattr(args, "definition", None):
        try:
            definition = _json.loads(args.definition)
        except ValueError as exc:
            print(f"[REPLAY] invalid --definition JSON: {exc}", file=sys.stderr)
            sys.exit(2)
    elif getattr(args, "definition_file", None):
        try:
            definition = _json.loads(
                Path(args.definition_file).expanduser().read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            print(f"[REPLAY] cannot read --definition-file: {exc}", file=sys.stderr)
            sys.exit(2)

    policy = None
    if getattr(args, "policy", None):
        try:
            policy = _json.loads(args.policy)
        except ValueError as exc:
            print(f"[REPLAY] invalid --policy JSON: {exc}", file=sys.stderr)
            sys.exit(2)
        if not isinstance(policy, dict):
            print("[REPLAY] --policy must be a JSON object", file=sys.stderr)
            sys.exit(2)
    elif getattr(args, "policy_file", None):
        try:
            policy = _json.loads(
                Path(args.policy_file).expanduser().read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            print(f"[REPLAY] cannot read --policy-file: {exc}", file=sys.stderr)
            sys.exit(2)
        if not isinstance(policy, dict):
            print("[REPLAY] --policy-file must contain a JSON object", file=sys.stderr)
            sys.exit(2)

    try:
        store = get_store()
        out = replay_run(
            args.source_run,
            replay_run_id=getattr(args, "replay_run", None),
            workflow_id=getattr(args, "workflow_id", None),
            task_id=getattr(args, "task_id", None),
            definition=definition,
            policy=policy,
            agent=getattr(args, "agent", None),
            goal=getattr(args, "goal", None),
            prompt=getattr(args, "prompt", None),
            source=getattr(args, "source", None) or "replay",
            # Replay V1 always preflights and launches; flags remain accepted
            # only for command-line compatibility.
            launch=True,
            run_preflight=True,
            dry_run=bool(getattr(args, "dry_run", False)),
            db_path=getattr(store, "db_path", None),
        )
    except ValueError as exc:
        print(f"[REPLAY] {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:  # noqa: BLE001 -- CLI boundary reports and exits nonzero
        print(f"[REPLAY] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)

    if getattr(args, "json", False):
        print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        return
    mode = "DRY_RUN" if out.get("dry_run") else "REPLAYED"
    print(f"[REPLAY_{mode}] {out['spec']['source_run_id']} -> {out['spec']['replay_run_id']}")
    print(f"Workflow: {out.get('workflow_id')} | Snapshot: {out.get('snapshot') or '-'}")
    if out.get("launch_argv"):
        print(f"Launch: {' '.join(str(p) for p in out['launch_argv'])}")
    if out.get("preflight_argv"):
        print(f"Preflight: {' '.join(str(p) for p in out['preflight_argv'])}")
    if out.get("warnings"):
        print(f"Warnings: {', '.join(str(w) for w in out['warnings'])}")

def cmd_eval_compare(args, *, get_store):
    """Compare two recorded eval rows factually."""
    from herdr.eval_engine import compare_evals
    from herdr.eval_store import get_eval_result, get_latest_eval_result

    try:
        store = get_store()
        db_path = getattr(store, "db_path", None)
        if getattr(args, "before_revision", None) is not None:
            before = get_eval_result(
                args.before_run, int(args.before_revision), db_path=db_path)
        else:
            before = get_latest_eval_result(args.before_run, db_path=db_path)
        if getattr(args, "after_revision", None) is not None:
            after = get_eval_result(
                args.after_run, int(args.after_revision), db_path=db_path)
        else:
            after = get_latest_eval_result(args.after_run, db_path=db_path)
        diff = compare_evals(before, after)
    except ValueError as exc:
        print(f"[EVAL_COMPARE] {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:  # noqa: BLE001 -- CLI boundary reports and exits nonzero
        print(f"[EVAL_COMPARE] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)

    if getattr(args, "json", False):
        print(json.dumps(diff, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("[EVAL_COMPARE] factual fields:")
    for field in ("requirements_satisfied", "verification_passed",
                  "human_intervention_count", "final_status"):
        print(f"{field}: {diff['before'][field]} -> {diff['after'][field]}")
