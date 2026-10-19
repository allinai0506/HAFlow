"""Read-only task verification CLI reporting."""

import json
import os
import sys
from pathlib import Path


def verify_baseline(task_id, *, load_tasks, find_task, _git_head_sha,
                    _git_interval_commits, context_workspace_fingerprint,
                    current_workspace_fingerprint):
    data = load_tasks()
    task = find_task(data, task_id)

    if not task:
        print(f"Task not found: {task_id}")
        sys.exit(1)

    clone = task.get("clone_path")

    if not clone or not os.path.isdir(clone):
        print(f"Clone not found: {clone}")
        sys.exit(2)

    baseline = task.get("baseline_fingerprint")

    if baseline is None:
        print(
            f"Task has no baseline_fingerprint: "
            f"{task_id}"
        )
        sys.exit(3)

    if task.get("execution_mode") != "context":
        # Path-level union (plan-arch §2 AR-2, §3.4): verdict is driven by the
        # union of worktree-vs-HEAD paths and anchor..HEAD interval paths, not
        # by commit existence. Pure --allow-empty intervals carry no paths and
        # stay BASELINE_MATCH. An already-registered HEAD (task commit, or
        # the post-rebase integrated_commit) is registered deliverable, not
        # unregistered change, and stays BASELINE_MATCH. Anchor-less tasks
        # keep commit分量 closed (commits_ahead=0, basis="absent", AC3-8).
        anchor = task.get("baseline_commit")
        anchor_head = _git_head_sha(clone) if anchor else None
        committed_changes = []
        commits_ahead = 0
        basis = "absent"
        head_for_payload = anchor_head
        if anchor:
            basis = "baseline_commit"
            if anchor_head and anchor_head != anchor:
                # P2: integrate may rebase the deliverable (commit=A,
                # HEAD=integrated_commit=B). Either registered identity
                # counts; anything else is unregistered change. Status
                # guards the integrated identity so a stray
                # integrated_commit value on a non-integrated task cannot
                # launder unrelated HEADs.
                commit_registered = (
                    task.get("commit") == anchor_head
                    and task.get("commit_result") in ("adopted", "created")
                )
                integrated_registered = (
                    task.get("integrated_commit") == anchor_head
                    and task.get("status") in ("committed", "integrated")
                )
                registered = commit_registered or integrated_registered
                interval = _git_interval_commits(clone, anchor, anchor_head)
                if interval is None:
                    # M-4: enumeration failure must fail closed, never
                    # BASELINE_MATCH. Record an unknown committed entry.
                    commits_ahead = 0
                    if not registered:
                        committed_changes.append(
                            {
                                "type": "committed",
                                "path": "__enumeration_failed__",
                                "commit": anchor_head,
                                "baseline": anchor,
                                "current": anchor_head,
                            }
                        )
                else:
                    commits_ahead = len(interval)
                    if not registered:
                        unknown_shas = [
                            item.get("sha") for item in interval
                            if item.get("paths") is None
                        ]
                        if unknown_shas:
                            # F-3: uninspectable interval commits fail
                            # closed, never BASELINE_MATCH.
                            committed_changes.append(
                                {
                                    "type": "committed",
                                    "path": "__commit_paths_unknown__",
                                    "commit": anchor_head,
                                    "baseline": anchor,
                                    "current": anchor_head,
                                }
                            )
                        seen_paths = set()
                        for item in interval:
                            sha = item.get("sha")
                            for path in item.get("paths") or []:
                                if path not in seen_paths:
                                    seen_paths.add(path)
                                    committed_changes.append(
                                        {
                                            "type": "committed",
                                            "path": path,
                                            "commit": sha,
                                            "baseline": anchor,
                                            "current": anchor_head,
                                        }
                                    )
                        # Pure --allow-empty interval: no paths -> no committed
                        # entries; verdict falls through to worktree-only.
            else:
                commits_ahead = 0
        # Stash for the union below (worktree changes computed next).
        _vb_committed = committed_changes
        _vb_commits_ahead = commits_ahead
        _vb_basis = basis
        _vb_anchor = anchor
        _vb_head = head_for_payload
    else:
        _vb_committed = []
        _vb_commits_ahead = 0
        _vb_basis = "absent"
        _vb_anchor = None
        _vb_head = None

    current = (
        context_workspace_fingerprint(clone)
        if task.get("execution_mode") == "context"
        else current_workspace_fingerprint(clone)
    )

    changes = []

    for kind in ("tracked", "untracked"):
        before = baseline.get(kind, {})
        after = current.get(kind, {})

        all_paths = sorted(
            set(before.keys()) | set(after.keys())
        )

        for relpath in all_paths:
            old = before.get(
                relpath,
                "__NOT_PRESENT__"
            )
            new = after.get(
                relpath,
                "__NOT_PRESENT__"
            )

            if old != new:
                changes.append({
                    "type": kind,
                    "path": relpath,
                    "baseline": old,
                    "current": new
                })

    worktree_changed = bool(changes)
    if task.get("execution_mode") != "context":
        changes = changes + list(_vb_committed)
    else:
        _vb_committed = []
        _vb_commits_ahead = 0
        _vb_basis = "absent"
        _vb_anchor = None
        _vb_head = None

    payload = {
        "task_id": task_id,
        "baseline_match": not bool(changes),
        "changes": changes,
        "worktree_changed": worktree_changed,
        "commits_ahead": int(_vb_commits_ahead),
        "baseline_commit": _vb_anchor,
        "head": _vb_head,
        "basis": _vb_basis,
    }

    if not changes:
        print("BASELINE_MATCH")
        print("HERDR_BASELINE_RESULT=" + json.dumps(payload, ensure_ascii=False))
        return

    print("TASK_CHANGED:")

    for item in changes:
        print(
            f"- [{item['type']}] "
            f"{item['path']}"
        )

    print(
        "HERDR_BASELINE_RESULT="
        + json.dumps(
            payload,
            ensure_ascii=False
        )
    )


def verify_metrics(task_id, min_score=99.9, if_present=False, *,
                   load_tasks, find_task):
    """Verify that .herdr-loop/METRICS.json exists and meets required score threshold."""
    data = load_tasks()
    task = find_task(data, task_id)

    if not task:
        print(f"Task not found: {task_id}")
        sys.exit(1)

    clone = task.get("clone_path")
    if not clone or not os.path.isdir(clone):
        print(f"Clone not found: {clone}")
        sys.exit(2)

    loop_dir = Path(clone) / ".herdr-loop"
    metrics_file = loop_dir / "METRICS.json"

    if not metrics_file.exists():
        if if_present:
            print(f"TASK_METRICS: skipped (no .herdr-loop configured for task {task_id})")
            sys.exit(0)
        print(f"No metrics found for task {task_id} (.herdr-loop/METRICS.json missing)")
        sys.exit(3)

    try:
        metrics_data = json.loads(metrics_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Invalid metrics file: {e}")
        sys.exit(4)

    score = float(metrics_data.get("composite_score", 0.0))
    passed = score >= min_score

    print(f"TASK_METRICS: score={score:.2f} (target>={min_score:.2f}) passed={passed}")
    print(
        "HERDR_METRICS_RESULT="
        + json.dumps(
            {
                "task_id": task_id,
                "score": score,
                "target": min_score,
                "passed": passed,
                "metrics": metrics_data,
            },
            ensure_ascii=False,
        )
    )

    if not passed:
        sys.exit(5)


def differential_test(args):
    """Evaluate candidate test failures against the selected baseline."""
    from herdr.domain import get_domain_adapter

    candidate_path = getattr(args, "candidate_output", None)
    if not candidate_path:
        print("ERROR: --candidate-output is required for differential-test", file=sys.stderr)
        sys.exit(2)
    if not os.path.isfile(candidate_path):
        print("ERROR: candidate output file not found: " + str(candidate_path), file=sys.stderr)
        sys.exit(1)

    base_output = None
    base_output_path = getattr(args, "base_output", None)
    if base_output_path:
        if not os.path.isfile(base_output_path):
            print("ERROR: base output file not found: " + str(base_output_path), file=sys.stderr)
            sys.exit(1)
        base_output = Path(base_output_path).read_text(encoding="utf-8", errors="replace")

    base_snapshot_path = getattr(args, "base_snapshot", None)
    if base_snapshot_path and not os.path.isfile(base_snapshot_path):
        print("ERROR: base snapshot file not found: " + str(base_snapshot_path), file=sys.stderr)
        sys.exit(1)

    candidate_output = Path(candidate_path).read_text(encoding="utf-8", errors="replace")
    adapter = get_domain_adapter(domain=getattr(args, "domain", "software"))
    result = adapter.evaluate_differential_tests(
        candidate_output=candidate_output,
        base_output=base_output,
        base_snapshot=base_snapshot_path,
        candidate_exit_code=getattr(args, "candidate_exit_code", 0),
        base_exit_code=getattr(args, "base_exit_code", 0),
    )

    verdict = result.get("verdict", "pass")
    delta_count = len(result.get("delta_failures", []))
    baseline_count = len(result.get("pre_existing_failures", []))
    print(f"DIFFERENTIAL_TEST: verdict={verdict} delta={delta_count} pre_existing={baseline_count}")
    if result.get("warning"):
        print(f"WARNING: {result['warning']}")
    print("HERDR_DIFFERENTIAL_TEST_RESULT=" + json.dumps(result, ensure_ascii=False))
    if verdict != "pass":
        sys.exit(1)
