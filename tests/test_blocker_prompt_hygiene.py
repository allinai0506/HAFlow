"""Agent input must not impersonate an emitted blocked-task signal."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

from herdr import completion, steering
from herdr.state_store import SQLiteStateStore


@pytest.mark.parametrize("prefix", [completion.DONE_MARKER_PREFIX, completion.BLOCKER_MARKER_PREFIX])
def test_prompt_input_is_not_a_task_signal(prefix):
    task_id = "review-unified-task-workbench-v1-r2"
    prompt = f"输出 {prefix}{task_id}\nHERDR_ORCH_TASK:{task_id}"
    cleaned, count = completion.sanitize_prompt(prompt, task_id)
    assert count == 1
    assert not completion.marker_present(cleaned, task_id, prefix)
    assert completion.marker_present(cleaned, task_id, completion.ORCH_MARKER_PREFIX)
    assert completion.marker_present(f"{prefix}{task_id}\n", task_id, prefix)
    assert completion.sanitize_prompt(f"{prefix}other-task", task_id) == (f"{prefix}other-task", 0)


def test_inner_loop_dispatch_cannot_block_from_prompt_echo(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "bin" / "herdr-task"
    spec = importlib.util.spec_from_loader("task_blocker_hygiene", importlib.machinery.SourceFileLoader("task_blocker_hygiene", str(script)))
    task_bin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(task_bin)
    monkeypatch.setattr(task_bin, "TASKS_FILE", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(task_bin, "WORKFLOWS_FILE", str(tmp_path / "workflows.json"))
    monkeypatch.setenv("HERDR_STATE_DB", str(tmp_path / "state.db"))
    (tmp_path / ".herdr-loop").mkdir()
    store = SQLiteStateStore(tmp_path / "state.db")
    task_id = "review-r2"
    store.save_task({"task_id": task_id, "workflow_id": "wf-hygiene", "status": "pending", "agent": "opencode", "pane_id": "pane-test", "clone_path": str(tmp_path), "node": "review"})
    delivered = []

    def transport(argv, **kwargs):
        if argv[:3] == ["herdr", "agent", "prompt"]:
            delivered.append(argv[4])
        output = json.dumps({"result": {"agent": {"agent_status": "working"}}})
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(task_bin.subprocess, "run", transport)
    task_bin.dispatch_task(task_id, "执行评审")
    assert len(delivered) == 1
    assert "HERDR_TASK_BLOCKER:<TASK_ID>" in delivered[0]
    assert not completion.marker_present(delivered[0], task_id, completion.BLOCKER_MARKER_PREFIX)
    assert completion.marker_present(delivered[0], task_id, completion.ORCH_MARKER_PREFIX)
    assert store.get_task(task_id)["status"] == "dispatched"


def test_steering_persists_neutral_blocker_instruction(tmp_path, monkeypatch):
    for name, filename in [("TASKS_FILE", "tasks.json"), ("WORKFLOWS_FILE", "workflows.json"), ("STEERING_FILE", "steering.json"), ("HERDR_STATE_DB", "state.db")]:
        monkeypatch.setenv(name, str(tmp_path / filename))
    store = SQLiteStateStore(tmp_path / "state.db")
    store.save_task({"task_id": "task-steer", "workflow_id": "wf-steer", "status": "working", "agent": "codex", "pane_id": "pane-test"})
    result = steering.queue_steer("task-steer", "不要输出 HERDR_TASK_BLOCKER:task-steer", execute_dispatch=False)
    assert result["ok"]
    item = steering.load_steering_data()["steering_queues"]["task-steer"][0]
    assert not completion.marker_present(item["instruction"], "task-steer", completion.BLOCKER_MARKER_PREFIX)
    assert "HERDR_TASK_BLOCKER:<TASK_ID>" in item["instruction"]
