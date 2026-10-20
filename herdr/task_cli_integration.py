"""Task delivery integration and publication CLI orchestration."""

import json
import os
import subprocess
import sys


def integrate_task(task_id, *, load_tasks, find_task, get_store,
                   ensure_branch_available, branch_ownership_error, set_status,
                   clear_finalize_escalation, publish_integrated_task,
                   git_head_sha, git_is_ancestor, current_workspace_fingerprint,
                   project_for_workflow):
    data = load_tasks()
    task = find_task(data, task_id)

    if not task:
        print(f"Task not found: {task_id}")
        sys.exit(1)

    if task.get("execution_mode") == "context":
        print(
            f"Context-mode task has no git integrate path; "
            f"deliverables live in task workspace: {task_id}"
        )
        sys.exit(2)

    from herdr import wrapup_delivery
    workflow = get_store().get_workflow(task["workflow_id"]) or {}
    wrapup_phase = task.get("wrapup_delivery") or {}
    wrapup_pending = (wrapup_delivery.enabled(task, workflow.get("config") or {})
                      and task.get("status") in wrapup_delivery.ACTIVE
                      and wrapup_phase.get("phase") in {"committed", "integrated"}
                      and wrapup_phase.get("run_id") == task.get("run_id")
                      and wrapup_phase.get("execution_id") == workflow.get("execution_id"))
    if task["status"] == "integrated" or (wrapup_pending and wrapup_delivery.integrated(task, workflow)):
        if task.get("commit") and task.get("integration_ref"):
            clear_finalize_escalation(task_id)
            publish_integrated_task(task_id)
            print(f"[INTEGRATED] {task_id} (already)")
            print(f"[HERDR REF] {task.get('integration_ref')}")
            print(
                "HERDR_INTEGRATE_RESULT="
                + json.dumps(
                    {
                        "task_id": task_id,
                        "result": "already_integrated",
                        "integration_ref": task.get("integration_ref"),
                    },
                    ensure_ascii=False,
                )
            )
            return
        print(
            f"Task cannot integrate from status: "
            f"{task['status']}"
        )
        sys.exit(2)

    if task["status"] != "committed" and not wrapup_pending:
        print(
            f"Task cannot integrate from status: "
            f"{task['status']}"
        )
        sys.exit(2)

    clone = task.get("clone_path")
    branch = task.get("branch")

    if not clone or not branch:
        print("Task missing clone_path or branch")
        sys.exit(1)

    clone = os.path.realpath(
        os.path.expanduser(clone)
    )

    checked_out = subprocess.run(['git', '-C', clone, 'symbolic-ref', '--quiet', '--short', 'HEAD'],
                                 text=True, capture_output=True, check=False, timeout=10)
    if checked_out.returncode or checked_out.stdout.strip() != branch:
        print('[INTEGRATE REJECTED] clone branch ownership differs from Task')
        sys.exit(2)

    baseline = task.get("baseline_fingerprint")
    if baseline is None:
        print(
            f"Task has no baseline_fingerprint: "
            f"{task_id}"
        )
        sys.exit(3)

    current = current_workspace_fingerprint(clone)

    if current != baseline:
        print(
            "Clone working tree differs from "
            "Task baseline after commit:"
        )

        for kind in ("tracked", "untracked"):
            before = baseline.get(kind, {})
            after = current.get(kind, {})
            for relpath in sorted(set(before) | set(after)):
                if (
                    before.get(relpath, "__NOT_PRESENT__")
                    != after.get(relpath, "__NOT_PRESENT__")
                ):
                    print(f"- [{kind}] {relpath}")

        sys.exit(3)

    print("[BASELINE RESTORED] working tree matches Task baseline")

    project = project_for_workflow(
        task.get("workflow_id")
    ) or {}

    base_branch = (
        task.get("base_branch")
        or project.get("base_branch")
        or "dev"
    )

    main_repo = (
        task.get("source_repo")
        or project.get("project_root")
    )

    if not main_repo:
        print("Task missing source_repo/project_root")
        sys.exit(4)

    main_repo = os.path.realpath(
        os.path.expanduser(main_repo)
    )

    tracked_status = subprocess.check_output(
        [
            "git",
            "-C",
            main_repo,
            "status",
            "--porcelain",
            "--untracked-files=no"
        ],
        text=True
    ).strip()

    if tracked_status:
        from herdr import repo_hygiene as _hygiene

        try:
            _email = subprocess.check_output(
                ["git", "-C", main_repo, "config", "user.email"],
                text=True,
            ).strip()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            _email = ""
        diagnosis = _hygiene.diagnose_main_dirty(
            porcelain=tracked_status,
            configured_email=_email,
            task_branch=branch,
            task_id=task_id,
        )
        print("Main repository has tracked changes")
        print(tracked_status)
        print(f"blocked_files={diagnosis['blocked_files']}")
        print(f"blocked_total={diagnosis['blocked_total']}")
        print(f"owner={diagnosis['owner']} owner_source={diagnosis['owner_source']}")
        print(f"remediation_cmd={diagnosis['remediation_cmd']}")
        print(
            "HERDR_INTEGRATE_RESULT="
            + json.dumps(
                {
                    "task_id": task_id,
                    "result": "main_dirty",
                    "tracked_status": tracked_status[:2000],
                    "blocked_files": diagnosis["blocked_files"],
                    "blocked_total": diagnosis["blocked_total"],
                    "owner": diagnosis["owner"],
                    "owner_source": diagnosis["owner_source"],
                    "task_branch": diagnosis["task_branch"],
                    "remediation_cmd": diagnosis["remediation_cmd"],
                },
                ensure_ascii=False,
            )
        )
        sys.exit(5)


    try:
        ensure_branch_available(
            branch,
            data.get("tasks", []),
            task_id=task_id,
        )
    except branch_ownership_error as exc:
        print(f"[BRANCH OWNERSHIP ERROR] {exc}")
        sys.exit(5)

    from herdr import kernel, recovery_store
    authority, _, _ = recovery_store.read_snapshot(get_store().db_path, task['workflow_id'])
    parent_episode = task.get('integration_publication_episode_id')
    if parent_episode is None:
        parent_episode = task.get('candidate_episode_id')
        if parent_episode is None and not authority['candidate_episode_id']:
            parent_episode = 0  # Initial publication has no predecessor episode.
        if parent_episode is None and git_head_sha(clone) == authority.get('candidate_sha'):
            # An explicit manual freeze already published this exact result.
            parent_episode = authority['candidate_episode_id']
        if parent_episode is None:
            print('[INTEGRATE REJECTED] publication episode unknown; evidence migration required')
            sys.exit(2)
        if parent_episode != authority['candidate_episode_id'] and git_head_sha(clone) != authority.get('candidate_sha'):
            print('[INTEGRATE REJECTED] publication episode changed')
            sys.exit(2)
        kernel.update_task_metadata(task_id, {'integration_publication_episode_id': parent_episode}, store=get_store())

    # Agent init anchors are local worktree baselines, not remote branches.
    # Fetch a task-scoped ref while holding the existing source Git lock.
    if base_branch.startswith("agent/") and base_branch.endswith("-init"):
        base_ref = f"refs/herdr/bases/{task_id}"
        subprocess.run(
            ["git", "-C", clone, "fetch", main_repo, f"refs/heads/{base_branch}:{base_ref}"],
            check=True,
        )
    else:
        base_ref = f"origin/{base_branch}"
        subprocess.run(["git", "-C", clone, "fetch", "origin", base_branch], check=True)

    # FR-8 pre-check: remote task branch equivalence (L-4). Best-effort fetch
    # of the同名远端分支; if it already contains HEAD, the deliverable is
    # already published -> idempotent success path (no extra push). If it
    # exists but diverges from HEAD, fail once with a clear reason instead
    # of entering a retry loop.
    subprocess.run(
        ["git", "-C", clone, "fetch", "origin", branch],
        text=True,
        capture_output=True,
        check=False,
    )
    remote_ref = f"origin/{branch}"
    remote_exists = subprocess.run(
        ["git", "-C", clone, "show-ref", "--verify", "--quiet",
         f"refs/remotes/{remote_ref}"],
        check=False,
    ).returncode == 0
    if remote_exists and task.get('onto_branch') != branch:
        local_head = git_head_sha(clone)
        head_in_remote = (
            git_is_ancestor(clone, local_head, remote_ref)
            if local_head else False
        )
        remote_in_head = (
            git_is_ancestor(clone, remote_ref, local_head)
            if local_head else False
        )
        if head_in_remote:
            print(
                f"[INTEGRATE REMOTE EQUIVALENT] task={task_id} "
                f"branch={branch} already contains HEAD; idempotent"
            )
        elif not remote_in_head:
            print(
                f"[INTEGRATE REMOTE DIVERGED] task={task_id} "
                f"branch={branch} diverged from {remote_ref}; "
                "needs human confirmation"
            )
            print(
                "HERDR_INTEGRATE_RESULT="
                + json.dumps(
                    {
                        "task_id": task_id,
                        "result": "remote_diverged",
                        "branch": branch,
                        "remote": remote_ref,
                    },
                    ensure_ascii=False,
                )
            )
            sys.exit(4)

    integration_base_sha = subprocess.check_output(
        ['git', '-C', clone, 'rev-parse', base_ref], text=True).strip()
    if wrapup_pending:
        task = wrapup_delivery.prepare_rebase(get_store(), task, integration_base_sha)
    integration_head_before_rebase = git_head_sha(clone)

    rebase = subprocess.run(
        [
            "git",
            "-C",
            clone,
            "rebase",
            integration_base_sha
        ],
        text=True,
        capture_output=True,
        check=False
    )

    if rebase.returncode != 0:
        subprocess.run(
            ["git", "-C", clone, "rebase", "--abort"],
            text=True,
            capture_output=True,
            check=False,
        )
        print(
            f"[INTEGRATE REBASE CONFLICT] task={task_id} "
            f"branch={branch} base={base_ref}: "
            f"{(rebase.stderr.strip() or rebase.stdout.strip())[:500]}"
        )
        print(
            "HERDR_INTEGRATE_RESULT="
            + json.dumps(
                {
                    "task_id": task_id,
                    "result": "rebase_conflict",
                    "branch": branch,
                    "base_branch": base_branch,
                },
                ensure_ascii=False,
            )
        )
        sys.exit(6)

    relation = subprocess.check_output(
        [
            "git",
            "-C",
            clone,
            "rev-list",
            "--left-right",
            "--count",
            f"{integration_base_sha}...HEAD"
        ],
        text=True
    ).strip().split()

    if len(relation) != 2 or relation[0] != "0":
        print(
            "Task branch is not based cleanly "
            f"on latest {base_ref}"
        )
        print(
            "HERDR_INTEGRATE_RESULT="
            + json.dumps(
                {
                    "task_id": task_id,
                    "result": "not_based_cleanly",
                    "branch": branch,
                    "base_branch": base_branch,
                    "relation": relation,
                },
                ensure_ascii=False,
            )
        )
        sys.exit(4)

    # Rebase changes identity even if exporting the ref later fails. Persist
    # the immutable result before transport; retry uses this new commit.
    from herdr import kernel
    integrated_commit = git_head_sha(clone)
    task = kernel.update_task_metadata(task_id, {
        'commit': integrated_commit,
        'integration_head_before_rebase': integration_head_before_rebase,
        'integration_base_sha': integration_base_sha,
    }, store=get_store(), expected_version=task.get('version') if wrapup_pending else None)
    herdr_ref = f"refs/herdr/tasks/{task_id}"

    subprocess.run(
        [
            "git",
            "-C",
            main_repo,
            "fetch",
            clone,
            f"{branch}:{herdr_ref}",
            "--force"
        ],
        check=True
    )

    integration_branch = (
        f"herdr/integration-{task_id}"
    )

    subprocess.run(
        [
            "git",
            "-C",
            main_repo,
            "update-ref",
            f"refs/heads/{integration_branch}",
            herdr_ref,
        ],
        check=True,
    )

    if wrapup_pending:
        subprocess.run(['git', '-C', main_repo, 'update-ref',
            'refs/heads/' + wrapup_delivery.publication_branch(task, integrated_commit), integrated_commit], check=True)

    from herdr import kernel
    integrated_commit = git_head_sha(clone)
    kernel.update_task_metadata(task_id, {
        "integration_branch": integration_branch,
        "integration_ref": herdr_ref,
        "base_branch": base_branch,
        "source_repo": main_repo,
        "integrated_commit": integrated_commit,
        "commit": integrated_commit,
        "integration_head_before_rebase": integration_head_before_rebase,
    }, store=get_store(), expected_version=task.get('version') if wrapup_pending else None)

    if wrapup_pending:
        wrapup_delivery.finish_integration(get_store(), task_id)
    else:
        set_status(task_id, "integrated")

    clear_finalize_escalation(task_id)
    publish_integrated_task(task_id)
    print(f"[INTEGRATED] {task_id}")
    print(f"[PROJECT] {main_repo}")
    print(f"[BASE BRANCH] {base_branch}")
    print(f"[TASK BRANCH] {branch}")
    print(f"[INTEGRATION BRANCH] {integration_branch}")
    print(f"[HERDR REF] {herdr_ref}")
