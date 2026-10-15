"""Assemble task-owned Git deliveries without checking out the source repository."""

import hashlib
import json
import os
import re
import subprocess
import time


def delivery_inventory(workflow, tasks, source_node):
    """Stable identity of every current Git delivery in the source node."""
    owned = [
        t
        for t in tasks
        if t.get("workflow_id") == workflow["workflow_id"]
        and (t.get("node") or t.get("stage")) == source_node
        and (
            not t.get("execution_id")
            or t["execution_id"] == workflow.get("execution_id")
        )
        and t.get("status") != "superseded"
        and not t.get("superseded_by")
        and t.get("integration_mode") == "git"
    ]
    if len(owned) > 64:
        raise ValueError("candidate delivery inventory exceeds budget")
    inventory = []
    for task in owned:
        sha = task.get("integrated_commit")
        if (
            not workflow.get("execution_id")
            or task.get("execution_id") != workflow["execution_id"]
            or not task.get("run_id")
            or task.get("commit") != sha
            or not re.fullmatch(r"[0-9a-f]{40}", sha or "")
            or task.get("status") not in {"integrated", "cleanup_ready", "cleaned"}
            or task.get("stage_verdict") == "blocked"
            or task.get("finalize_escalated")
            or task.get("integration_ref") != f"refs/herdr/tasks/{task['task_id']}"
        ):
            raise ValueError("candidate delivery identity incomplete")
        inventory.append(
            {
                "task_id": task["task_id"],
                "run_id": task["run_id"],
                "commit": sha,
                "integration_ref": task["integration_ref"],
            }
        )
    return sorted(inventory, key=lambda t: t["task_id"])


def assemble_candidate(repo, inventory, *, preferred="", allow_merge=True, timeout=10):
    """Merge exact immutable task refs; conflicts never publish a candidate.

    Object writes and a content-keyed private ref do not touch HEAD, index or
    the working tree. Identical inventory produces the same merge commits.
    """
    if not inventory or len(inventory) > 64:
        raise ValueError("candidate requires bounded deliveries")
    deadline = time.monotonic() + timeout
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="HAFlow",
        GIT_AUTHOR_EMAIL="haflow@localhost",
        GIT_COMMITTER_NAME="HAFlow",
        GIT_COMMITTER_EMAIL="haflow@localhost",
        GIT_AUTHOR_DATE="@1 +0000",
        GIT_COMMITTER_DATE="@1 +0000",
    )

    def git(*args, check=True, input=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("candidate assembly timed out")
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            text=True,
            capture_output=True,
            input=input,
            env=env,
            timeout=remaining,
        )
        if check and result.returncode:
            raise ValueError("candidate Git operation failed: " + args[0])
        return result

    commits = [t["commit"] for t in inventory]
    for task in inventory:
        if (
            git(
                "rev-parse", "--verify", task["integration_ref"] + "^{commit}"
            ).stdout.strip()
            != task["commit"]
        ):
            raise ValueError("candidate task ref changed")

    def ancestor(left, right):
        result = git("merge-base", "--is-ancestor", left, right, check=False)
        if result.returncode not in (0, 1):
            raise ValueError("candidate ancestry unknown")
        return result.returncode == 0

    def covers(commit, head):
        if ancestor(commit, head):
            return True
        merged = git("merge-tree", "--write-tree", head, commit, check=False)
        return (
            merged.returncode == 0
            and merged.stdout.splitlines()[0]
            == git("rev-parse", head + "^{tree}").stdout.strip()
        )

    if preferred and all(covers(commit, preferred) for commit in commits):
        return preferred
    if not allow_merge:
        raise ValueError("candidate assembly requires a current publication")
    identity = hashlib.sha256(
        json.dumps(
            {"deliveries": inventory, "preferred": preferred}, sort_keys=True
        ).encode()
    ).hexdigest()
    ref = "refs/herdr/candidates/" + identity
    cached = git("rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False)
    if cached.returncode == 0:
        head = cached.stdout.strip()
        if all(covers(commit, head) for commit in commits):
            return head
        raise ValueError("candidate cache does not cover deliveries")
    head = preferred or commits[0]
    for commit in commits:
        if ancestor(commit, head):
            continue
        if ancestor(head, commit):
            head = commit
            continue
        tree = git("merge-tree", "--write-tree", head, commit).stdout.splitlines()[0]
        head = git(
            "-c",
            "commit.gpgsign=false",
            "commit-tree",
            tree,
            "-p",
            head,
            "-p",
            commit,
            input="HAFlow joint candidate " + identity + "\n",
        ).stdout.strip()
    git("update-ref", ref, head)
    return head
