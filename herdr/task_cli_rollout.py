"""CLI handlers for controlled rollout and reverification reports."""
import json
import sys
from herdr import canary_router, rollout_policy, state_db

def _rollout_db_path():
    """The single resolver for every rollout read and write.

    Read and write MUST agree on which database holds rollout state, or
    ``rollout set`` could succeed against one database while
    ``rollout status`` reports an empty state from another. This is the
    same pure resolver the read-only evaluation commands use
    (``state_db.resolve_state_db_path``); the write path deliberately
    does not go back through ``_get_store()`` to guess a second time.
    """
    from herdr import state_db
    return state_db.resolve_state_db_path()

def _rollout_stage_label(value):
    """Render a staged percentage; 0/off renders as ``off``."""
    return "off" if not value else f"{value}%"

def _rollout_bucket_args(args):
    """Validate + normalize the shared bucket identity flags."""
    from herdr import rollout_policy
    agent = (getattr(args, "agent", None) or "").strip()
    node = (getattr(args, "node", None) or "").strip()
    task_type = getattr(args, "task_type", None)
    if task_type is None:
        task_type = ""
    try:
        bucket = rollout_policy.normalize_bucket(agent, node, task_type)
    except ValueError as exc:
        print(f"[ROLLOUT] {exc}", file=sys.stderr)
        sys.exit(2)
    return bucket

def _rollout_config_fallback(bucket):
    """Canary config percentage this bucket runs at with no staged row.

    ``None`` means the bucket has no canary traffic to suppress (config
    disabled, or bucket not whitelisted). The policy layer needs the
    real number so ``rollout off`` writes an explicit off row instead of
    reporting a no-op while the config keeps diverting.
    """
    from herdr import canary_router
    return canary_router.config_percentage_for(
        bucket["agent"], bucket["node"], bucket["task_type"])

def _rollout_change(args, new_percentage, reason, source, *, verb):
    """Shared set/off write path: validate, write atomically, report."""
    from herdr import rollout_policy
    bucket = _rollout_bucket_args(args)
    if not reason:
        print(f"[ROLLOUT] --reason is required for {verb}", file=sys.stderr)
        sys.exit(2)
    try:
        db_path = _rollout_db_path()
        record = rollout_policy.set_stage(
            db_path, agent=bucket["agent"], node=bucket["node"],
            task_type=bucket["task_type"], new_percentage=new_percentage,
            reason=reason, source=source,
            config_fallback=_rollout_config_fallback(bucket))
    except ValueError as exc:
        # Invalid stage/transition or a concurrent-writer conflict: the
        # bucket is unchanged and nothing was persisted.
        print(f"[ROLLOUT] {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:  # noqa: BLE001 -- CLI boundary
        print(f"[ROLLOUT] failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        sys.exit(1)
    if getattr(args, "json", False):
        print(json.dumps(record, ensure_ascii=False, indent=2,
                         sort_keys=True))
        return
    key = (f"{bucket['agent']}/{bucket['node']}/"
           f"{bucket['task_type'] or '(no type)'}")
    if record.get("action") == "noop":
        print(f"[ROLLOUT] {key} already at "
              f"{_rollout_stage_label(record.get('new_percentage'))}; "
              "no change")
        return
    print(f"[ROLLOUT] {key} "
          f"{_rollout_stage_label(record.get('previous_percentage'))} -> "
          f"{_rollout_stage_label(record.get('new_percentage'))} "
          f"action={record.get('action')} audit_id={record.get('id')}")

def cmd_reverification(args):
    """Selective Reverification audit entry (status/history) — read-only.

    Read-only by construction: it only ever reads recorded decision facts. It
    answers "why did this verifier not re-run?", which cannot be answered from
    tasks alone because a reuse leaves no task behind.
    """
    action = getattr(args, "reverification_command", None)
    if action == "status":
        _cmd_reverification_status(args)
    elif action == "history":
        _cmd_reverification_history(args)
    else:
        print("[REVERIFICATION] unknown action; use status|history",
              file=sys.stderr)
        sys.exit(2)

def _reverification_rows(workflow_id, limit=None):
    """Reverification decision facts for one workflow (read-only, bounded)."""
    from herdr import scheduler_facts, state_db
    db_path = state_db.resolve_state_db_path()
    if not db_path.is_file():
        return []
    try:
        return scheduler_facts.list_reverification_decisions(
            workflow_id, db_path=db_path, limit=limit)
    except Exception as exc:  # noqa: BLE001 -- read-only boundary
        print(f"[REVERIFICATION] decisions unavailable: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)

def _reverification_current_decisions(workflow_id):
    """Newest decision per verifier, restricted to the current candidate.

    Scoped to the frozen candidate because that is the question `status` claims
    to answer: a decision about an older candidate is history, and `history` is
    the command for that. Reporting the newest decision overall would silently
    answer a different question.

    Guarded on the DB existing before touching the state store. ``get_state_store``
    runs ``init_db`` — full DDL plus migration — so a read-only command that
    called it unconditionally would create a half-megabyte database on a host
    that has none.
    """
    from herdr import scheduler_facts, state_db
    db_path = state_db.resolve_state_db_path()
    if not db_path.is_file():
        return {}, ""
    events = _reverification_rows(workflow_id)
    try:
        expected = scheduler_facts.latest_frozen_candidate_sha(
            workflow_id, db_path=db_path)
    except Exception as exc:  # noqa: BLE001 -- read-only boundary
        print(f"[REVERIFICATION] candidate unavailable: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
    if not expected:
        return {}, ""
    latest = {}
    for event in events:
        payload = event.get("payload") or {}
        if str(payload.get("to_candidate_sha") or "") != expected:
            continue
        latest[str(payload.get("verifier") or "")] = payload
    return latest, expected

def _cmd_reverification_status(args):
    """Why the current candidate did or did not re-run each verifier."""
    from herdr import reverification
    workflow_id = args.workflow_id
    latest, expected = _reverification_current_decisions(workflow_id)
    decisions = [d for _n, d in latest.items()]
    payload = {
        "workflow_id": workflow_id,
        "candidate_sha": expected,
        "metrics": reverification.reverification_metrics(decisions),
        "decisions": sorted(decisions, key=lambda p: p.get("verifier", "")),
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    metrics = payload["metrics"]
    print(f"workflow={workflow_id} candidate={expected[:8] or '(none)'} "
          f"verifiers={metrics['total_verifiers']} "
          f"reuse={metrics['reuse_count']} rerun={metrics['rerun_count']} "
          f"reuse_rate={metrics['reuse_rate']:.2f}")
    for decision in payload["decisions"]:
        print(f"- {decision.get('verifier')}: "
              f"{decision.get('decision')} "
              f"({decision.get('from_candidate_sha', '')[:8]} -> "
              f"{decision.get('to_candidate_sha', '')[:8]})")
        print(f"    reason: {decision.get('reason')}")
        print(f"    changed: "
              f"{','.join(decision.get('changed_paths') or []) or '-'}")
        if decision.get("decision") == "reuse":
            print(f"    scope: "
                  f"{','.join(decision.get('reusable_scope') or []) or '-'}")
            print(f"    source: {decision.get('source_task_id')} "
                  f"verdict={decision.get('source_verdict')} "
                  f"verified_candidate_sha="
                  f"{(decision.get('source_verified_candidate_sha') or '')[:8]}")
        print(f"    policy: {decision.get('policy_version')}")

def _cmd_reverification_history(args):
    try:
        limit = int(getattr(args, "limit", None) or 100)
    except (TypeError, ValueError):
        print("[REVERIFICATION] --limit must be an int", file=sys.stderr)
        sys.exit(2)
    if limit <= 0:
        print("[REVERIFICATION] --limit must be a positive int", file=sys.stderr)
        sys.exit(2)
    rows = _reverification_rows(args.workflow_id, limit=limit)
    if getattr(args, "json", False):
        print(json.dumps([e.get("payload") or {} for e in rows],
                         ensure_ascii=False, indent=2, sort_keys=True))
        return
    if not rows:
        print(f"reverification: no decision facts for {args.workflow_id}")
        return
    for event in rows:
        payload = event.get("payload") or {}
        print(f"#{event.get('id')} {payload.get('verifier')} "
              f"{payload.get('decision')} "
              f"{str(payload.get('from_candidate_sha') or '')[:8]} -> "
              f"{str(payload.get('to_candidate_sha') or '')[:8]} "
              f"reason={payload.get('reason')} "
              f"policy={payload.get('policy_version')}")

def cmd_rollout(args):
    """Controlled Rollout operator entry (status/set/off/history/check-guard)."""
    action = getattr(args, "rollout_command", None)
    if action == "status":
        _cmd_rollout_status(args)
    elif action == "set":
        _cmd_rollout_set(args)
    elif action == "off":
        _cmd_rollout_off(args)
    elif action == "history":
        _cmd_rollout_history(args)
    elif action == "check-guard":
        _cmd_rollout_check_guard(args)
    else:
        print("[ROLLOUT] unknown action; use status|set|off|history|check-guard",
              file=sys.stderr)
        sys.exit(2)

def _cmd_rollout_status(args):
    from herdr import rollout_policy, state_db
    db_path = state_db.resolve_state_db_path()
    if not db_path.is_file():
        states = []
    else:
        # Read failures must not be flattened into "all off": an unknown
        # rollout state is not a safe-looking empty list.
        try:
            states = rollout_policy.list_states(db_path)
        except Exception as exc:  # noqa: BLE001 -- read-only boundary
            print(f"[ROLLOUT] rollout state unavailable: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            sys.exit(1)
    killed = not rollout_policy.rollout_enabled()
    if getattr(args, "json", False):
        print(json.dumps({
            "killed": killed,
            "states": states,
            "algorithm_version": rollout_policy.ALGORITHM_VERSION,
        }, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if killed:
        print("rollout: KILLED (HERDR_ADAPTIVE_ROLLOUT_ENABLED=false) "
              "-> all buckets Legacy")
    if not states:
        print("rollout: no staged buckets "
              "(no rollout rows; buckets with none follow the canary config)")
        return
    for row in states:
        print(f"{row.get('agent')} / {row.get('node')} / "
              f"{row.get('task_type') or '(no type)'} current: "
              f"{_rollout_stage_label(row.get('percentage'))}")

def _cmd_rollout_set(args):
    from herdr import rollout_policy
    raw_pct = getattr(args, "percentage", None)
    if raw_pct is None:
        print("[ROLLOUT] --percentage off/5/10/25/50 is required",
              file=sys.stderr)
        sys.exit(2)
    try:
        pct = rollout_policy.normalize_percentage(raw_pct)
    except ValueError as exc:
        print(f"[ROLLOUT] {exc}", file=sys.stderr)
        sys.exit(2)
    _rollout_change(
        args, pct,
        (getattr(args, "reason", None) or "").strip(),
        (getattr(args, "source", None) or "cli").strip(),
        verb="every rollout change")

def _cmd_rollout_off(args):
    _rollout_change(
        args, 0,
        (getattr(args, "reason", None) or "").strip(),
        (getattr(args, "source", None) or "cli").strip(),
        verb="rollback")

def _cmd_rollout_history(args):
    from herdr import rollout_policy, state_db
    try:
        limit = int(getattr(args, "limit", None) or 100)
    except (TypeError, ValueError):
        print("[ROLLOUT] --limit must be an int", file=sys.stderr)
        sys.exit(2)
    if limit <= 0:
        print("[ROLLOUT] --limit must be a positive int", file=sys.stderr)
        sys.exit(2)
    db_path = state_db.resolve_state_db_path()
    if not db_path.is_file():
        rows = []
    else:
        try:
            rows = rollout_policy.get_history(
                db_path, agent=getattr(args, "agent", None),
                node=getattr(args, "node", None),
                task_type=getattr(args, "task_type", None), limit=limit)
        except Exception as exc:  # noqa: BLE001 -- read-only boundary
            print(f"[ROLLOUT] rollout history unavailable: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            sys.exit(1)
    if getattr(args, "json", False):
        print(json.dumps(rows, ensure_ascii=False, indent=2,
                         sort_keys=True))
        return
    if not rows:
        print("rollout: no audit events")
        return
    for row in rows:
        print(f"#{row.get('id')} {row.get('recommended_agent')}/"
              f"{row.get('node')}/{row.get('task_type') or '(no type)'} "
              f"{_rollout_stage_label(row.get('previous_percentage'))} -> "
              f"{_rollout_stage_label(row.get('new_percentage'))} "
              f"{row.get('action')} "
              f"by {row.get('source')} reason={row.get('reason')}")

def _cmd_rollout_check_guard(args):
    from herdr import rollout_policy, state_db
    bucket = _rollout_bucket_args(args)
    auto = bool(getattr(args, "auto_rollback", False))
    reason = (getattr(args, "reason", None) or "safety guard triggered").strip()
    source = (getattr(args, "source", None) or "guard").strip()
    # Read-only evaluation must not create or migrate the database; the
    # path comes from the same single resolver the write path uses.
    db_path = _rollout_db_path()
    try:
        if auto:
            result = rollout_policy.maybe_auto_rollback(
                db_path, agent=bucket["agent"], node=bucket["node"],
                task_type=bucket["task_type"], reason=reason, source=source,
                config_fallback=_rollout_config_fallback(bucket))
        else:
            verdict = rollout_policy.evaluate_guard(
                db_path, agent=bucket["agent"], node=bucket["node"],
                task_type=bucket["task_type"])
            result = {"action": "triggered" if verdict.get("triggered")
                      else "none", **verdict}
    except ValueError as exc:
        print(f"[ROLLOUT] {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:  # noqa: BLE001 -- CLI boundary
        print(f"[ROLLOUT] failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        sys.exit(1)
    # "Could not evaluate" is not "no problem": a monitoring job must
    # never read an unavailable guard as a passing check.
    if result.get("status") == "unavailable":
        if getattr(args, "json", False):
            print(json.dumps(result, ensure_ascii=False, indent=2,
                             sort_keys=True), file=sys.stderr)
        else:
            print(f"[ROLLOUT] guard could not evaluate "
                  f"(status=unavailable): {result.get('reason')}",
                  file=sys.stderr)
        sys.exit(1)
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2,
                         sort_keys=True))
        return
    print(f"[ROLLOUT] guard action={result.get('action')} "
          f"status={result.get('status')} reason={result.get('reason')}")
