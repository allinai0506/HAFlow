"""Record completed test executions and route their verification consequences.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def check_task_tests_completed(ports, task, store=None, now=None):
    """Continuous Evaluation Checkpoint: tests_completed.

    Deterministic trigger when a real test run produces new METRICS.json evidence.
    Does NOT complete tasks, does NOT disrupt working agents on intermediate failures,
    uses persisted evidence_id dedup (restart-safe) and RateGate throttling.
    """
    if ports.supervisor_harness is None or not task:
        return None

    # 1. Kill switches — checked FIRST, before any file I/O (Fix 5).
    #    supervisor_enabled() verifies: enabled flag + provider flag + API key.
    cfg = ports.supervisor_harness.load_config()
    from herdr.supervisor.config import supervisor_enabled
    if not supervisor_enabled(cfg):
        return None

    clone_path = task.get("clone_path")
    if not clone_path or not ports.os.path.isdir(clone_path):
        return None

    # 2. Extract test evidence (gated by EVAL_DONE.json atomic sentinel)
    from herdr.supervisor import evidence as supervisor_evidence
    test_evidence = supervisor_evidence.extract_test_evidence(clone_path)
    if not test_evidence:
        return None
    if task.get('completion_protocol') == 'receipt-v1' and (
            test_evidence.get('task_id') != task.get('task_id')
            or test_evidence.get('run_id') != task.get('run_id')
            or test_evidence.get('epoch') != task.get('completion_epoch')):
        # Never rebind an old or unattached evaluator snapshot to a current Run.
        return None

    # 3. Build deterministic evidence fingerprint
    evidence_id = supervisor_evidence.build_test_evidence_id(test_evidence)

    # 4. Dedup against persisted evaluation events (restart-safe).
    #    Fix 4: query specifically for supervisor_evaluation events from the
    #    semantic_supervisor source, so the dedup window is never squeezed out
    #    by unrelated events flooding the ledger.
    st = store if store is not None else ports._get_store()
    task_id = task.get("task_id")
    try:
        events = st.list_events(
            task_id=task_id,
            event_type="supervisor_evaluation",
            source="semantic_supervisor",
            desc=True,
        ) or []
    except Exception:
        events = []

    from herdr.supervisor.evaluation import latest_tests_completed_evidence_id
    latest_ev_id = latest_tests_completed_evidence_id(events)
    if latest_ev_id == evidence_id:
        return None

    # 5. RateGate check: can supervisor evaluate now?
    # If RateGate skips, we DEFER (do not evaluate now, but keep evidence un-evaluated
    # so it can be evaluated when RateGate interval clears).
    sup = ports.supervisor_harness.get_supervisor(cfg)
    if sup is not None:
        skip_reason = sup.should_evaluate(task_id, "tests_completed", now=now)
        if skip_reason is not None:
            return None

    # 6. Materialize the compact verification receipt as immutable evidence.
    verification = {
        "type": "tests_completed",
        "epoch": test_evidence.get("epoch"),
        "candidate_sha": test_evidence.get("candidate_sha"),
        "execution_mode": test_evidence.get("execution_mode", "unknown"),
        "command": test_evidence.get("command"),
        "environment": test_evidence.get("environment"),
        "source_fingerprint": test_evidence.get("source_fingerprint"),
        "exit_code": test_evidence.get("exit_code"),
        "counts": {"pass": test_evidence.get("passed_tests"),
                   "fail": test_evidence.get("failing_count"), "skip": test_evidence.get("skipped_tests")},
        "passed": bool(test_evidence.get("converged", False)),
        "evidence_id": evidence_id,
        "passed_tests": test_evidence.get("passed_tests"),
        "total_tests": test_evidence.get("total_tests"),
        "failing_count": test_evidence.get("failing_count", 0),
        "lint_errors": test_evidence.get("lint_errors", 0),
        "type_errors": test_evidence.get("type_errors", 0),
        "composite_score": test_evidence.get("composite_score", 0.0),
    }
    dispatch = ports._latest_verification_dispatch(task, st)
    if dispatch is not None:
        if not ports._verification_evidence_is_new(dispatch, test_evidence):
            print(
                f"[VERIFY EVIDENCE DEFERRED] task={task_id}: "
                "EVAL_DONE predates current VERIFY dispatch"
            )
            return None
        dispatch_payload = dispatch.get("payload") or {}
        verification["intervention_id"] = dispatch_payload.get("intervention_id")
        verification["decision_id"] = dispatch_payload.get("decision_id")
    try:
        observation, _created = ports.create_verification_observation_with_status(
            verification,
            run_id=task.get("run_id") or f"run_{task_id}",
            task_id=task_id,
            workflow_id=task.get("workflow_id"),
            store=ports.ObservationStore(getattr(st, "db_path", None)),
        )
        verification["observation_id"] = observation.observation_id
        ports.record_observation_created(
            task,
            observation,
            ledger=ports.TrajectoryLedger(getattr(st, "db_path", None)),
        )
    except Exception as exc:
        print(f"[VERIFICATION OBSERVATION SKIPPED] task={task_id}: {type(exc).__name__}")

    # 7. Record deterministic tests_completed event
    try:
        st.record_event(
            "tests_completed",
            {
                "task_id": task_id,
                "workflow_id": task.get("workflow_id"),
                "iteration": test_evidence.get("iteration"),
                "evidence_id": evidence_id,
                "passed_tests": test_evidence.get("passed_tests"),
                "total_tests": test_evidence.get("total_tests"),
                "failing_count": test_evidence.get("failing_count", 0),
                "lint_errors": test_evidence.get("lint_errors", 0),
                "type_errors": test_evidence.get("type_errors", 0),
                "composite_score": test_evidence.get("composite_score", 0.0),
                "intervention_id": verification.get("intervention_id"),
                "decision_id": verification.get("decision_id"),
            },
            task_id=task_id,
            workflow_id=task.get("workflow_id"),
            node_id=task.get("node") or task.get("stage"),
            agent_id=task.get("agent"),
            source="herdr-controller",
        )
        receipt = ports.record_trajectory_event(
            task,
            "verification_completed",
            ledger=ports.TrajectoryLedger(getattr(st, "db_path", None)),
            verification=verification,
        )
        if not receipt:
            raise RuntimeError("verification_completed receipt was not persisted")
    except Exception as exc:
        print(f"[TESTS_COMPLETED EVENT ERROR] task={task_id}: {exc}")
        # Do not consume/deduplicate this evidence until the durable receipt
        # exists. The next watcher pass must be able to retry the receipt.
        return None

    # 7. Run supervisor checkpoint
    return ports.supervisor_checkpoint(
        task,
        "tests_completed",
        test_evidence=test_evidence,
        evidence_id=evidence_id,
        now=now,
    )


DEPENDENCIES = {'check_task_tests_completed': ('ObservationStore', 'TrajectoryLedger', '_get_store',
                                '_latest_verification_dispatch', '_verification_evidence_is_new',
                                'create_verification_observation_with_status', 'os',
                                'record_observation_created', 'record_trajectory_event',
                                'supervisor_checkpoint', 'supervisor_harness')}

__all__ = tuple(DEPENDENCIES)
