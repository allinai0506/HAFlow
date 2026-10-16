"""Wrapup completion requires a machine-readable review-closure receipt.

Codex review P1 (wf-haflow-1015-01): the PR/review-closure requirement was
prompt text only — a bare ``set <wrapup-task> completed`` (including the
controller's auto-accept path, which is also a CLI set) could still close the
workflow without any PR URL, CI result, or findings receipt. These tests pin
the machine enforcement: ``record-review-closure`` writes the receipt and the
completed transition validates it, with an audited ``--skip-review-closure
--note`` bypass.
"""
import os
from pathlib import Path
import subprocess
import sys

import pytest
from herdr.state_store import SQLiteStateStore

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def scene(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    store.save_workflow({"workflow_id": "wf-closure", "status": "running"})
    store.save_task({
        "task_id": "wrapup-task", "workflow_id": "wf-closure",
        "status": "agent_done", "stage": "wrapup", "node": "wrapup",
        "agent": "qodercli",
    })
    store.save_task({
        "task_id": "impl-task", "workflow_id": "wf-closure",
        "status": "agent_done", "stage": "implementation",
        "node": "implementation", "agent": "opencode",
    })
    return store


def run_cli(store, *argv):
    env = os.environ.copy()
    env["HERDR_STATE_DB"] = str(store.db_path)
    env.pop("TASKS_FILE", None)
    env.pop("WORKFLOWS_FILE", None)
    return subprocess.run(
        [sys.executable, str(ROOT / "bin/herdr-task"), *argv],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=15,
    )


def test_wrapup_completion_requires_receipt(scene):
    result = run_cli(scene, "set", "wrapup-task", "completed")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "REVIEW CLOSURE REQUIRED" in (result.stdout + result.stderr)
    assert scene.get_task("wrapup-task")["status"] == "agent_done"


def test_record_review_closure_then_completion_succeeds(scene):
    recorded = run_cli(
        scene, "record-review-closure", "wrapup-task",
        "--pr-url", "https://example.test/pr/1",
        "--ci-status", "pass",
        "--findings", "P1=0, P2=1 disposed",
    )
    assert recorded.returncode == 0, recorded.stdout + recorded.stderr
    assert "REVIEW CLOSURE RECORDED" in recorded.stdout

    result = run_cli(scene, "set", "wrapup-task", "completed")
    assert result.returncode == 0, result.stdout + result.stderr
    assert scene.get_task("wrapup-task")["status"] == "completed"


def test_deferred_receipt_requires_reason_but_then_allows_completion(scene):
    refused = run_cli(scene, "record-review-closure", "wrapup-task", "--deferred")
    assert refused.returncode == 2
    assert "REVIEW CLOSURE REFUSED" in (refused.stdout + refused.stderr)

    deferred = run_cli(
        scene, "record-review-closure", "wrapup-task", "--deferred",
        "--reason", "no remote configured: git remote -v empty",
    )
    assert deferred.returncode == 0, deferred.stdout + deferred.stderr
    assert run_cli(scene, "set", "wrapup-task", "completed").returncode == 0


def test_pr_url_required_unless_deferred(scene):
    refused = run_cli(
        scene, "record-review-closure", "wrapup-task", "--ci-status", "pass",
    )
    assert refused.returncode == 2
    assert "REVIEW CLOSURE REFUSED" in (refused.stdout + refused.stderr)


def test_skip_bypass_requires_note_and_completes(scene):
    refused = run_cli(
        scene, "set", "wrapup-task", "completed", "--skip-review-closure",
    )
    assert refused.returncode == 2
    assert "SKIP REFUSED" in (refused.stdout + refused.stderr)

    bypassed = run_cli(
        scene, "set", "wrapup-task", "completed", "--skip-review-closure",
        "--note", "audited emergency: platform outage",
    )
    assert bypassed.returncode == 0, bypassed.stdout + bypassed.stderr
    assert scene.get_task("wrapup-task")["status"] == "completed"


def test_non_wrapup_completion_unaffected(scene):
    result = run_cli(scene, "set", "impl-task", "completed")
    assert result.returncode == 0, result.stdout + result.stderr
    assert scene.get_task("impl-task")["status"] == "completed"
