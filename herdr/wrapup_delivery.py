"""Task-owned wrapup Git delivery, independent of Task completion.

No Task state is forged: the active wrapup owns its commit and integration;
the scheduler owns publication consumption and fresh verifier dispatch.
"""

import hashlib
import json
import re
import tempfile

from . import recovery_store, state_db

ACTIVE = {"working", "agent_done", "rework"}
TEMPLATES = {"software-development-v1", "spec-development-v1"}


def enabled(task, config):
    node = next((n for n in config.get("nodes", []) if n.get("id") == "wrapup"), {})
    return (
        config.get("name") in TEMPLATES
        and node.get("review_closure_required") is True
        and (task.get("node") or task.get("stage")) == "wrapup"
        and task.get("integration_mode") == "none"
        and task.get("execution_mode") != "context"
    )


def integrated(task, workflow):
    delivery = task.get("wrapup_delivery") or {}
    return bool(
        enabled(task, workflow.get("config") or {})
        and delivery.get("phase") == "integrated"
        and task.get("status") in ACTIVE | {"completed", "cleanup_ready", "cleaned"}
        and task.get("run_id")
        and task.get("run_id") == delivery.get("run_id")
        and task.get("execution_id")
        == workflow.get("execution_id")
        == delivery.get("execution_id")
        and task.get("commit")
        == task.get("integrated_commit")
        == delivery.get("integrated_sha")
        and re.fullmatch(r"[0-9a-f]{40}", task.get("commit") or "")
        and task.get("integration_ref") == f"refs/herdr/tasks/{task['task_id']}"
    )


def _write(store, task, updates, *, parent_episode=None):
    with recovery_store._transaction(store.db_path) as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_id=?", (task["task_id"],)
        ).fetchone()
        fresh = state_db._decode_task_row(row) if row else {}
        workflow, config, _ = recovery_store._snapshot(conn, task["workflow_id"])
        if (
            fresh.get("version") != task.get("version")
            or not enabled(fresh, config)
            or fresh.get("status") not in ACTIVE
            or not fresh.get("run_id")
            or fresh.get("execution_id") != workflow.get("execution_id")
            or workflow.get("status") != "running"
            or (
                parent_episode is not None
                and workflow.get("candidate_episode_id") != parent_episode
            )
        ):
            raise ValueError("wrapup delivery identity changed")
        state_db.update_task_metadata(task["task_id"], updates, conn=conn)
    return store.get_task(task["task_id"])


def has_worktree_output(task):
    from .pr_delivery import _git
    from .repo_hygiene import teardown_output_diagnosis

    return bool(
        teardown_output_diagnosis(
            _git(task["clone_path"], "status", "--porcelain"), task_id=task["task_id"]
        )["reason"]
    )


def begin(store, task):
    """Pin parent episode before Git effects; repeat registration resumes it."""
    from .pr_delivery import _git, _publication

    workflow, config, tasks = recovery_store.read_snapshot(
        store.db_path, task["workflow_id"]
    )
    if not enabled(task, config) or task.get("status") not in ACTIVE:
        return None
    if (
        not task.get("run_id")
        or not workflow.get("execution_id")
        or task.get("execution_id") != workflow["execution_id"]
        or workflow["status"] != "running"
        or task.get("superseded_by")
    ):
        raise ValueError("wrapup delivery identity unknown")
    head = _git(task["clone_path"], "rev-parse", "HEAD")
    if _git(task["clone_path"], "symbolic-ref", "--short", "HEAD") != task.get(
        "branch"
    ):
        raise ValueError("wrapup delivery branch ownership differs")
    prior = task.get("wrapup_delivery") or {}
    if prior and prior.get("phase") != "integrated":
        if (
            prior.get("run_id") != task["run_id"]
            or prior.get("execution_id") != workflow["execution_id"]
            or prior.get("parent_episode_id") != workflow.get("candidate_episode_id")
        ):
            raise ValueError("wrapup delivery parent episode changed")
        if prior.get("phase") == "committed" and prior.get("rebase"):
            return recover_rebase(store, task)
        return task
    if (
        integrated(task, workflow)
        and head == task["integrated_commit"]
        and not has_worktree_output(task)
    ):
        return task
    # X must really be certified; a launch SHA alone is not acceptance.
    from . import workflow_docs as wd
    from .delivery_record import select_effective_delivery, _body_value

    notes = wd.load_notes(task["workflow_id"])
    note = select_effective_delivery(wd.annotate_notes(notes))
    parent = workflow.get("candidate_sha")
    if not note or _body_value(note, "candidate_sha") != parent:
        raise ValueError("wrapup parent has no effective certified delivery")
    for publisher in tasks:
        if not publisher.get("integration_ref"):
            continue
        try:
            certified = _publication(publisher, notes, store)[0]
        except ValueError:
            continue
        if certified == parent:
            break
    else:
        raise ValueError("wrapup parent has no integrated certified delivery")
    if not parent or not workflow.get("candidate_episode_id"):
        raise ValueError("wrapup parent candidate unknown")
    if head == parent and not has_worktree_output(task):
        return task  # No new deliverable: retain the existing certification.
    _git(task["clone_path"], "merge-base", "--is-ancestor", parent, head)
    round_number = int(prior.get("round") or 0) + 1
    if round_number > 3:
        raise ValueError("wrapup candidate update budget exhausted")
    delivery = {
        "phase": "registered",
        "run_id": task["run_id"],
        "execution_id": workflow["execution_id"],
        "parent_sha": parent,
        "parent_episode_id": workflow["candidate_episode_id"],
        "round": round_number,
    }
    return _write(
        store,
        task,
        {
            "wrapup_delivery": delivery,
            "integration_publication_episode_id": workflow["candidate_episode_id"],
        },
    )


def committed(store, task_id):
    task = store.get_task(task_id)
    delivery = dict(
        task["wrapup_delivery"], phase="committed", source_sha=task["commit"]
    )
    return _write(store, task, {"wrapup_delivery": delivery})


def recover_rebase(store, task):
    """Recover only the content of the pinned rebase, never arbitrary HEAD.

    Git cannot atomically commit a rebase with SQLite. Replay the recorded input
    onto its fixed base in a disposable local clone when that window is hit.
    """
    from .pr_delivery import _git

    delivery = task["wrapup_delivery"]
    intent = delivery.get("rebase") or {}
    clone = task["clone_path"]
    head = _git(clone, "rev-parse", "HEAD")
    if head == task.get("commit"):
        return task
    if (
        delivery.get("phase") != "committed"
        or delivery.get("run_id") != task.get("run_id")
        or delivery.get("execution_id") != task.get("execution_id")
        or intent.get("input_sha") != task.get("commit")
        or any(
            not re.fullmatch(r"[0-9a-f]{40}", str(intent.get(key) or ""))
            for key in ("input_sha", "base_sha")
        )
    ):
        raise ValueError(
            "wrapup HEAD differs from registered commit without a matching rebase intent"
        )
    if has_worktree_output(task):
        raise ValueError("wrapup rebase recovery requires unchanged worktree")
    _git(clone, "merge-base", "--is-ancestor", intent["base_sha"], head)
    # Local shared objects only; no remote fetch, launch, or inherited hooks.
    with tempfile.TemporaryDirectory(prefix="haflow-wrapup-rebase-") as temporary:
        replay = temporary + "/replay"
        _git(
            clone,
            "clone",
            "--shared",
            "--no-checkout",
            "--template=",
            "--",
            clone,
            replay,
        )
        _git(
            replay,
            "-c",
            "core.hooksPath=/dev/null",
            "checkout",
            "--detach",
            intent["input_sha"],
        )
        _git(
            replay,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "user.name=HAFlow Recovery",
            "-c",
            "user.email=recovery@haflow.invalid",
            "rebase",
            intent["base_sha"],
        )
        if _git(replay, "rev-parse", "HEAD^{tree}") != _git(
            clone, "rev-parse", head + "^{tree}"
        ):
            raise ValueError("wrapup HEAD does not match the pinned rebase result")
    if _git(clone, "rev-parse", "HEAD") != head or has_worktree_output(task):
        raise ValueError("wrapup worktree changed during rebase recovery")
    return _write(
        store,
        task,
        {
            "commit": head,
            "integration_head_before_rebase": intent["input_sha"],
            "integration_base_sha": intent["base_sha"],
        },
        parent_episode=delivery["parent_episode_id"],
    )


def prepare_rebase(store, task, base_sha):
    """Persist the exact registered input/base before Git changes HEAD."""
    from .pr_delivery import _git

    task = recover_rebase(store, task)
    delivery = task["wrapup_delivery"]
    if _git(task["clone_path"], "rev-parse", "HEAD") != task.get("commit"):
        raise ValueError("wrapup HEAD differs from registered commit")
    return _write(
        store,
        task,
        {
            "wrapup_delivery": dict(
                delivery,
                rebase={
                    "input_sha": task["commit"],
                    "base_sha": base_sha,
                },
            )
        },
        parent_episode=delivery["parent_episode_id"],
    )


def finish_integration(store, task_id):
    task = store.get_task(task_id)
    delivery = dict(
        task["wrapup_delivery"],
        phase="integrated",
        integrated_sha=task["integrated_commit"],
        publication_branch=publication_branch(task, task["integrated_commit"]),
    )
    return _write(store, task, {"wrapup_delivery": delivery})


def publication_branch(task, sha):
    scope = hashlib.sha256(task["task_id"].encode()).hexdigest()[:16]
    return f"herdr/delivery-{scope}-{sha}"


def reconcile_verifiers(store, workflow_id):
    """Resume freeze→retirement atomically using ordinary lifecycle edges.

    Historical verdicts and candidate claims are retained. Pending replacement
    obligations feed the normal dispatch planner; no validator is started here.
    """
    with recovery_store._transaction(store.db_path) as conn:
        workflow, config, tasks = recovery_store._snapshot(conn, workflow_id)
        row = conn.execute(
            "SELECT * FROM events WHERE id=? AND workflow_id=? AND event_type='candidate_frozen'",
            (workflow.get("candidate_episode_id"), workflow_id),
        ).fetchone()
        freeze = json.loads(row["payload_json"]) if row else {}
        if freeze.get("source_node") != "wrapup" or workflow.get("status") != "running":
            return False
        for task in tasks:
            if (
                (task.get("node") or task.get("stage"))
                not in {"test", "review", "acceptance"}
                or task.get("execution_id") != workflow.get("execution_id")
                or task.get("status") == "superseded"
                or task.get("superseded_by")
                or (
                    task.get("candidate_sha") == workflow["candidate_sha"]
                    and task.get("candidate_episode_id")
                    == workflow["candidate_episode_id"]
                )
            ):
                continue
            status = task["status"]
            # Git verifiers are not part of either default template.
            if task.get("integration_mode") not in (None, "none") or status in {
                "pending",
                "committed",
                "integrated",
            }:
                raise ValueError(
                    "wrapup verifier retirement requires normal settled/non-git lineage"
                )
            path = {
                "completed": ["cleanup_ready", "cleaned", "superseded"],
                "cleanup_ready": ["cleaned", "superseded"],
            }.get(status, ["superseded"])
            for target in path:
                result = state_db.transition_task(
                    task["task_id"],
                    target,
                    source="wrapup-delivery",
                    reason="new wrapup candidate requires fresh verification",
                    expected_status=status,
                    metadata={
                        "replacement_pending": True,
                        "wrapup_reverification_episode": workflow[
                            "candidate_episode_id"
                        ],
                    },
                    conn=conn,
                )
                if not result.get("accepted"):
                    raise ValueError("wrapup verifier retirement changed")
                status = target
        return True


def is_current_round(store, workflow_id, sha):
    workflow = store.get_workflow(workflow_id) or {}
    if workflow.get("candidate_sha") != sha:
        return False
    conn = state_db.get_readonly_db_connection(store.db_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM events WHERE id=? AND workflow_id=? AND event_type='candidate_frozen' AND source='critical-path-scheduler'",
            (workflow.get("candidate_episode_id"), workflow_id),
        ).fetchone()
        return bool(
            row and json.loads(row["payload_json"]).get("source_node") == "wrapup"
        )
    finally:
        conn.close()


def record_pr_receipt(store, task, workflow, receipt):
    """External responses cannot register against a newer delivery generation."""
    with recovery_store._transaction(store.db_path) as conn:
        fresh_workflow, _, tasks = recovery_store._snapshot(conn, task["workflow_id"])
        fresh = next((t for t in tasks if t["task_id"] == task["task_id"]), {})
        if (
            fresh.get("version") != task.get("version")
            or not integrated(fresh, fresh_workflow)
            or fresh_workflow.get("candidate_sha") != receipt["candidate_sha"]
            or any(
                fresh_workflow.get(key) != workflow.get(key)
                for key in ("execution_id", "candidate_episode_id", "status")
            )
        ):
            raise ValueError(
                "delivery changed during publication; reconcile the external result"
            )
        state_db.record_event(
            {
                "event_type": "pull_request_created",
                "payload": receipt,
                "task_id": task["task_id"],
                "workflow_id": task["workflow_id"],
                "run_id": task["run_id"],
                "source": "herdr-task",
            },
            conn=conn,
        )
