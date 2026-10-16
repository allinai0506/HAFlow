"""Wrapup completion requires an acceptable, head-bound review-closure receipt.

Codex review P1s (wf-haflow-1015-01), pinned behaviourally:

1. the invariant lives in the **shared transition gateway** — the
   controller's CAS auto-accept path (``kernel.transition_task``) is covered
   too, not only the CLI ``set`` pre-check;
2. a receipt is only acceptable with concrete content: non-deferred needs
   PR URL + ``ci_status=pass`` + findings; deferred needs a reason — always
   plus the certified 40-hex head SHA;
3. the receipt is **bound to the reviewed head**: a later commit invalidates
   it (stale receipt must be re-recorded); the audited
   ``--skip-review-closure --note`` bypass writes a skipped event;
4. the contract is **opt-in** via the pinned workflow configuration
   (``review_closure_required: true`` on the wrapup node): legacy workflows
   and custom templates keep their previous behaviour instead of being
   bricked by repeated auto-accept refusals.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from herdr.state_store import SQLiteStateStore

ROOT = Path(__file__).resolve().parents[1]
DUMMY_HEAD = "a" * 40


def run_cli(store, *argv):
    env = os.environ.copy()
    env["HERDR_STATE_DB"] = str(store.db_path)
    env.pop("TASKS_FILE", None)
    env.pop("WORKFLOWS_FILE", None)
    return subprocess.run(
        [sys.executable, str(ROOT / "bin/herdr-task"), *argv],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=15,
    )


def run_kernel_transition(store, task_id, to_status="completed"):
    env = os.environ.copy()
    env["HERDR_STATE_DB"] = str(store.db_path)
    code = (
        "from herdr import kernel; "
        f"kernel.transition_task({task_id!r}, {to_status!r}, 'shared-path-test', 'test')"
    )
    return subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=15,
    )


def make_git_clone(tmp_path: Path) -> Path:
    clone = tmp_path / "clone"
    clone.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(clone), *args], check=True,
                       capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@example.test")
    git("config", "user.name", "t")
    (clone / "f.txt").write_text("one\n")
    git("add", "f.txt")
    git("commit", "-qm", "one")
    return clone


def clone_head(clone: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def commit_more(clone: Path, content: str) -> None:
    (clone / "f.txt").write_text(content)
    subprocess.run(["git", "-C", str(clone), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-qm", "more"],
                   check=True, capture_output=True, text=True)


def opted_config(tmp_path: Path) -> Path:
    config = tmp_path / "workflow.json"
    config.write_text(json.dumps({
        "nodes": [
            {"id": "implementation"},
            {"id": "wrapup", "review_closure_required": True},
        ],
    }))
    return config


@pytest.fixture
def scene(tmp_path):
    config = opted_config(tmp_path)
    store = SQLiteStateStore(tmp_path / "state.db")
    store.save_workflow({"workflow_id": "wf-closure", "status": "running"})
    store.save_task({
        "task_id": "wrapup-task", "workflow_id": "wf-closure",
        "status": "agent_done", "stage": "wrapup", "node": "wrapup",
        "agent": "qodercli", "workflow_config": str(config),
    })
    # Legacy wrapup task: its pinned configuration predates the contract.
    store.save_task({
        "task_id": "legacy-wrapup", "workflow_id": "wf-closure",
        "status": "agent_done", "stage": "wrapup", "node": "wrapup",
        "agent": "qodercli",
    })
    store.save_task({
        "task_id": "impl-task", "workflow_id": "wf-closure",
        "status": "agent_done", "stage": "implementation",
        "node": "implementation", "agent": "opencode",
    })
    return store


def record_valid(store, task_id="wrapup-task", head=DUMMY_HEAD):
    return run_cli(
        store, "record-review-closure", task_id,
        "--pr-url", "https://example.test/pr/1",
        "--ci-status", "pass",
        "--findings", "P1=0, P2=1 disposed",
        "--head-sha", head,
    )


def test_wrapup_completion_requires_receipt(scene):
    result = run_cli(scene, "set", "wrapup-task", "completed")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "REVIEW CLOSURE REQUIRED" in (result.stdout + result.stderr)
    assert scene.get_task("wrapup-task")["status"] == "agent_done"


def test_shared_transition_gateway_enforces_invariant(scene):
    """The controller's CAS auto-accept path must be gated too (kernel funnel)."""
    refused = run_kernel_transition(scene, "wrapup-task")
    assert refused.returncode != 0
    assert "REVIEW CLOSURE REQUIRED" in refused.stderr
    assert scene.get_task("wrapup-task")["status"] == "agent_done"

    assert record_valid(scene).returncode == 0
    allowed = run_kernel_transition(scene, "wrapup-task")
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert scene.get_task("wrapup-task")["status"] == "completed"


@pytest.mark.parametrize("ci_args", [
    ["--ci-status", "fail"],
    ["--ci-status", "pending"],
    [],
])
def test_receipt_requires_passing_ci(scene, ci_args):
    argv = ["record-review-closure", "wrapup-task",
            "--pr-url", "https://example.test/pr/1",
            "--findings", "P1=0", "--head-sha", DUMMY_HEAD, *ci_args]
    result = run_cli(scene, *argv)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "REVIEW CLOSURE REFUSED" in (result.stdout + result.stderr)


def test_receipt_requires_findings_and_head(scene):
    no_findings = run_cli(
        scene, "record-review-closure", "wrapup-task",
        "--pr-url", "https://example.test/pr/1",
        "--ci-status", "pass", "--head-sha", DUMMY_HEAD,
    )
    assert no_findings.returncode == 2

    no_head = run_cli(
        scene, "record-review-closure", "wrapup-task",
        "--pr-url", "https://example.test/pr/1",
        "--ci-status", "pass", "--findings", "P1=0",
    )
    assert no_head.returncode == 2
    assert "head-sha" in (no_head.stdout + no_head.stderr)


def test_stale_receipt_is_rejected_scoped(tmp_path):
    clone = make_git_clone(tmp_path)
    config = opted_config(tmp_path)
    store = SQLiteStateStore(tmp_path / "state.db")
    store.save_workflow({"workflow_id": "wf-closure", "status": "running"})
    store.save_task({
        "task_id": "wrapup-task", "workflow_id": "wf-closure",
        "status": "agent_done", "stage": "wrapup", "node": "wrapup",
        "agent": "qodercli", "clone_path": str(clone),
        "workflow_config": str(config),
    })

    assert record_valid(store, head=clone_head(clone)).returncode == 0
    commit_more(clone, "two\n")  # a commit after the reviewed head

    stale = run_cli(store, "set", "wrapup-task", "completed")
    assert stale.returncode == 2
    assert "stale" in (stale.stdout + stale.stderr)
    assert store.get_task("wrapup-task")["status"] == "agent_done"

    assert record_valid(store, head=clone_head(clone)).returncode == 0
    fresh = run_cli(store, "set", "wrapup-task", "completed")
    assert fresh.returncode == 0, fresh.stdout + fresh.stderr
    assert store.get_task("wrapup-task")["status"] == "completed"


def test_deferred_receipt_requires_reason_then_allows_completion(scene):
    refused = run_cli(
        scene, "record-review-closure", "wrapup-task",
        "--deferred", "--head-sha", DUMMY_HEAD,
    )
    assert refused.returncode == 2
    assert "REVIEW CLOSURE REFUSED" in (refused.stdout + refused.stderr)

    deferred = run_cli(
        scene, "record-review-closure", "wrapup-task", "--deferred",
        "--reason", "no remote configured: git remote -v empty",
        "--head-sha", DUMMY_HEAD,
    )
    assert deferred.returncode == 0, deferred.stdout + deferred.stderr
    assert run_cli(scene, "set", "wrapup-task", "completed").returncode == 0


def test_skip_bypass_requires_note_and_records(scene):
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


def test_legacy_or_custom_wrapup_without_capability_is_not_gated(scene):
    """Upgrades must not brick pinned configurations that never opted in."""
    legacy = run_cli(scene, "set", "legacy-wrapup", "completed")
    assert legacy.returncode == 0, legacy.stdout + legacy.stderr
    assert scene.get_task("legacy-wrapup")["status"] == "completed"
