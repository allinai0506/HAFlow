"""Task review-closure evidence handlers."""

import json
import os
import sys


def _enforce_wrapup_review_closure(
    task_id,
    task,
    *,
    skip,
    note,
    operator=None,
    operator_token=None,
    operator_credentials=None,
    deps,
):
    _get_store = deps["get_store"]
    "CLI-side pre-check for the shared wrapup review-closure invariant.\n\n    The authoritative gate lives in ``herdr.kernel.transition_task`` — every\n    completion source (CLI ``set``, the controller's CAS auto-accept path and\n    repair flows) funnels through it. This pre-check only exists for a\n    friendly refusal message. The audited ``--skip-review-closure`` bypass\n    requires an authenticated operator identity independent of the task worker\n    plus the ``HERDR_OPERATOR=1`` shell capability (task workers do not carry it);\n    it records a ``review_closure_skipped`` event, which also satisfies the\n    kernel gate (Codex review P1, Issue #228 Item 1).\n    "
    from herdr import review_closure

    store = _get_store()
    if skip:
        reason = str(note or "").strip()
        if not reason:
            print(
                "[REVIEW CLOSURE SKIP REFUSED] --skip-review-closure requires --note with the audited reason",
                file=sys.stderr,
            )
            sys.exit(2)
        operator_id = str(operator or "").strip()
        if not operator_id:
            print(
                "[REVIEW CLOSURE SKIP REFUSED] --skip-review-closure requires --operator <identity> (an operator independent of the task worker)",
                file=sys.stderr,
            )
            sys.exit(2)
        if os.environ.get("HERDR_OPERATOR") != "1":
            print(
                "[REVIEW CLOSURE SKIP REFUSED] the emergency bypass requires an operator shell capability (HERDR_OPERATOR=1); task workers do not carry it",
                file=sys.stderr,
            )
            sys.exit(2)
        auth_ok, auth_why = review_closure.authenticate_operator(
            store,
            task,
            operator_id,
            token=operator_token,
            credentials_path=operator_credentials,
        )
        if not auth_ok:
            print(
                f"[REVIEW CLOSURE SKIP REFUSED] operator authentication failed: {auth_why}",
                file=sys.stderr,
            )
            sys.exit(2)
        head = review_closure.clone_head_sha(task.get("clone_path"))
        if not head:
            print(
                "[REVIEW CLOSURE SKIP REFUSED] cannot bind the bypass: the task clone/HEAD is unavailable (keep the clone retained or fix clone_path before bypassing)",
                file=sys.stderr,
            )
            sys.exit(2)
        store.record_event(
            "review_closure_skipped",
            {
                "task_id": task_id,
                "reason": reason,
                "head_sha": head,
                "operator": operator_id,
            },
            task_id=task_id,
            workflow_id=task.get("workflow_id"),
            node_id=task.get("node") or task.get("stage"),
            source="herdr-task",
        )
        print(
            f"[REVIEW CLOSURE SKIPPED] {task_id}: completion allowed with audited bypass"
        )
        return
    ok, why = review_closure.check_completion_allowed(store, task)
    if ok:
        print(f"[REVIEW CLOSURE RECEIPT] {task_id}: receipt present")
        return
    print(f"[REVIEW CLOSURE REQUIRED] {why}", file=sys.stderr)
    sys.exit(2)


def record_review_closure(
    task_id,
    pr_url=None,
    ci_status=None,
    findings=None,
    deferred=False,
    reason=None,
    head_sha=None,
    unresolved_p1=None,
    unresolved_p2=None,
    *,
    deps,
):
    _get_store = deps["get_store"]
    load_tasks = deps["load_tasks"]
    find_task = deps["find_task"]
    "Record the machine-readable review-closure receipt for a task.\n\n    The receipt is what the completion transition validates for wrapup tasks:\n    the head SHA it closed out (a later commit makes it stale) plus, for a\n    normal closure, the delivery PR URL, ``ci_status=pass``, the structured\n    unresolved P1/P2 counts (P1 must be 0) and a disposition text — or, for a\n    deferred closure, a reason with the blocking evidence. Content is\n    validated here so an unacceptable receipt can never be written; when the\n    task clone is readable the supplied head must match its current HEAD.\n    "
    from herdr import review_closure

    data = load_tasks()
    task = find_task(data, task_id)
    if not task:
        print(f"Task not found: {task_id}")
        sys.exit(1)
    payload = {
        "task_id": task_id,
        "pr_url": str(pr_url or "").strip(),
        "ci_status": str(ci_status or "").strip().lower(),
        "findings": str(findings or "").strip(),
        "deferred": bool(deferred),
        "reason": str(reason or "").strip(),
        "head_sha": str(head_sha or "").strip().lower(),
        "unresolved_p1": unresolved_p1,
        "unresolved_p2": unresolved_p2,
    }
    ok, why = review_closure.validate_receipt_payload(
        payload, require_verification=False
    )
    if not ok:
        print(f"[REVIEW CLOSURE REFUSED] {why}", file=sys.stderr)
        sys.exit(2)
    current_head = review_closure.clone_head_sha(task.get("clone_path"))
    if current_head and current_head != payload["head_sha"]:
        print(
            f"[REVIEW CLOSURE REFUSED] --head-sha {payload['head_sha'][:12]} does not match the task clone HEAD {current_head[:12]}; the receipt must certify the current reviewed head",
            file=sys.stderr,
        )
        sys.exit(2)
    store = _get_store()
    if not deferred:
        identity_why = review_closure.identity_mismatch(
            store, task, payload["pr_url"], payload["head_sha"]
        )
        if identity_why:
            print(
                f"[REVIEW CLOSURE REFUSED] delivery identity check failed: {identity_why}",
                file=sys.stderr,
            )
            sys.exit(2)
        verified, detail, verification = review_closure.verify_against_forge(
            payload["pr_url"],
            payload["head_sha"],
            declared_p2=payload.get("unresolved_p2"),
            findings=payload.get("findings"),
        )
        if not verified:
            print(
                f"[REVIEW CLOSURE REFUSED] forge verification failed: {detail}",
                file=sys.stderr,
            )
            sys.exit(2)
        payload["verification"] = verification
    else:
        evidence_ok, evidence_detail, evidence = (
            review_closure.collect_deferred_evidence(store, task)
        )
        if not evidence_ok:
            print(
                f"[REVIEW CLOSURE REFUSED] deferred is not justified: {evidence_detail}",
                file=sys.stderr,
            )
            sys.exit(2)
        payload["verification"] = evidence
    ok, why = review_closure.validate_receipt_payload(payload)
    if not ok:
        print(f"[REVIEW CLOSURE REFUSED] {why}", file=sys.stderr)
        sys.exit(2)
    store.record_event(
        "review_closure_recorded",
        payload,
        task_id=task_id,
        workflow_id=task.get("workflow_id"),
        node_id=task.get("node") or task.get("stage"),
        source="herdr-task",
    )
    print("[REVIEW CLOSURE RECORDED] " + json.dumps(payload, ensure_ascii=False))
