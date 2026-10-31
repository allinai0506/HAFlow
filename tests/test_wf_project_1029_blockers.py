"""Regression tests for wf-project-1029-01 blockers:
1. clone_archive ignores node_modules, build dirs, target, .m2, and virtualenvs so enterprise clones don't exceed archive budget.
2. _business_scope and _workflow_key normalize workflow status 'closing' to 'running' so closing workflows do not invalidate business acceptance receipts.
3. wait_startup_ready polls until deadline rather than terminating prematurely after 3 seconds, and start_agent retries on agent_not_ready.
4. finalize_completed_task serializes in-flight execution per task to eliminate git_busy / active process collisions.
5. finalize_completed_task fast-fails and escalates immediately on pre-commit hook / complexity gate failures without wasting 50 minutes.
6. pr_delivery _git uses extended timeout (120s) for git push operations instead of 30s.
7. advancement throttles [STARTUP WAIT] log messages to avoid log flooding during Deep Preflight.
"""

import importlib.util
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from herdr import business_gate, clone_archive, pr_delivery, task_checkpoint


def _load_worker():
    path = Path(__file__).resolve().parent.parent / "services" / "herdr-worker.py"
    spec = importlib.util.spec_from_file_location("herdr_worker", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_clone_archive_ignores_node_modules_and_build_dirs(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    # Normal files
    (root / "src").mkdir()
    (root / "src" / "index.ts").write_text("console.log('hi');")
    (root / "README.md").write_text("doc")

    # Dependency and build directories that would otherwise blow budget
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1;")
    (root / "dist-release").mkdir()
    (root / "dist-release" / "bundle.js").write_text("bundled" * 1000)
    (root / "target" / "classes").mkdir(parents=True)
    (root / "target" / "classes" / "App.class").write_text("binary" * 100)
    (root / ".m2" / "repository").mkdir(parents=True)
    (root / ".m2" / "repository" / "lib.jar").write_text("jar" * 100)
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("py")

    # Regular project report deliverable must NEVER be treated as bypass
    (root / "reports").mkdir()
    (root / "reports" / "analysis.md").write_text("# Report Analysis")

    assert clone_archive.is_bypass_rel_path("node_modules/pkg/index.js")
    assert clone_archive.is_bypass_rel_path("dist-release/bundle.js")
    assert clone_archive.is_bypass_rel_path("target/classes/App.class")
    assert clone_archive.is_bypass_rel_path(".m2/repository/lib.jar")
    assert clone_archive.is_bypass_rel_path(".venv/bin/python")
    assert not clone_archive.is_bypass_rel_path("reports/analysis.md")

    inv = clone_archive._inventory(root)
    inv_keys = set(inv.keys())
    assert "src" in inv_keys
    assert "src/index.ts" in inv_keys
    assert "README.md" in inv_keys
    assert "reports/analysis.md" in inv_keys
    assert not any(k.startswith("node_modules") for k in inv_keys)
    assert not any(k.startswith("dist-release") for k in inv_keys)
    assert not any(k.startswith("target") for k in inv_keys)
    assert not any(k.startswith(".m2") for k in inv_keys)
    assert not any(k.startswith(".venv") for k in inv_keys)

    # Even within a bypass dir, an explicitly declared deliverable on the task is preserved
    (root / "dist").mkdir()
    (root / "dist" / "declared_artifact.pdf").write_text("pdf bytes")
    task_with_decl = {"artifacts": ["dist/declared_artifact.pdf"]}
    inv_with_decl = clone_archive._inventory(root, task=task_with_decl)
    assert "dist/declared_artifact.pdf" in inv_with_decl


def test_preserve_clone_and_verify_source_with_declared_artifact_in_excluded_dir(tmp_path):
    repo = tmp_path / "clones" / "t-decl"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "index.js").write_text("console.log('hi');")
    (repo / "dist").mkdir()
    (repo / "dist" / "analysis.md").write_text("# Deliverable Analysis")

    task = {
        "task_id": "t-decl",
        "workflow_id": "wf-decl",
        "run_id": "r-decl",
        "clone_path": str(repo),
        "artifacts": ["dist/analysis.md"],
    }
    archive = clone_archive.preserve_clone(task, tmp_path / "clones")
    assert archive is not None
    # Immediate verify_source must succeed and not raise 'clone output changed after archive'
    clone_archive.verify_source(task, tmp_path / "clones", archive)


def test_business_scope_and_key_normalizes_closing_status():
    wf_running = {
        "workflow_id": "wf-1",
        "execution_id": "wf-1",
        "status": "running",
        "candidate_sha": "abc1234",
        "candidate_episode_id": 10,
        "created_at": 1000,
        "reopened_at": None,
    }
    wf_closing = dict(wf_running, status="closing")
    config = {"nodes": [{"id": "test", "node_type": "verifier"}]}

    task = {
        "task_id": "t-1",
        "workflow_id": "wf-1",
        "run_id": "r-1",
        "execution_id": "wf-1",
        "node": "test",
        "stage": "test",
        "completion_epoch": "e-1",
        "candidate_sha": "abc1234",
        "status": "cleaned",
    }

    # Hashing should be identical whether workflow status is 'running' or 'closing'
    scope_running = task_checkpoint._business_scope(task, wf_running)
    scope_closing = task_checkpoint._business_scope(task, wf_closing)
    assert scope_running == scope_closing

    key_running = business_gate._workflow_key(wf_running, config)
    key_closing = business_gate._workflow_key(wf_closing, config)
    assert key_running == key_closing


def test_wait_startup_ready_polls_until_deadline_for_slow_agent():
    worker = _load_worker()

    call_count = [0]

    def fake_run_json(cmd, timeout=None):
        call_count[0] += 1
        return {"result": {"agent": {"agent_session": "sess-1", "name": "ag-1", "agent_status": "idle" if call_count[0] >= 4 else "busy"}}}

    def fake_readiness(kind, started, runtime, text):
        if text is None:
            return {"interactive_ready": False, "status": "UNKNOWN", "reason": "transcript_unavailable"}
        if runtime.get("agent_status") == "idle":
            return {"interactive_ready": True, "status": "READY", "reason": "native_identity_idle"}
        return {"interactive_ready": False, "status": "UNKNOWN", "reason": "interactive_not_idle"}

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "prompt> "

    with patch.object(worker, "run_json", side_effect=fake_run_json), \
         patch.object(worker.subprocess, "run", return_value=fake_proc), \
         patch("herdr.agent_adapter.startup_readiness", side_effect=fake_readiness), \
         patch("time.sleep", return_value=None):
        verdict = worker.wait_startup_ready("opencode", "w1:p1", {"agent_session": "sess-1"}, attempts=None, total_timeout=10)
        assert verdict["interactive_ready"] is True
        assert verdict["status"] == "READY"
        assert call_count[0] >= 4


def test_start_agent_retries_on_agent_not_ready():
    worker = _load_worker()

    calls = [0]

    def fake_run_json(cmd, timeout=None):
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError('{"error":{"code":"agent_not_ready","message":"agent is blocked during startup and is not ready for prompts"},"id":"cli:agent:start"}')
        return {"result": {"agent": {"name": "ag-ready", "agent_session": "sess-1"}}}

    with patch.object(worker, "run_json", side_effect=fake_run_json), \
         patch("time.sleep", return_value=None):
        agent = worker.start_agent("task-1", "grok", "w1:p1", retries=3, delay=0.01)
        assert agent["name"] == "ag-ready"
        assert calls[0] == 2


def test_finalize_completed_task_serializes_concurrent_calls():
    from services.controller import git_finalize

    mock_ports = MagicMock()
    mock_ports._finalize_inflight = set()
    mock_ports.get_task.return_value = {"task_id": "t-busy", "status": "completed", "integration_mode": "none"}

    # Simulate task already in flight
    mock_ports._finalize_inflight.add("t-busy")

    res = git_finalize.finalize_completed_task(mock_ports, "t-busy")
    assert res["retryable"] is True
    assert res["kind"] == "inflight"
    assert res["reason"] == "finalize_already_in_progress"


def test_finalize_completed_task_escalates_on_precommit_hook_failure():
    from services.controller import git_finalize

    mock_ports = MagicMock()
    mock_ports._finalize_inflight = set()
    task = {"task_id": "t-hook", "status": "completed", "integration_mode": "git"}
    mock_ports.get_task.return_value = task
    mock_ports._parse_commit_result.return_value = {}

    run_res = MagicMock()
    run_res.returncode = 1
    run_res.stdout = "husky - pre-commit script failed (code 1)\nComplexity check failed! Commit blocked."
    run_res.stderr = ""
    mock_ports.subprocess.run.return_value = run_res

    res = git_finalize.finalize_completed_task(mock_ports, "t-hook")
    # Must be unrecoverable, non-retryable, and escalate
    assert res["retryable"] is False
    assert res["kind"] == "error"
    mock_ports._escalate_finalize.assert_called_once()
    escalate_reason = mock_ports._escalate_finalize.call_args[0][1]
    assert escalate_reason == "commit_fatal_error"


def test_pr_delivery_push_uses_extended_timeout():
    executed_timeouts = []

    def fake_subprocess_run(cmd, capture_output=True, text=True, timeout=None):
        executed_timeouts.append((cmd, timeout))
        res = MagicMock()
        res.returncode = 0
        res.stdout = "main"
        res.stderr = ""
        return res

    with patch("subprocess.run", side_effect=fake_subprocess_run):
        # Normal git call should use 30s
        pr_delivery._git("/dummy/repo", "rev-parse", "HEAD")
        assert executed_timeouts[-1][1] == 30

        # Push git call should use 120s
        pr_delivery._git("/dummy/repo", "push", "origin", "main")
        assert executed_timeouts[-1][1] == 120


def test_startup_wait_logging_is_throttled(capsys):
    from services.controller import advancement

    mock_ports = MagicMock()
    mock_ports.workflow_closed.return_value = False
    mock_ports.workflow_config_for.return_value = {"nodes": []}
    mock_ports.project_for_workflow.return_value = {"startup_ready": False}
    mock_ports._workflow_entry.return_value = {"status": "running"}
    mock_ports._get_store.return_value.get_workflow.return_value = {"status": "pending"}

    advancement.check_workflow_stage_advance(mock_ports, "wf-throttle-test")
    out1 = capsys.readouterr().out
    assert "[STARTUP WAIT] workflow=wf-throttle-test" in out1

    # Second call immediately after should be throttled (no output)
    advancement.check_workflow_stage_advance(mock_ports, "wf-throttle-test")
    out2 = capsys.readouterr().out
    assert "[STARTUP WAIT]" not in out2
