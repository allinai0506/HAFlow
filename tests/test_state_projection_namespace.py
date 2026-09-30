"""An isolated state DB must not read or overwrite default host projections."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path

import pytest

from herdr import state_db, steering
from herdr.state_store import SQLiteStateStore, reset_state_store


@pytest.fixture
def namespaces(tmp_path, monkeypatch):
    host = tmp_path / "host" / ".herdr-controller"
    selected = tmp_path / "selected"
    host.mkdir(parents=True)
    selected.mkdir()
    for key in ["TASKS_FILE", "WORKFLOW_FILE", "WORKFLOWS_FILE", "STEERING_FILE", "CHECKPOINTS_DIR", "HERDR_STATE_DB"]:
        monkeypatch.delenv(key, raising=False)
    original_expand = os.path.expanduser
    monkeypatch.setattr(os.path, "expanduser", lambda value: str(host / value.split("/")[-1]) if value.startswith("~/.herdr-controller/") else original_expand(value))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "host"))
    monkeypatch.setattr(state_db, "CONTROLLER_DIR", host)
    monkeypatch.setenv("HERDR_STATE_DB", str(selected / "state.db"))
    reset_state_store()
    yield host, selected
    reset_state_store()


def task_cli():
    script = Path(__file__).resolve().parents[1] / "bin" / "herdr-task"
    spec = importlib.util.spec_from_loader("task_projection_namespace", importlib.machinery.SourceFileLoader("task_projection_namespace", str(script)))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def task():
    return {"task_id": "selected-task", "workflow_id": "selected-workflow", "status": "working", "agent": "opencode", "pane_id": "test-pane"}


def test_cli_save_uses_selected_db_projection(namespaces):
    host, selected = namespaces
    host_projection = host / "tasks.json"
    host_projection.write_text('{"host":"must remain"}')
    cli = task_cli()
    cli.save_tasks({"tasks": [task()]})
    assert host_projection.read_text() == '{"host":"must remain"}'
    data = json.loads((selected / "tasks.json").read_text())
    assert [t["task_id"] for t in data["tasks"]] == ["selected-task"]
    assert cli._get_store().get_task("selected-task")["status"] == "working"


def test_steering_queue_uses_selected_db_projection(namespaces):
    host, selected = namespaces
    host_projection = host / "steering.json"
    host_projection.write_text('{"host":"must remain"}')
    store = SQLiteStateStore(selected / "state.db")
    store.save_task(task())
    assert steering.queue_steer("selected-task", "继续验收", execute_dispatch=False)["ok"]
    assert host_projection.read_text() == '{"host":"must remain"}'
    data = json.loads((selected / "steering.json").read_text())
    assert len(data["steering_queues"]["selected-task"]) == 1
    steering.save_tasks_data({"tasks": [task()]})
    assert (selected / "tasks.json").exists()
    assert not (host / "tasks.json").exists()


def test_export_all_defaults_to_store_namespace(namespaces):
    host, selected = namespaces
    store = SQLiteStateStore(selected / "state.db")
    store.save_task(task())
    receipt = store.export_all_json()
    assert Path(receipt["target_dir"]) == selected
    assert not (host / "tasks.json").exists()
    assert json.loads((selected / "tasks.json").read_text())["tasks"][0]["task_id"] == "selected-task"
    explicit = host / "requested-export"
    assert Path(store.export_all_json(explicit)["target_dir"]) == explicit


@pytest.mark.parametrize("local_projection", [False, True])
def test_opt_in_migration_reads_selected_namespace(namespaces, local_projection):
    host, selected = namespaces
    (host / "tasks.json").write_text(json.dumps({"tasks": [dict(task(), task_id="host-task")]}))
    if local_projection:
        (selected / "tasks.json").write_text(json.dumps({"tasks": [task()]}))
    store = SQLiteStateStore(selected / "state.db", auto_migrate_json=True)
    assert (store.get_task("selected-task") is not None) == local_projection
    assert store.get_task("host-task") is None


def test_explicit_cli_and_steering_destinations_remain_authoritative(namespaces, monkeypatch):
    host, selected = namespaces
    cli = task_cli()
    explicit = host / "requested-tasks.json"
    monkeypatch.setattr(cli, "TASKS_FILE", str(explicit))
    cli.save_tasks({"tasks": [task()]})
    assert json.loads(explicit.read_text())["tasks"][0]["task_id"] == "selected-task"
    monkeypatch.setenv("TASKS_FILE", str(explicit))
    monkeypatch.setenv("STEERING_FILE", str(host / "requested-steering.json"))
    assert steering.get_tasks_file() == explicit
    assert steering.get_steering_file() == host / "requested-steering.json"


def test_explicit_workflows_file_selects_db_without_state_override(namespaces, monkeypatch):
    host, selected = namespaces
    monkeypatch.delenv("HERDR_STATE_DB")
    monkeypatch.setenv("WORKFLOWS_FILE", str(selected / "workflows.json"))
    cli = task_cli()
    assert cli._get_store().db_path == selected / "state.db"
