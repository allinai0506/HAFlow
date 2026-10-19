"""Candidate commit attribution and durable adoption CLI flow."""

import json
import os
import subprocess
import sys


def adopt_head_if_attributable(
    task_id, task, clone, allow_rebase=False, *, get_store, adoption_skew_seconds,
    git_head_sha, git_current_branch, git_is_ancestor, git_interval_commits,
    git_head_history, git_remote_contained_shas, coerce_epoch, set_status,
    classify_commit_state=None, explain=None,
):
    """空 index 收编:ADOPT 登记 HEAD,EMPTY exit 3,REFUSED exit 4。"""
    from herdr.git_adoption import classify_commit_state as classify, explain as explain_result

    if classify_commit_state is None:
        classify_commit_state = classify
    if explain is None:
        explain = explain_result
    head = git_head_sha(clone)
    baseline = task.get("baseline_commit")
    onto = task.get("onto_branch")
    branch = task.get("branch")
    created = coerce_epoch(task.get("created_at"))
    skew = adoption_skew_seconds()
    # P1: record which branch is actually checked out; the pure
    # classifier refuses when it is not the task's ownership branch.
    current_branch = git_current_branch(clone)
    # F-1: the task's own (and onto) branch never counts as foreign, so
    # a legitimate agent-side push of the task branch cannot refuse.
    exclude_branches = [b for b in (branch, onto) if b]
    from herdr.domain import get_domain_adapter
    _domain_adapter = get_domain_adapter(
        domain=task.get("domain"),
        task_type=task.get("task_type"),
        task=task,
    )
    effective_allow_rebase = _domain_adapter.should_allow_rebase(
        task,
        explicit_flag=allow_rebase,
        env_flag=os.environ.get("HERDR_ADOPT_ALLOW_REBASE", "").strip().lower() in ("1", "true", "yes"),
    )

    interval = None
    history = None
    ancestor = True
    remote_shas = set()
    enumeration_failed = False
    remote_probe_failed = False
    if baseline:
        ancestor = git_is_ancestor(clone, baseline, head)
        if ancestor and head:
            interval = git_interval_commits(clone, baseline, head)
            if interval is None:
                enumeration_failed = True
            elif interval:
                try:
                    probed = git_remote_contained_shas(
                        clone, [c.get("sha") for c in interval],
                        exclude_branches=exclude_branches)
                except (OSError, ValueError, RuntimeError,
                        subprocess.SubprocessError):
                    probed = None
                if probed is None:
                    # P1: unknown foreignness must fail closed, never be
                    # read as "no foreign commits".
                    remote_probe_failed = True
                else:
                    remote_shas = probed
        else:
            interval = []
    else:
        history = git_head_history(clone, head) if head else []
        if history is None:
            enumeration_failed = True
        elif history:
            try:
                probed = git_remote_contained_shas(
                    clone, [c.get("sha") for c in history],
                    exclude_branches=exclude_branches)
            except (OSError, ValueError, RuntimeError,
                    subprocess.SubprocessError):
                probed = None
            if probed is None:
                remote_probe_failed = True
            else:
                remote_shas = probed

    verdict, detail = classify_commit_state(
        baseline_commit=baseline,
        head=head,
        onto_branch=onto,
        branch=branch,
        task_id=task_id,
        created_at=created,
        interval_commits=interval,
        head_history=history,
        baseline_is_ancestor=ancestor,
        skew_seconds=skew,
        remote_shas=remote_shas,
        enumeration_failed=enumeration_failed,
        remote_probe_failed=remote_probe_failed,
        current_branch=current_branch,
        allow_rebase=effective_allow_rebase,
    )
    basis = detail.get("basis") or "none"
    reason = detail.get("reason") or "unknown"
    commits = int(detail.get("commits") or 0)
    noop_commits = list(detail.get("noop_commits") or [])
    from herdr import kernel

    if verdict == "adopt":
        kernel.update_task_metadata(
            task_id,
            {
                "commit": head,
                "commit_result": "adopted",
                "commit_basis": basis,
                "commit_baseline": detail.get("baseline"),
                "commit_commits": commits,
            },
            store=get_store(),
        )
        is_none_mode = (task.get("integration_mode") or ("none" if not task.get("branch") else "git")) == "none"
        preserve_status = ("completed", "cleanup_ready", "cleaned") if is_none_mode else ("cleanup_ready", "cleaned")
        target_status = task["status"] if task.get("status") in preserve_status else "committed"
        if task.get("status") != target_status:
            set_status(
                task_id,
                target_status
            )
        short_base = str(detail.get("baseline") or "")[:8]
        print(
            f"[ADOPTED] task={task_id} "
            f"commits={commits} baseline={short_base} basis={basis}"
        )
        print(f"[COMMITTED] {task_id}")
        print(f"[COMMIT] {head}")
        print(
            "HERDR_COMMIT_RESULT="
            + json.dumps(
                {
                    "task_id": task_id,
                    "result": "adopted",
                    "commit": head,
                    "commits": commits,
                    "baseline": detail.get("baseline"),
                    "basis": basis,
                    "noop_commits": noop_commits,
                },
                ensure_ascii=False,
            )
        )
        return

    if verdict == "empty":
        kernel.update_task_metadata(
            task_id,
            {
                "commit_result": "empty",
                "commit_head": head,
                "commit_basis": basis,
            },
            store=get_store(),
        )
        print(f"Task has no changes to commit: {task_id}")
        print(f"[EMPTY] task={task_id} head={head} basis={basis}")
        print(
            "HERDR_COMMIT_RESULT="
            + json.dumps(
                {
                    "task_id": task_id,
                    "result": "empty",
                    "head": head,
                    "basis": basis,
                },
                ensure_ascii=False,
            )
        )
        sys.exit(3)

    kernel.update_task_metadata(
        task_id,
        {
            "commit_result": "refused",
            "commit_refuse_reason": reason,
            "commit_head": head,
            "commit_basis": basis,
        },
        store=get_store(),
    )
    line = (
        f"[REFUSED] task={task_id} reason={reason} "
        f"head={head} basis={basis} detail={explain(verdict, detail)}"
    )
    print(line)
    print(line, file=sys.stderr)
    print(
        "HERDR_COMMIT_RESULT="
        + json.dumps(
            {
                "task_id": task_id,
                "result": "refused",
                "reason": reason,
                "head": head,
                "basis": basis,
            },
            ensure_ascii=False,
        )
    )
    sys.exit(4)
