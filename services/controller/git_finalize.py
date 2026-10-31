"""Commit and integrate completed tasks through the existing CLI pipeline.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""
from services.controller.finalize_support import (_INTEGRATION_IDENTITY_KEYS,
                                                  _preserve_none_mode_output,
                                                  _settle_integration_result,
                                                  _should_allow_rebase)


def finalize_completed_task(ports, task_id, before_action=None):
    """Drive one git-finalize step; returns ``{"retryable": bool, ...}``.

    M-2: deterministic outcomes (rc=3 EMPTY, rc=4 REFUSED, rc=5
    integrate-blocked/main-dirty, rc=6 conflict) report
    ``retryable=False`` so ``_check_finalize_retry`` never consumes
    budget or logs a retry for them; only transient/unknown failures
    (rc=75, unexpected rc, state-transition failures) report True.
    H-1: rc=5 (main repo tracked changes) is persistent, never self-heals,
    so it escalates immediately instead of retrying 5x into
    ``finalize_escalated`` + silent ``delivered``.
    ``before_action(action, task_id=None, commit_head=None)`` optionally
    re-checks recovery budget/ownership before each new independent action;
    raising stops the chain in place, keeping results already produced.
    A legal rebase's new HEAD is accepted only after the integration
    evidence attributes it to this call; the confirmed SHA feeds every
    later guard, and an unattributable result escalates to a human.
    """
    inflight = getattr(ports, "_finalize_inflight", None)
    if inflight is not None and isinstance(inflight, set):
        if task_id in inflight:
            return {"retryable": True, "kind": "inflight", "reason": "finalize_already_in_progress"}
        inflight.add(task_id)
    try:
        return _finalize_completed_task_body(ports, task_id, before_action=before_action)
    finally:
        if inflight is not None and isinstance(inflight, set):
            inflight.discard(task_id)


def _finalize_completed_task_body(ports, task_id, before_action=None):
    task = ports.get_task(task_id)

    def guard(action, commit_head=None):
        if before_action is not None:
            before_action(action, task_id=task_id, commit_head=commit_head)

    own_head = None

    if not task:
        print(f"[FINALIZE SKIP] task={task_id} missing")
        return {"retryable": False, "kind": "skip"}

    if task.get("status") not in ("completed", "committed"):
        print(
            f"[FINALIZE SKIP] "
            f"task={task_id} "
            f"status={task.get('status')}"
        )
        return {"retryable": False, "kind": "skip"}

    if task.get('superseded_by'):
        return {'retryable': False, 'kind': 'retired_lineage'}

    # Acceptance and delivery are separate: never land a known failed candidate.
    if task.get('integration_mode') == 'git' and task.get('commit'):
        failed_sha = task['commit']
        for verifier in ports._get_store().list_tasks(workflow_id=task.get('workflow_id')):
            if (verifier.get('status') != 'superseded' and not verifier.get('superseded_by')
                    and verifier.get('stage_verdict') == 'blocked'
                    and verifier.get('candidate_sha') == failed_sha):
                payload = {
                    "failed_sha": failed_sha,
                    "verifier_task_id": verifier.get("task_id"),
                    "stage_verdict": "blocked",
                }
                print(f"[FINALIZE BLOCKED] task={task_id} candidate={failed_sha} blocked by verifier={verifier.get('task_id')}")
                ports._record_finalize_event(task, "finalize_candidate_blocked", payload)
                ports._escalate_finalize(task, "candidate_blocked", payload)
                return {'retryable': False, 'kind': 'candidate_blocked'}

    # Collaboration accelerator: authoritative task completion closes the
    # handoffs targeting it, so handoff latency metrics stay trustworthy.
    for completed in ports.maybe_complete_on_task_done(task_id):
        print(f"[COLLABORATION COMPLETED] {completed.get('event_id')}")

    mode = task.get("integration_mode") or "none"

    print(
        f"[FINALIZE] "
        f"task={task_id} "
        f"integration_mode={mode}"
    )

    # --------------------------------
    # 需要 Git 集成
    # --------------------------------
    if mode == "git":

        clone_path = task.get("clone_path")
        if clone_path and ports.ensure_no_git_processes is not None:
            try:
                ports.ensure_no_git_processes(clone_path)
            except Exception as exc:
                print(
                    f"[FINALIZE WAIT] task={task_id} git process still active: {exc}"
                )
                return {"retryable": True, "kind": "wait"}

        # 1. 将 Task 自己产生的修改安全提交(若此前已 committed 则跳过)
        skip_integrate = False
        if task.get("status") == "completed":
            # 提交门禁拆分:herdr 任务克隆内只跑快速必需检查,
            # 全量测试由 workflow test 节点与 pre-push 门禁负责。
            commit_env = dict(ports.os.environ)
            commit_env["HERDR_DEFER_HEAVY_TESTS"] = "1"

            commit_args = [
                ports.TASK_MANAGER,
                "commit",
                task_id,
                "--message",
                f"task: {task_id}",
            ]
            check_rebase = getattr(ports, "_should_allow_rebase_for_finalize", _should_allow_rebase)
            if check_rebase(task):
                commit_args.append("--allow-rebase")

            guard("commit")
            result = ports.subprocess.run(
                commit_args,
                text=True,
                capture_output=True,
                env=commit_env
            )

            if result.stdout.strip():
                print(result.stdout.strip())

            commit_payload = ports._parse_commit_result(result.stdout)

            if result.returncode == 3:
                fresh = ports.get_task(task_id) or task
                print(
                    f"[FINALIZE EMPTY] "
                    f"task={task_id} "
                    f"head={commit_payload.get('head')} "
                    f"basis={commit_payload.get('basis')}"
                )
                ports._record_finalize_event(
                    fresh, "finalize_empty", commit_payload
                )
                if not ports._empty_auto_releasable(fresh):
                    ports._escalate_finalize(
                        fresh,
                        "empty_unreleasable",
                        commit_payload,
                    )
                    return {"retryable": False, "kind": "empty", "rc": 3}
                guard("cleanup_ready")
                if not ports.set_task_status(task_id, "cleanup_ready"):
                    print(
                        f"[FINALIZE ERROR] "
                        f"task={task_id} "
                        f"empty release did not reach cleanup_ready"
                    )
                    return {"retryable": True, "kind": "error", "rc": 3}
                skip_integrate = True
                task = ports.get_task(task_id)
            elif (result.returncode == 7 and commit_payload.get("task_id") == task_id
                  and commit_payload.get("result") == "delivery_blocked"):
                delivery = commit_payload.get("delivery") or {}
                from herdr.task_delivery import record_delivery_failure
                if record_delivery_failure(task, delivery, store=ports._get_store()):
                    print(f"[DELIVERY BLOCKED] task={task_id} waiting for scoped repair")
                    return {"retryable": False, "kind": "delivery", "rc": 7}
                return {"retryable": False, "kind": "stale_delivery_result", "rc": 7}
            elif result.returncode == 4:
                print(
                    f"[FINALIZE REFUSED] "
                    f"task={task_id} "
                    f"reason={commit_payload.get('reason')}"
                )
                ports._escalate_finalize(
                    ports.get_task(task_id) or task,
                    "commit_refused",
                    commit_payload,
                )
                return {"retryable": False, "kind": "refused", "rc": 4, "reason": "commit_refused"}
            elif result.returncode == 75:
                print(
                    f"[FINALIZE WAIT] "
                    f"task={task_id} "
                    f"reason=git_busy retry later"
                )
                if (commit_payload.get("task_id") == task_id
                        and commit_payload.get("result") == "wait"
                        and commit_payload.get("reason") == "git_index_lock"):
                    return {"retryable": True, "kind": "wait", "rc": 75,
                            "reason": "git_index_lock", "budgeted": False}
                return {"retryable": True, "kind": "wait", "rc": 75}
            elif result.returncode != 0:
                from herdr.task_delivery import diagnostic_tail
                err_text = result.stderr.strip() or result.stdout.strip()
                print(
                    f"[COMMIT ERROR] "
                    f"task={task_id} "
                    f"rc={result.returncode}: "
                    f"{err_text}"
                )
                ports._record_finalize_event(
                    task,
                    "finalize_commit_error",
                    {
                        "rc": result.returncode,
                        "detail": diagnostic_tail(result.stdout, result.stderr),
                    },
                )
                unrecoverable = (
                    result.returncode == 2
                    or "does not have a commit checked out" in err_text.lower()
                    or "unable to index file" in err_text.lower()
                    or "fatal: adding files failed" in err_text.lower()
                    or "pre-commit script failed" in err_text.lower()
                    or "complexity check failed" in err_text.lower()
                    or "commit blocked" in err_text.lower()
                    or "husky - pre-commit" in err_text.lower()
                )
                if unrecoverable:
                    ports._escalate_finalize(
                        ports.get_task(task_id) or task,
                        "commit_fatal_error",
                        {
                            "rc": result.returncode,
                            "detail": diagnostic_tail(result.stdout, result.stderr),
                        },
                    )
                    return {"retryable": False, "kind": "error", "rc": result.returncode}
                return {"retryable": True, "kind": "error", "rc": result.returncode}
            else:
                if commit_payload.get("task_id") == task_id and commit_payload.get("result") in ("created", "adopted"):
                    own_head = commit_payload.get("commit") or None
                task = ports.get_task(task_id)

                if not task or task.get("status") != "committed":
                    print(f"[FINALIZE ERROR] task={task_id} did not reach committed")
                    return {"retryable": True, "kind": "error"}

        if skip_integrate:
            print(
                f"[FINALIZE EMPTY RELEASED] "
                f"task={task_id} "
                f"completed -> cleanup_ready"
            )
        else:
            # 2. Rebase + 导入主仓库 + Integration Branch
            guard("integrate", commit_head=own_head)
            # Authorized integrate input (own commit receipt this round, or
            # the already authorized task commit) and entry identity, so the
            # post-rebase result can be attributed to this call.
            authorized_input = own_head or task.get("commit") or None
            entry_identity = {key: task.get(key) for key in _INTEGRATION_IDENTITY_KEYS}
            result = ports.subprocess.run(
                [
                    ports.TASK_MANAGER,
                    "integrate",
                    task_id
                ],
                text=True,
                capture_output=True
            )

            if result.stdout.strip():
                print(result.stdout.strip())

            if result.returncode == 6:
                print(
                    f"[FINALIZE REFUSED] "
                    f"task={task_id} "
                    f"reason=integrate_rebase_conflict"
                )
                ports._escalate_finalize(
                    ports.get_task(task_id) or task,
                    "integrate_rebase_conflict",
                    ports._parse_integrate_result(result.stdout),
                )
                return {"retryable": False, "kind": "refused", "rc": 6, "reason": "integrate_rebase_conflict"}

            if result.returncode == 4:
                # M-7: two exit-4 sources carry different payloads:
                # remote_diverged (explicit HERDR_INTEGRATE_RESULT) vs
                # not_based_cleanly (relation check). Do not conflate.
                integrate_payload = ports._parse_integrate_result(result.stdout)
                if integrate_payload.get("result") == "remote_diverged":
                    reason = "integrate_remote_diverged"
                else:
                    reason = "integrate_not_based_cleanly"
                print(
                    f"[FINALIZE REFUSED] "
                    f"task={task_id} "
                    f"reason={reason}"
                )
                ports._escalate_finalize(
                    ports.get_task(task_id) or task,
                    reason,
                    integrate_payload,
                )
                return {"retryable": False, "kind": "refused", "rc": 4, "reason": reason}

            if result.returncode == 5:
                # H-1: main-repo tracked changes are persistent (never
                # self-heal). Escalate deterministically; retrying 5x
                # then auto-closing as delivered would silently drop
                # the never-integrated deliverable.
                integrate_payload = ports._parse_integrate_result(result.stdout)
                print(
                    f"[FINALIZE REFUSED] "
                    f"task={task_id} "
                    f"reason=integrate_main_dirty"
                )
                ports._escalate_finalize(
                    ports.get_task(task_id) or task,
                    "integrate_main_dirty",
                    integrate_payload,
                )
                return {"retryable": False, "kind": "refused", "rc": 5, "reason": "integrate_main_dirty"}

            if result.returncode == 7:
                integrate_payload = ports._parse_integrate_result(result.stdout)
                reason = "integrate_base_ref_missing"
                print(
                    f"[FINALIZE REFUSED] "
                    f"task={task_id} "
                    f"reason={reason}"
                )
                ports._escalate_finalize(
                    ports.get_task(task_id) or task,
                    reason,
                    integrate_payload,
                )
                return {"retryable": False, "kind": "refused", "rc": 7, "reason": reason}

            if result.returncode != 0:
                err_output = (result.stderr.strip() or result.stdout.strip())
                if "couldn't find remote ref" in err_output.lower() or "base_ref_missing" in err_output.lower():
                    integrate_payload = ports._parse_integrate_result(result.stdout)
                    reason = "integrate_base_ref_missing"
                    print(
                        f"[FINALIZE REFUSED] "
                        f"task={task_id} "
                        f"reason={reason}"
                    )
                    ports._escalate_finalize(
                        ports.get_task(task_id) or task,
                        reason,
                        integrate_payload or {"detail": err_output[:500]},
                    )
                    return {"retryable": False, "kind": "refused", "rc": result.returncode, "reason": reason}

                print(
                    f"[INTEGRATE ERROR] "
                    f"task={task_id} "
                    f"rc={result.returncode}: "
                    f"{err_output}"
                )
                ports._record_finalize_event(
                    task,
                    "finalize_integrate_error",
                    {
                        "rc": result.returncode,
                        "detail": err_output[:500],
                    },
                )
                return {"retryable": True, "kind": "error", "rc": result.returncode}

            task = ports.get_task(task_id)

            if not task or task.get("status") != "integrated":
                print(
                    f"[FINALIZE ERROR] "
                    f"task={task_id} "
                    f"did not reach integrated"
                )
                return {"retryable": True, "kind": "error"}

            # Later guards follow the confirmed result of this integrate, not
            # a stale receipt: a legal rebase's new HEAD must be attributable.
            confirmed_head = _settle_integration_result(
                ports, task, authorized_input, entry_identity)
            if not confirmed_head:
                payload = {
                    "task_id": task_id,
                    "authorized_input": authorized_input,
                    "reason": "integrate_result_unverified",
                }
                print(f"[FINALIZE REFUSED] task={task_id} integrate_result_unverified")
                ports._record_finalize_event(task, "finalize_integrate_result_unverified", payload)
                ports._escalate_finalize(task, "integrate_result_unverified", payload)
                return {"retryable": False, "kind": "refused",
                        "reason": "integrate_result_unverified"}
            own_head = confirmed_head

            # 3. 允许清理
            guard("cleanup_ready", commit_head=own_head)
            if not ports.set_task_status(
                task_id,
                "cleanup_ready"
            ):
                return {"retryable": True, "kind": "error"}

    # --------------------------------
    # 不需要 Git 集成
    # --------------------------------
    elif mode == "none":
        own_head, refusal = _preserve_none_mode_output(
            ports, task_id, task, own_head, guard
        )
        if refusal:
            return refusal
        guard("cleanup_ready", commit_head=own_head)
        if not ports.set_task_status(
            task_id,
            "cleanup_ready"
        ):
            return {"retryable": True, "kind": "error"}

    else:
        print(
            f"[FINALIZE ERROR] "
            f"task={task_id} "
            f"unknown integration_mode={mode}"
        )
        return {"retryable": False, "kind": "error"}

    # --------------------------------
    # 自动 Cleanup
    # --------------------------------
    guard("cleanup", commit_head=own_head)
    result = ports.subprocess.run(
        [
            ports.TASK_MANAGER,
            "cleanup",
            task_id
        ],
        text=True,
        capture_output=True
    )

    if result.stdout.strip():
        print(result.stdout.strip())

    if result.returncode != 0:
        print(
            f"[CLEANUP ERROR] "
            f"task={task_id}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
        return {"retryable": True, "kind": "error", "rc": result.returncode}

    print(
        f"[FINALIZED] task={task_id}"
    )

    # Task 最终 cleaned 后检查整个阶段是否已经完成。
    task = ports.get_task(task_id)

    if task:
        guard("stage_advance", commit_head=own_head)
        ports.enqueue_stage_advance(
            task
        )
    return {"retryable": False, "kind": "finalized"}


DEPENDENCIES = {'finalize_completed_task': ('TASK_MANAGER', '_empty_auto_releasable', '_escalate_finalize',
                             '_finalize_inflight',
                             '_get_store', '_parse_commit_result', '_parse_integrate_result',
                             '_record_finalize_event', 'enqueue_stage_advance',
                             'ensure_no_git_processes', 'get_task', 'maybe_complete_on_task_done',
                             'os', 'set_task_status', 'subprocess')}

__all__ = tuple(DEPENDENCIES)
