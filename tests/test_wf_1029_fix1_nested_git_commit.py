"""Regression tests for wf-haflow-1029-02 Fix 1:
1. commit_task filters out untracked directories containing nested .git repositories.
2. finalize_completed_task treats unrecoverable commit errors as non-retryable and escalates.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from herdr import task_cli_delivery
from services.controller import git_finalize


def test_commit_task_ignores_nested_git_repo(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    clone.mkdir()
    subprocess.run(["git", "-C", str(clone), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.email", "test@test.com"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.name", "test"], check=True)

    # Initial commit
    (clone / "init.txt").write_text("hello")
    subprocess.run(["git", "-C", str(clone), "add", "init.txt"], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-m", "init"], check=True)

    # Now create a new normal file AND a nested git repo inside test-evidence/cli-chain/project
    (clone / "new_feature.py").write_text("x = 1")
    nested = clone / "test-evidence" / "cli-chain" / "project"
    nested.mkdir(parents=True)
    subprocess.run(["git", "-C", str(nested), "init"], check=True, capture_output=True)
    # The nested repo has NO commits checked out
    (nested / "scratch.txt").write_text("nested")

    task = {
        "task_id": "test-nested-task",
        "clone_path": str(clone),
        "integration_mode": "git",
        "execution_mode": "git",
        "status": "completed",
        "node": "implementation",
        "stage": "implementation",
    }

    mock_store = MagicMock()
    mock_store.db_path = str(tmp_path / "mock.db")
    mock_store.get_task.return_value = task
    mock_store.list_tasks.return_value = [task]

    # Run commit_task
    task_cli_delivery.commit_task(
        "test-nested-task",
        message="feat: new feature",
        load_tasks=lambda: {"tasks": [task]},
        find_task=lambda data, tid: task,
        get_store=lambda: mock_store,
        is_internal_untracked=lambda p: False,
        adopt_head_if_attributable=lambda *a, **k: False,
        set_status=lambda *a, **k: None,
        run_commit_index_command=lambda tid, cl, cmd: subprocess.run(cmd, capture_output=True, text=True),
    )

    # Check git log in clone: it committed new_feature.py without choking on nested .git
    res = subprocess.run(["git", "-C", str(clone), "log", "-1", "--name-only"], capture_output=True, text=True)
    assert "new_feature.py" in res.stdout
    assert "test-evidence" not in res.stdout


def test_git_finalize_fatal_commit_error_escalates_without_retry():
    mock_ports = MagicMock()
    task = {
        "task_id": "t-fatal-commit",
        "status": "completed",
        "integration_mode": "git",
        "execution_mode": "git",
        "clone_path": "/fake/clone",
        "run_id": "run-1",
    }
    mock_ports.get_task.return_value = task
    mock_ports._get_store.return_value = MagicMock()
    mock_ports.ensure_no_git_processes = None
    mock_ports.maybe_complete_on_task_done.return_value = []
    mock_ports.os.environ = {}
    mock_ports.TASK_MANAGER = "herdr-task"

    # Mock subprocess.run for herdr-task commit failing with exit code 128 (does not have a commit checked out)
    fake_result = subprocess.CompletedProcess(
        args=["herdr-task", "commit", "t-fatal-commit"],
        returncode=128,
        stdout="",
        stderr="error: 'test-evidence/cli-chain/project/' does not have a commit checked out\nfatal: adding files failed",
    )
    mock_ports.subprocess.run.return_value = fake_result

    outcome = git_finalize.finalize_completed_task(mock_ports, "t-fatal-commit")

    # Must be retryable=False and escalate immediately
    assert outcome["retryable"] is False
    assert outcome["kind"] == "error"
    assert outcome["rc"] == 128
    mock_ports._escalate_finalize.assert_called_once()
    assert mock_ports._escalate_finalize.call_args[0][1] == "commit_fatal_error"
