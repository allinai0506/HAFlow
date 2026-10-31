"""Git commit and candidate-attribution CLI orchestration."""

import json
import os
import subprocess
import sys
from pathlib import Path


def _default_is_launcher_owned(clone):
    """未注入时的默认 launcher-owned 判定（clone 相对路径）。"""
    from herdr.repo_hygiene import is_launcher_owned
    return lambda item: is_launcher_owned(item, clone)


def commit_task(
    task_id,
    message=None,
    allow_rebase=False,
    *,
    load_tasks,
    find_task,
    get_store,
    is_internal_untracked,
    is_launcher_owned=None,
    adopt_head_if_attributable,
    set_status,
    run_commit_index_command,
):
    data = load_tasks()
    task = find_task(data, task_id)

    if not task:
        print(f"Task not found: {task_id}")
        sys.exit(1)

    if task.get("execution_mode") == "context":
        print(
            f"Context-mode task has no git commit path; "
            f"deliverables live in task workspace: {task_id}"
        )
        sys.exit(2)

    from herdr import wrapup_delivery
    wrapup_task = wrapup_delivery.begin(get_store(), task) if (task.get("node") or task.get("stage")) == "wrapup" else None
    if wrapup_task:
        task = wrapup_task
        if not task.get("wrapup_delivery"):
            print("[COMMITTED] " + task_id + " (no candidate change)")
            return
        if ((task.get("wrapup_delivery") or {}).get("phase") == "committed"
                and subprocess.check_output(["git", "-C", task["clone_path"], "rev-parse", "HEAD"], text=True).strip() == task.get("commit")
                and not wrapup_delivery.has_worktree_output(task)):
            print("[COMMITTED] " + task_id + " (already registered)")
            return
        if (task.get("wrapup_delivery") or {}).get("phase") == "integrated":
            print("[COMMITTED] " + task_id + " (already integrated)")
            return

    if task["status"] != "completed" and not wrapup_task:
        existing = task.get("commit")
        status = task.get("status")

        def _print_idempotent_commit(cid):
            print(f"[COMMITTED] {task_id}")
            print(f"[COMMIT] {cid}")
            stored_result = task.get("commit_result")
            if stored_result not in ("created", "adopted"):
                stored_result = "created"
            print(
                "HERDR_COMMIT_RESULT="
                + json.dumps(
                    {
                        "task_id": task_id,
                        "result": stored_result,
                        "commit": cid,
                        "status": status,
                    },
                    ensure_ascii=False,
                )
            )

        if status == "integrated":
            # Once integrated, a candidate is final; new commits cannot be made here.
            clone_path = task.get("clone_path")
            has_uncommitted = False
            if clone_path and os.path.exists(clone_path):
                status_res = subprocess.run(
                    [
                        "git",
                        "-C",
                        clone_path,
                        "status",
                        "--porcelain",
                        "--untracked-files=normal",
                    ],
                    capture_output=True,
                    text=True,
                )
                if status_res.returncode != 0:
                    print(
                        f"Failed to inspect worktree status for task (rc={status_res.returncode}): {task_id}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                from herdr.repo_hygiene import teardown_output_diagnosis

                diag = teardown_output_diagnosis(status_res.stdout, task_id=task_id)
                has_dirty = bool(diag.get("reason"))

                head_res = subprocess.run(
                    ["git", "-C", clone_path, "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                )
                if head_res.returncode != 0:
                    print(
                        f"Failed to resolve HEAD for task (rc={head_res.returncode}): {task_id}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                head_sha = head_res.stdout.strip()
                expected_commit = task.get("integrated_commit") or task.get("commit")
                head_diverged = bool(
                    head_sha and expected_commit and head_sha != expected_commit
                )
                has_uncommitted = has_dirty or head_diverged
            if has_uncommitted:
                print(
                    f"Task is already integrated; post-integration changes cannot be committed directly "
                    f"(reintegration required): {task_id}"
                )
                sys.exit(2)
            if existing:
                _print_idempotent_commit(existing)
                return
            print(f"Task cannot commit from status: {status}")
            sys.exit(2)

        if status in ("cleanup_ready", "cleaned"):
            # Only non-git tasks (e.g. integration_mode="none") with uncommitted output
            # are allowed to perform a preservation commit in cleanup_ready/cleaned status.
            clone_path = task.get("clone_path")
            has_uncommitted = False
            if clone_path and os.path.exists(clone_path):
                status_res = subprocess.run(
                    [
                        "git",
                        "-C",
                        clone_path,
                        "status",
                        "--porcelain",
                        "--untracked-files=normal",
                    ],
                    capture_output=True,
                    text=True,
                )
                if status_res.returncode != 0:
                    print(
                        f"Failed to inspect worktree status for task (rc={status_res.returncode}): {task_id}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                from herdr.repo_hygiene import teardown_output_diagnosis

                diag = teardown_output_diagnosis(status_res.stdout, task_id=task_id)
                has_dirty = bool(diag.get("reason"))

                head_res = subprocess.run(
                    ["git", "-C", clone_path, "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                )
                if head_res.returncode != 0:
                    print(
                        f"Failed to resolve HEAD for task (rc={head_res.returncode}): {task_id}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                head_sha = head_res.stdout.strip()
                expected_commit = task.get("integrated_commit") or task.get("commit")
                head_diverged = bool(
                    head_sha and expected_commit and head_sha != expected_commit
                )
                has_uncommitted = has_dirty or head_diverged

            if task.get("integration_mode") == "git":
                if has_uncommitted:
                    print(
                        f"Task has completed git lifecycle; "
                        f"post-integration changes cannot be committed directly: {task_id}"
                    )
                    sys.exit(2)
                if existing:
                    _print_idempotent_commit(existing)
                    return
                print(f"Task cannot commit from status: {status}")
                sys.exit(2)
            else:
                if has_uncommitted:
                    pass
                elif existing:
                    _print_idempotent_commit(existing)
                    return
                else:
                    print(f"Task cannot commit from status: {status}")
                    sys.exit(2)

        elif status == "committed" and existing:
            _print_idempotent_commit(existing)
            return

        else:
            print(f"Task cannot commit from status: " f"{task['status']}")
            sys.exit(2)

    clone = task.get("clone_path")

    if not clone:
        print("Task missing clone_path")
        sys.exit(1)

    clone = os.path.realpath(os.path.expanduser(clone))

    baseline_untracked = set(task.get("baseline_untracked", []))

    # 当前 untracked
    result = subprocess.run(
        ["git", "-C", clone, "ls-files", "--others", "--exclude-standard", "-z"],
        text=True,
        capture_output=True,
    )

    if result.returncode != 0:
        print(result.stderr.strip())
        sys.exit(1)

    current_untracked = {item for item in result.stdout.split("\0") if item}

    def _is_nested_git_dir(rel_item: str) -> bool:
        target = Path(clone) / rel_item
        if (target / ".git").exists():
            return True
        try:
            cur = target.parent
            clone_path = Path(clone).resolve()
            while cur.resolve() != clone_path and cur != cur.parent:
                if (cur / ".git").exists():
                    return True
                cur = cur.parent
        except Exception:
            pass
        return False

    launcher_owned = is_launcher_owned or _default_is_launcher_owned(clone)
    # 只允许提交 Task 新产生的 untracked（内部控制文件与嵌套子仓库不属于业务交付）
    task_untracked = sorted(
        item
        for item in current_untracked - baseline_untracked
        if not (is_internal_untracked(item) and launcher_owned(item))
        and not _is_nested_git_dir(item)
    )

    # tracked 文件在 Task 创建前要求 clean，
    # 因此当前 tracked 修改全部属于该 Task。
    staged_update = run_commit_index_command(
        task_id, clone, ["git", "-C", clone, "add", "-u"]
    )
    if staged_update.returncode:
        raise subprocess.CalledProcessError(
            staged_update.returncode,
            staged_update.args,
            staged_update.stdout,
            staged_update.stderr,
        )

    if task_untracked:
        staged_new = run_commit_index_command(
            task_id, clone, ["git", "-C", clone, "add", "--", *task_untracked]
        )
        if staged_new.returncode:
            raise subprocess.CalledProcessError(
                staged_new.returncode,
                staged_new.args,
                staged_new.stdout,
                staged_new.stderr,
            )

    if task.get("delivery_contract") is not None:
        from herdr.task_delivery import check_delivery

        delivery = check_delivery(task_id, store=get_store())
        if delivery["status"] != "ready":
            print(
                "HERDR_COMMIT_RESULT="
                + json.dumps(
                    {
                        "task_id": task_id,
                        "result": (
                            "delivery_blocked"
                            if delivery["status"] == "blocked"
                            else "delivery_unknown"
                        ),
                        "delivery": delivery,
                    },
                    ensure_ascii=False,
                )
            )
            sys.exit(7)

    # 必须确实存在 staged changes
    staged = subprocess.run(
        ["git", "-C", clone, "diff", "--cached", "--quiet"], check=False
    )

    if staged.returncode == 0:
        # 空 index:可能是 Agent 直接提交后的干净工作区,尝试收编 HEAD。
        # 真空任务仍 exit 3,无法归属的推进 exit 4,状态一律不变(除收编成功)。
        adoption_task = dict(task, baseline_commit=task["wrapup_delivery"]["parent_sha"]) if wrapup_task else task
        adopt_head_if_attributable(task_id, adoption_task, clone, allow_rebase=allow_rebase)
        if wrapup_task:
            wrapup_delivery.committed(get_store(), task_id)
        return

    if message is None:
        message = f"task: {task_id}"

    result = run_commit_index_command(
        task_id, clone, ["git", "-C", clone, "commit", "-m", message]
    )

    if result.returncode != 0:
        print(f"Commit failed: {task_id}")
        sys.exit(result.returncode)

    commit_hash = subprocess.check_output(
        ["git", "-C", clone, "rev-parse", "HEAD"], text=True
    ).strip()

    # 保存 commit hash
    from herdr import kernel

    kernel.update_task_metadata(
        task_id,
        {
            "commit": commit_hash,
            "commit_result": "created",
            "commit_basis": "baseline_commit",
        },
        store=get_store(),
    )

    is_none_mode = (
        task.get("integration_mode") or ("none" if not task.get("branch") else "git")
    ) == "none"
    preserve_status = (
        ("completed", "cleanup_ready", "cleaned")
        if is_none_mode
        else ("cleanup_ready", "cleaned")
    )
    target_status = (
        task["status"] if task.get("status") in preserve_status else "committed"
    )
    if wrapup_task:
        wrapup_delivery.committed(get_store(), task_id)
    elif task.get("status") != target_status:
        set_status(task_id, target_status)

    print(f"[COMMITTED] {task_id}")
    print(f"[COMMIT] {commit_hash}")
    print(
        "HERDR_COMMIT_RESULT="
        + json.dumps(
            {
                "task_id": task_id,
                "result": "created",
                "commit": commit_hash,
                "basis": "staged",
            },
            ensure_ascii=False,
        )
    )


def cleanup_task(task_id, *, load_tasks, find_task, get_store, set_status):
    data = load_tasks()
    task = find_task(data, task_id)

    if not task:
        print(f"Task not found: {task_id}")
        sys.exit(1)

    if task["status"] != "cleanup_ready":
        print(f"Task cannot cleanup from status: " f"{task['status']}")
        sys.exit(2)

    # Logical cleanup only.
    # Workspace / Stage Tab / Task Pane / Task Clone are retained.
    from herdr import kernel

    kernel.update_task_metadata(
        task_id,
        {
            "resource_retention": "persistent",
            "pane_retained": True,
            "clone_retained": True,
        },
        store=get_store(),
    )

    set_status(task_id, "cleaned")

    print(f"[PANE RETAINED] {task.get('pane_id')}")
    print(f"[CLONE RETAINED] {task.get('clone_path')}")
    print(f"[LOGICAL CLEANUP] {task_id} -> cleaned")


def purge_task(
    task_id,
    *,
    load_tasks,
    find_task,
    get_store,
    teardown_blocking_statuses,
    delete_clone_safely,
    run_command,
    path_exists,
):
    data = load_tasks()
    task = find_task(data, task_id)

    if not task:
        print(f"Task not found: {task_id}")
        sys.exit(1)

    if task.get("status") in teardown_blocking_statuses:
        print(
            f"Task can only purge from a settled status, "
            f"current={task.get('status')}"
        )
        sys.exit(2)

    pane_id = task.get("pane_id")
    clone_path = task.get("clone_path")

    if pane_id:
        run_command(["herdr", "pane", "close", pane_id], text=True, capture_output=True)
        print(f"[PANE PURGED] {pane_id}")

    if clone_path:
        if delete_clone_safely(clone_path):
            print(f"[CLONE PURGED] {clone_path}")
        elif path_exists(clone_path):
            print(f"Unsafe clone path, refusing delete: " f"{clone_path}")
            sys.exit(3)

    from herdr import kernel

    kernel.update_task_metadata(
        task_id,
        {
            "pane_retained": False,
            "clone_retained": False,
            "resource_retention": "purged",
        },
        store=get_store(),
    )

    print(f"[PURGED] {task_id}")


def finalize_task(
    task_id,
    force=False,
    purge_clones=False,
    *,
    load_tasks,
    find_task,
    teardown_blocking_statuses,
    finalize_one,
):
    data = load_tasks()
    task = find_task(data, task_id)

    if not task:
        print(f"Task not found: {task_id}")
        sys.exit(1)

    status = task.get("status")
    blocked = status in teardown_blocking_statuses
    if blocked or (status == "failed" and not force):
        print(
            f"Task cannot finalize while {status}; "
            "only settled tasks can be torn down "
            "(use --force for failed tasks)."
        )
        sys.exit(2)

    row = finalize_one(task, purge_clones=purge_clones)

    if row.get("action") == "retained":
        print("[FINALIZE ABORT] " + json.dumps(row, ensure_ascii=False))
        sys.exit(2)

    print(
        f"[FINALIZE] {task_id} status={row['status']} "
        f"pane_closed={row['pane_closed']}"
    )
    if row["evidence"]:
        print(f"[EVIDENCE] {row['evidence']}")
    if row["clone_deleted"]:
        print(f"[CLONE PURGED] {task.get('clone_path')}")
    elif row["clone_retained_reason"]:
        print(
            f"[CLONE RETAINED] {task.get('clone_path')} "
            f"({row['clone_retained_reason']}; "
            "use --purge-clones to force)"
        )
    print(f"[FINALIZED] {task_id}")
    return row


def create_pull_request(args, *, get_store):
    from herdr.pr_delivery import create_task_pr

    body = ""
    if args.body_file:
        path = Path(args.body_file).expanduser()
        if path.stat().st_size > 65536:
            raise ValueError("PR body exceeds publication budget")
        body = path.read_text()
    result = create_task_pr(
        get_store(), args.task_id, title=args.title, body=body, draft=args.draft
    )
    print(json.dumps(result, ensure_ascii=False))


def merge_pull_request(args, *, get_store):
    from herdr.pr_delivery import merge_task_pr

    result = merge_task_pr(
        get_store(),
        args.task_id,
        pr_number=args.number,
        merge_method=getattr(args, "method", "merge"),
    )
    print(json.dumps(result, ensure_ascii=False))
