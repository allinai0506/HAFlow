"""Regression and integration tests for Slice 2: Scheduling Stability & Deadlock Defense (P4, P5, P11).

Covers:
- P4: recovery_store separation of ensure transaction and precondition validation.
- P5: record_candidate_frozen reachability and freshness checks against base branch.
- P11: check_zombie_obligations & required_task_issues convergence on terminal workflows and supersedes linkage.
"""

import json
import subprocess
import time
from pathlib import Path
import pytest

from herdr import recovery_store as rs
from herdr import scheduler_facts as sf
from herdr import state_db
from herdr.state_store import get_state_store
from herdr.task_cli_tasks import check_zombie_obligations
from herdr.scheduler import required_task_issues


# =========================================================================
# P4: recovery_store.py ensure transaction separation
# =========================================================================

def test_p4_decide_operation_preserves_ensure_when_precondition_fails(tmp_path, monkeypatch):
    """P4: If decide_operation fails precondition check, ensure_for_workflow changes must NOT be rolled back."""
    db_path = tmp_path / "p4_state.db"
    conn = state_db.get_db_connection(db_path)
    rs.ensure_schema(conn)
    now = 100.0

    # Seed running workflow with initial obligation
    state_db.save_workflow({
        "workflow_id": "wf-p4",
        "status": "running",
        "execution_id": "exec-1",
        "config": {"nodes": []},
    }, conn=conn)

    from herdr import workflow_progress
    initial_facts = {"identity_key": "op-p4", "status": "pending", "kind": "fix_loop", "reason": "test_failure"}
    monkeypatch.setattr(workflow_progress, "assess_workflow", lambda *a: {"obligations": [initial_facts]})
    rs.ensure_for_workflow(conn, "wf-p4", now)
    conn.commit()
    conn.close()

    op = rs.list_operations(db_path, "wf-p4")[0]
    orig_version = op["version"]

    # Now simulate that new facts arrived, which ensure_for_workflow will apply (e.g. facts_changed)
    updated_facts = {"identity_key": "op-p4", "status": "pending", "kind": "fix_loop", "reason": "updated_test_failure", "new_detail": 123}
    monkeypatch.setattr(workflow_progress, "assess_workflow", lambda *a: {"obligations": [updated_facts]})

    # Calling decide_operation with an invalid action / invalid expected_version / invalid precondition
    # In this case, pass an expected_version that matches orig_version BEFORE ensure runs.
    # When ensure runs in its own transaction, op["version"] will bump (because facts changed).
    # Then decide_operation's precondition check `op['version'] != expected_version` triggers ValueError!
    with pytest.raises(ValueError):
        rs.decide_operation(db_path, op["id"], orig_version, "operator1", "retry", "attempt retry", now + 10)

    # In the old code with combined transaction, the ensure transaction was rolled back,
    # so the DB still had the old payload_json and old version!
    # In P4 (separated transactions), the ensure transaction committed!
    fresh_op = rs.list_operations(db_path, "wf-p4")[0]
    assert fresh_op["payload"]["reason"] == "updated_test_failure"
    assert fresh_op["version"] > orig_version


def test_p4_mark_started_preserves_ensure_when_ownership_fails(tmp_path, monkeypatch):
    """P4: If mark_started fails _owned check, ensure_for_workflow changes must NOT be rolled back."""
    db_path = tmp_path / "p4_state_mark.db"
    conn = state_db.get_db_connection(db_path)
    rs.ensure_schema(conn)
    now = 100.0

    state_db.save_workflow({
        "workflow_id": "wf-p4-mark",
        "status": "running",
        "execution_id": "exec-1",
        "config": {"nodes": []},
    }, conn=conn)

    from herdr import workflow_progress
    initial_facts = {"identity_key": "op-p4-m", "status": "pending", "kind": "fix_loop"}
    monkeypatch.setattr(workflow_progress, "assess_workflow", lambda *a: {"obligations": [initial_facts]})
    rs.ensure_for_workflow(conn, "wf-p4-mark", now)
    conn.commit()
    conn.close()

    op = rs.list_operations(db_path, "wf-p4-mark")[0]

    # Change facts to test ensure commit
    updated_facts = {"identity_key": "op-p4-m", "status": "pending", "kind": "fix_loop", "param": "v2"}
    monkeypatch.setattr(workflow_progress, "assess_workflow", lambda *a: {"obligations": [updated_facts]})

    # Calling mark_started with wrong owner -> _owned raises ValueError
    with pytest.raises(ValueError):
        rs.mark_started(db_path, op["id"], "wrong_owner", "some_step", now + 10)

    fresh_op = rs.list_operations(db_path, "wf-p4-mark")[0]
    assert fresh_op["payload"].get("param") == "v2"


# =========================================================================
# P5: record_candidate_frozen reachability & freshness checks against base
# =========================================================================

def _git(repo_dir, *args):
    res = subprocess.run(["git", "-C", str(repo_dir), *args], text=True, capture_output=True, check=True)
    return res.stdout.strip()


@pytest.fixture
def git_repo_with_base(tmp_path):
    """Creates a real Git repo with 'main' branch and an isolated state DB."""
    repo = tmp_path / "test_repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Tester")

    # Initial commit on main
    (repo / "file.txt").write_text("initial main\n")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "main 1")
    main_1 = _git(repo, "rev-parse", "HEAD")

    # Feature branch with candidate
    _git(repo, "checkout", "-b", "feat")
    (repo / "feat.txt").write_text("feature content\n")
    _git(repo, "add", "feat.txt")
    _git(repo, "commit", "-m", "feature commit 1")
    candidate_1 = _git(repo, "rev-parse", "HEAD")

    # Switch back to main
    _git(repo, "checkout", "main")

    db_path = tmp_path / "p5_state.db"
    store = get_state_store(db_path)
    store.save_workflow({
        "workflow_id": "wf-p5",
        "status": "running",
        "project_root": str(repo),
        "base_branch": "main",
    })

    return {
        "repo": repo,
        "db": db_path,
        "main_1": main_1,
        "candidate_1": candidate_1,
    }


def test_p5_rejects_unreachable_candidate(git_repo_with_base):
    """P5: Non-existent commit SHA in git repo raises ValueError."""
    fake_sha = "0123456789abcdef0123456789abcdef01234567"
    with pytest.raises(ValueError, match="unreachable"):
        sf.record_candidate_frozen(
            "wf-p5", fake_sha, repo_path=git_repo_with_base["repo"],
            base_branch="main", db_path=git_repo_with_base["db"]
        )


def test_p5_freezes_fresh_candidate(git_repo_with_base):
    """P5: Fresh candidate containing main_1 is successfully frozen."""
    res = sf.record_candidate_frozen(
        "wf-p5", git_repo_with_base["candidate_1"],
        repo_path=git_repo_with_base["repo"], base_branch="main",
        db_path=git_repo_with_base["db"]
    )
    assert res["status"] == "created"
    assert res["event"]["payload"]["candidate_sha"] == git_repo_with_base["candidate_1"]

    # Calling again is idempotent
    res2 = sf.record_candidate_frozen(
        "wf-p5", git_repo_with_base["candidate_1"],
        repo_path=git_repo_with_base["repo"], base_branch="main",
        db_path=git_repo_with_base["db"]
    )
    assert res2["status"] == "exists"


def test_p5_invalidates_candidate_when_main_advances(git_repo_with_base):
    """P5: When main moves forward, calling record_candidate_frozen invalidates stale candidate."""
    repo = git_repo_with_base["repo"]
    db = git_repo_with_base["db"]
    cand = git_repo_with_base["candidate_1"]

    # 1. Freeze initial candidate
    res1 = sf.record_candidate_frozen(
        "wf-p5", cand, repo_path=repo, base_branch="main", db_path=db
    )
    assert res1["status"] == "created"

    # Verify recovery_store sees active candidate
    wf_snap, _, _ = rs.read_snapshot(db, "wf-p5")
    assert wf_snap.get("candidate_sha") == cand

    # 2. Main advances!
    (repo / "main_new.txt").write_text("main advanced\n")
    _git(repo, "add", "main_new.txt")
    _git(repo, "commit", "-m", "main advanced commit 2")
    main_2 = _git(repo, "rev-parse", "HEAD")
    assert main_2 != git_repo_with_base["main_1"]

    # 3. record_candidate_frozen with the old candidate detects that main advanced
    res2 = sf.record_candidate_frozen(
        "wf-p5", cand, repo_path=repo, base_branch="main", db_path=db
    )
    assert res2["status"] == "invalidated"
    assert res2["reason"] == "base_advanced"

    # 4. Invalidation event recorded and recovery_store snapshot reflects that candidate is invalidated (None)
    wf_snap2, _, _ = rs.read_snapshot(db, "wf-p5")
    assert wf_snap2.get("candidate_sha") is None


# =========================================================================
# P11: check_zombie_obligations & required_task_issues convergence
# =========================================================================

def test_p11_check_zombie_obligations_filters_supersedes_linkage():
    """P11: Task A without superseded_by is NOT a zombie if Task B declares supersedes=A."""
    data = {
        "tasks": [
            {
                "task_id": "task-old",
                "status": "superseded",
                "replacement_pending": True,
                "superseded_by": None,  # not backfilled yet
            },
            {
                "task_id": "task-new",
                "status": "dispatched",
                "supersedes": "task-old",  # successor declared link
            },
        ]
    }
    zombies = check_zombie_obligations(data, deps={"load_tasks": lambda: data})
    assert zombies == []


def test_p11_check_zombie_obligations_filters_terminal_workflows():
    """P11: Tasks in terminal workflows (completed, failed, halted, closed, abandoned) are not active zombies."""
    for terminal_status in ["completed", "failed", "halted", "closed", "abandoned"]:
        data = {
            "tasks": [
                {
                    "task_id": f"task-in-{terminal_status}",
                    "workflow_id": f"wf-{terminal_status}",
                    "status": "superseded",
                    "replacement_pending": True,
                    "superseded_by": None,
                }
            ],
            "workflows": [
                {
                    "workflow_id": f"wf-{terminal_status}",
                    "status": terminal_status,
                }
            ]
        }
        zombies = check_zombie_obligations(data, deps={"load_tasks": lambda: data})
        assert zombies == [], f"Workflow in terminal status '{terminal_status}' should not produce zombie obligations"


def test_p11_check_zombie_obligations_detects_real_zombie_in_running_workflow():
    """P11: Genuine zombie (no replacement, active running workflow) is detected."""
    data = {
        "tasks": [
            {
                "task_id": "real-zombie",
                "workflow_id": "wf-running",
                "status": "superseded",
                "replacement_pending": True,
                "superseded_by": None,
            }
        ],
        "workflows": [
            {
                "workflow_id": "wf-running",
                "status": "running",
            }
        ]
    }
    zombies = check_zombie_obligations(data, deps={"load_tasks": lambda: data})
    assert len(zombies) == 1
    assert zombies[0]["task_id"] == "real-zombie"


def test_p11_required_task_issues_recognizes_supersedes_linkage():
    """P11: required_task_issues does not flag zombie if successor has supersedes=task_id."""
    tasks = [
        {"task_id": "t-old", "status": "superseded", "replacement_pending": True, "superseded_by": None},
        {"task_id": "t-new", "status": "dispatched", "supersedes": "t-old"},
    ]
    issues = required_task_issues(tasks, None)
    assert issues == []
