"""Task status transitions, verdict acceptance, and adoption safeguards."""

import os
import json
import sys
from pathlib import Path


def set_task_status(task_id, new_status, verdict=None, note=None, affected_task_ids=None,
                    skip_review_closure=False, skip_operator=None,
                    operator_token=None, operator_credentials=None, *,
                    load_tasks, find_task, get_store, transitions,
                    active_task_statuses, is_gate_task, capture_verified_candidate,
                    enforce_wrapup_review_closure, authenticate_gate_verdict_evidence,
                    record_gate_note, clear_suppress_auto_close,
                    sync_tasks_projection, tasks_file=None,
                    gate_verdict_file_candidates=None):

    data = load_tasks()
    task = find_task(data, task_id)

    if not task:
        print(f"Task not found: {task_id}")
        sys.exit(1)

    if (verdict in ('pass', 'approved') and task.get('completion_protocol') == 'receipt-v1'
            and (task.get('node') or task.get('stage')) in ('test', 'review')):
        from herdr.task_checkpoint import has_business_acceptance
        if not has_business_acceptance(get_store(), task, task.get('candidate_sha')):
            print('Business acceptance checkpoint receipt required before passing gate', file=sys.stderr)
            sys.exit(2)

    # PR #110: 结构化归因目标仅允许随 blocked 结论持久化;pass 或未给
    # verdict 时携带 affected-task-id 直接拒绝(不静默忽略)。
    affected_ids = []
    for raw in affected_task_ids or []:
        text = str(raw or "").strip()
        if text and text not in affected_ids:
            affected_ids.append(text)
    if affected_ids and verdict != "blocked":
        print(
            "--affected-task-id is only valid with '--verdict blocked'",
            file=sys.stderr,
        )
        sys.exit(2)

    # Completion evidence, before any verdict is accepted.
    #
    # baseline_commit is *launch* evidence: the clone HEAD when the worker
    # started. An agent may pull, check out, or rebase mid-task, so it cannot
    # prove which revision was verified once the verdict is submitted. Re-read
    # the live clone now and store that as verified_candidate_sha, which is
    # what the join gate must trust (AGENTS.md §4.4: program-produced, never
    # model-reported).
    if verdict in ("pass", "blocked"):
        capture_verified_candidate(task)
        # Fail closed at the verdict boundary, for scheduler-managed tasks
        # only. A task that carries a candidate claim is asserting "I verified
        # revision X"; if the clone HEAD could not be read at verdict time,
        # that assertion is unproven, and letting it through would let launch
        # evidence quietly reinstate the "they happened to start from the same
        # candidate" claim this whole mechanism exists to eliminate.
        #
        # A task with no candidate claim makes no such assertion: it is outside
        # scheduler identity semantics, so refusing its verdict would break
        # unrelated work without closing any real hole.
        if (
            str(task.get("candidate_sha") or "").strip()
            and not str(task.get("verified_candidate_sha") or "").strip()
        ):
            print(
                f"[VERDICT REFUSED] {task_id}: refusing verdict "
                f"'{verdict}' without completion evidence. The clone HEAD "
                "could not be read at verdict time, so the revision actually "
                "verified is unproven. Re-run the verification and submit the "
                "verdict again (AGENTS.md §4.4: fail closed, no silent pass).",
                file=sys.stderr,
            )
            sys.exit(2)

    # ADR-0006 & OEP-0002 (D2): Authenticate candidate gate verdict file and bind immutable evidence hash
    if verdict in ("pass", "blocked"):
        cand_func = gate_verdict_file_candidates
        if cand_func is None:
            from herdr.task_cli_verification import _gate_verdict_file_candidates as cand_func
        existing_candidates = [p for p in cand_func(task) if os.path.exists(p)]
        if existing_candidates:
            ok, res_or_err = authenticate_gate_verdict_evidence(
                task, expected_verdict=verdict
            )
            if not ok:
                print(
                    f"[VERDICT REFUSED] task {task_id}: {res_or_err}",
                    file=sys.stderr,
                )
                sys.exit(2)
            snap_hash = getattr(res_or_err, "snapshot_hash", "")
            if snap_hash:
                task["evidence_snapshot_hash"] = snap_hash

    if new_status not in transitions:
        print(f"Invalid status: {new_status}")
        sys.exit(1)

    if (verdict or note or affected_ids) and new_status != "completed":
        print("verdict/note only apply to 'set <task> completed'")
        sys.exit(2)

    if verdict == "blocked" and not (note or "").strip():
        print("blocked verdict requires --note with the blocker list")
        sys.exit(2)

    # 先写入字段再判同状态:同状态重复 set completed 补落 verdict 也要生效。
    if verdict:
        task["stage_verdict"] = verdict

    if note:
        task["stage_verdict_note"] = note

    # PR #110:归因字段随结论一起覆盖写。只在非空时写会让上一次的
    # affected_task_ids 存活过新一轮结论——本轮 Verifier 明确「无法归因」
    # (不写该字段)时,系统会拿着上一轮的旧归因去做选择性返工。
    if verdict:
        task["stage_verdict_affected_task_ids"] = list(affected_ids)

    old_status = task["status"]

    if old_status == new_status:
        if verdict or note or affected_ids:
            updates = {}
            if verdict:
                updates["stage_verdict"] = verdict
            if note:
                updates["stage_verdict_note"] = note
            if verdict:
                updates["stage_verdict_affected_task_ids"] = list(affected_ids)
            if task.get("evidence_snapshot_hash"):
                updates["evidence_snapshot_hash"] = task["evidence_snapshot_hash"]
            store = get_store()
            try:
                from herdr import kernel
                kernel.update_task_metadata(
                    task_id=task_id,
                    updates=updates,
                    store=store,
                )
            except Exception as exc:
                print(f"Error updating task {task_id}: {exc}", file=sys.stderr)
                sys.exit(2)

            t_file = tasks_file or os.environ.get("TASKS_FILE")
            sync_tasks_projection(store=store, tasks_file=Path(t_file) if t_file else None)

            if verdict:
                record_gate_note(task, verdict, note)

            print(f"{task_id}: already {new_status}; verdict/note updated")
            return

        print(f"{task_id}: already {new_status}")
        return

    allowed_transitions = set(transitions.get(old_status, set()))
    force_transition = False
    is_watchdog_gate = (
        is_gate_task(task)
        and task.get("failure_reason") == "hard_timeout_watchdog"
    )
    if old_status == "failed" and new_status == "completed" and verdict and is_watchdog_gate:
        # Issue #250 & Codex review: Allow adopting gate verdict from failed state ONLY for watchdog-failed gate tasks
        allowed_transitions.add("completed")
        force_transition = True

    if new_status not in allowed_transitions:
        print(
            f"Illegal transition: "
            f"{task_id}: {old_status} -> {new_status}"
        )
        sys.exit(2)

    # Wrapup completion must carry the review-closure receipt (machine-
    # produced). Prompt-text-only requirements can still be bypassed by a
    # bare `set ... completed` — including the controller's auto-accept path,
    # which is also a CLI set (Codex review P1, wf-haflow-1015-01).
    if new_status == "completed" and (task.get("node") or task.get("stage")) == "wrapup":
        enforce_wrapup_review_closure(task_id, task, skip=skip_review_closure,
                                       note=note, operator=skip_operator,
                                       operator_token=operator_token,
                                       operator_credentials=operator_credentials)


    metadata = {}
    if verdict:
        metadata["stage_verdict"] = verdict
    if note:
        metadata["stage_verdict_note"] = note
    if verdict:
        metadata["stage_verdict_affected_task_ids"] = list(affected_ids)
    if task.get("evidence_snapshot_hash"):
        metadata["evidence_snapshot_hash"] = task["evidence_snapshot_hash"]

    expected_status = old_status if force_transition else None
    store = get_store()
    if force_transition:
        # P2: Validate that the current persistent snapshot still satisfies the authorized
        # watchdog-recovery invariants before adopting the verdict. If another writer updated
        # the task (e.g. different failure reason, execution epoch, or status), abort adoption.
        fresh = store.get_task(task_id)
        if not fresh or not isinstance(fresh, dict):
            print(f"Task not found: {task_id}", file=sys.stderr)
            sys.exit(2)
        fresh_watchdog_gate = (
            is_gate_task(fresh)
            and fresh.get("status") == "failed"
            and fresh.get("failure_reason") == "hard_timeout_watchdog"
        )
        if not fresh_watchdog_gate:
            print(
                f"Concurrent state change: task {task_id} is no longer an authorized watchdog-failed gate (status={fresh.get('status')}, reason={fresh.get('failure_reason')})",
                file=sys.stderr,
            )
            sys.exit(2)
        if task.get("execution_token") and fresh.get("execution_token") != task.get("execution_token"):
            print(
                f"Concurrent state change: task {task_id} execution token changed ({fresh.get('execution_token')} != {task.get('execution_token')})",
                file=sys.stderr,
            )
            sys.exit(2)
        # Retain the version bound to the authorized snapshot / candidate capture;
        # do not overwrite with an unverified newer version from another writer.
        if task.get("version") is not None and fresh.get("version") != task.get("version"):
            print(
                f"Concurrent state change: task {task_id} was modified by another writer (expected version {task.get('version')}, found {fresh.get('version')})",
                file=sys.stderr,
            )
            sys.exit(2)
        elif task.get("version") is None:
            task["version"] = fresh.get("version")

        # Codex Review P1 (Round 7): Re-read and authenticate durable gate verdict evidence from disk
        # before committing transition, binding the adopted verdict to the durable evidence snapshot.
        ok, res_or_err = authenticate_gate_verdict_evidence(fresh, expected_verdict=verdict)
        if not ok:
            print(
                f"[VERDICT REFUSED] task {task_id}: {res_or_err}",
                file=sys.stderr,
            )
            sys.exit(2)
        file_v, file_note, file_aff, file_mtime = res_or_err
        metadata["gate_verdict_evidence_mtime"] = file_mtime
        snap_hash = getattr(res_or_err, "snapshot_hash", "")
        if snap_hash:
            metadata["evidence_snapshot_hash"] = snap_hash
            task["evidence_snapshot_hash"] = snap_hash
        if not note and file_note:
            metadata["stage_verdict_note"] = file_note
        if file_v == "blocked" and file_aff and not affected_ids:
            metadata["stage_verdict_affected_task_ids"] = file_aff
    expected_version = task.get("version") if force_transition else None
    try:
        from herdr import kernel
        res = kernel.transition_task(
            task_id=task_id,
            to_status=new_status,
            reason="cli_set_status",
            source="herdr-task",
            metadata=metadata,
            force=force_transition,
            store=store,
            expected_status=expected_status,
            expected_version=expected_version,
        )
        if force_transition and not res.get("accepted", True):
            print(
                f"Concurrent state change: task {task_id} is no longer at observed failed state (version {expected_version})",
                file=sys.stderr,
            )
            sys.exit(2)
    except Exception as exc:
        print(f"Error transitioning task {task_id}: {exc}", file=sys.stderr)
        sys.exit(2)

    t_file = tasks_file or os.environ.get("TASKS_FILE")
    sync_tasks_projection(store=store, tasks_file=Path(t_file) if t_file else None)

    if new_status in active_task_statuses:
        clear_suppress_auto_close(task.get("workflow_id") or res.get("workflow_id"))

    if verdict:
        record_gate_note(task, verdict, note)

    print(f"{task_id}: {old_status} -> {new_status}")


def adopt_task_verdict(task_id, verdict=None, note=None, operator="human", as_json=False, *,
                       load_tasks, find_task, is_gate_task,
                       authenticate_evidence, set_task_status):
    """Adopt gate verdict from gate-verdict file into task (productizing manual arbitration)."""
    data = load_tasks()
    task = find_task(data, task_id)
    if not task:
        print(f"Task not found: {task_id}", file=sys.stderr)
        sys.exit(1)

    if not is_gate_task(task):
        print(f"Task {task_id} is not a gate task", file=sys.stderr)
        sys.exit(2)

    affected_ids = []
    if not verdict:
        ok, res_or_err = authenticate_evidence(task)
        if not ok:
            print(f"No valid gate verdict found for task {task_id}", file=sys.stderr)
            sys.exit(2)
        verdict, file_note, affected_ids, _ = res_or_err
        note = note or file_note
    else:
        ok, res_or_err = authenticate_evidence(task, expected_verdict=verdict)
        if not ok:
            print(f"[ADOPT VERDICT REFUSED] task {task_id}: {res_or_err}", file=sys.stderr)
            sys.exit(2)
        _, file_note, file_aff, _ = res_or_err
        note = note or file_note
        if verdict == "blocked" and file_aff and not affected_ids:
            affected_ids = file_aff

    if not verdict or verdict not in ("pass", "blocked"):
        print(f"No valid gate verdict found for task {task_id}", file=sys.stderr)
        sys.exit(2)

    if verdict == "blocked" and not (note or "").strip():
        note = f"gate verdict blocked (adopted by {operator})"

    old_status = task.get("status")
    set_task_status(
        task_id,
        "completed",
        verdict=verdict,
        note=note,
        affected_task_ids=affected_ids if affected_ids else None,
        skip_operator=operator,
    )

    result = {
        "task_id": task_id,
        "status": "completed",
        "previous_status": old_status,
        "verdict": verdict,
        "note": note,
        "affected_task_ids": affected_ids,
        "operator": operator,
    }
    if task.get("evidence_snapshot_hash"):
        result["evidence_snapshot_hash"] = task["evidence_snapshot_hash"]
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"[GATE VERDICT ADOPTED] task={task_id} status={old_status} -> completed "
            f"verdict={verdict} note=\"{note}\""
        )
        print("HERDR_GATE_ADOPT_RESULT=" + json.dumps(result, ensure_ascii=False))
    return 0
