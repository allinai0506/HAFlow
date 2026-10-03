"""Regression tests for §1.7: Zombie obligations without replacement.

Verifies that:
1. `supersede_task` rejects invocations without either `--by` or `--abandon`.
2. `required_task_issues` detects zombie tasks (status=superseded, replacement_pending=True, superseded_by=None)
   and reports reason `zombie_obligation_unreplaced`.
3. `check_zombie_obligations` correctly scans and identifies zombie obligations across tasks.
"""
import json
import pytest
from herdr.scheduler import required_task_issues, node_is_complete


def test_required_task_issues_flags_zombie_unreplaced_obligation():
    tasks = [
        {"task_id": "test-old", "status": "superseded", "replacement_pending": True, "superseded_by": None},
        {"task_id": "test-new", "status": "completed", "stage_verdict": "pass"},
    ]
    issues = required_task_issues(tasks, None)
    assert len(issues) == 1
    assert issues[0]["task_id"] == "test-old"
    assert issues[0]["reason"] == "zombie_obligation_unreplaced"
    assert not node_is_complete(tasks)


def test_required_task_issues_passes_when_abandoned():
    tasks = [
        {"task_id": "test-old", "status": "superseded", "replacement_pending": False, "superseded_by": None},
        {"task_id": "test-new", "status": "completed", "stage_verdict": "pass", "integration_mode": "none"},
    ]
    issues = required_task_issues(tasks, None)
    assert issues == []
    assert node_is_complete(tasks)


def test_required_task_issues_passes_when_linked():
    tasks = [
        {"task_id": "test-old", "status": "superseded", "replacement_pending": True, "superseded_by": "test-new"},
        {"task_id": "test-new", "status": "completed", "stage_verdict": "pass", "integration_mode": "none"},
    ]
    issues = required_task_issues(tasks, None)
    assert issues == []
    assert node_is_complete(tasks)


def test_supersede_task_rejects_missing_by_and_abandon(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "bin/herdr-task"
    spec = importlib.util.spec_from_loader("herdr_task_zombie", importlib.machinery.SourceFileLoader("herdr_task_zombie", str(path)))
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(json.dumps({"tasks": [{"task_id": "t1", "status": "failed"}]}))
    monkeypatch.setattr(cli, "TASKS_FILE", str(tasks_file))

    with pytest.raises(SystemExit) as exc:
        cli.supersede_task("t1")
    assert exc.value.code == 2

    # Verify task was NOT modified
    after = json.loads(tasks_file.read_text())["tasks"][0]
    assert after["status"] == "failed"


def test_supersede_task_allows_abandon(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "bin/herdr-task"
    spec = importlib.util.spec_from_loader("herdr_task_zombie2", importlib.machinery.SourceFileLoader("herdr_task_zombie2", str(path)))
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(json.dumps({"tasks": [{"task_id": "t1", "status": "failed"}]}))
    monkeypatch.setattr(cli, "TASKS_FILE", str(tasks_file))

    cli.supersede_task("t1", abandon=True, reason="discard scope")
    after = json.loads(tasks_file.read_text())["tasks"][0]
    assert after["status"] == "superseded"
    assert after["replacement_pending"] is False
    assert after.get("abandon_reason") == "discard scope"


def test_check_zombie_obligations_detects_inventory():
    import importlib.machinery
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "bin/herdr-task"
    spec = importlib.util.spec_from_loader("herdr_task_zombie3", importlib.machinery.SourceFileLoader("herdr_task_zombie3", str(path)))
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    data = {
        "tasks": [
            {"task_id": "z1", "status": "superseded", "replacement_pending": True, "superseded_by": None},
            {"task_id": "ok1", "status": "superseded", "replacement_pending": True, "superseded_by": "ok2"},
            {"task_id": "ok2", "status": "completed"},
        ]
    }
    zombies = cli.check_zombie_obligations(data)
    assert len(zombies) == 1
    assert zombies[0]["task_id"] == "z1"
