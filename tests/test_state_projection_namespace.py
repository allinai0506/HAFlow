"""An isolated state DB must not read or overwrite default host projections."""
import importlib.machinery
import importlib.util
import builtins
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


@pytest.mark.parametrize("kind", ["tasks", "workflows", "steering", "checkpoints"])
def test_missing_selected_companion_never_imports_host(tmp_path, monkeypatch, kind):
    host = tmp_path / 'host'
    selected = tmp_path / 'selected'
    host.mkdir()
    selected.mkdir()
    for name in ('WORKFLOWS_FILE', 'TASKS_FILE', 'STEERING_FILE', 'CHECKPOINTS_DIR'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(state_db, 'CONTROLLER_DIR', host)
    payload = {
        'tasks': {'tasks': [{'task_id': 'host-owned-task', 'workflow_id': 'host-wf', 'status': 'working'}]},
        'workflows': {'workflows': {'host-wf': {'status': 'running'}}},
        'steering': {'steering_queues': {'host-owned-task': [{'steer_id': 'host-steer', 'instruction': 'host instruction', 'status': 'pending'}]}},
        'checkpoints': {'checkpoint_id': 'host-cp', 'workflow_id': 'host-wf', 'tasks': []},
    }[kind]
    raw = json.dumps(payload)
    host_file = host / f'{kind}.json'
    if kind == 'checkpoints':
        (host / 'checkpoints').mkdir()
        host_file = host / 'checkpoints' / 'cp_host.json'
    host_file.write_text(raw)
    trigger = 'tasks' if kind == 'workflows' else 'workflows'
    trigger_records = [] if trigger == 'tasks' else {}
    (selected / f'{trigger}.json').write_text(json.dumps({trigger: trigger_records}))
    reads = []
    real_open = builtins.open
    def record_open(path, *args, **kwargs):
        if isinstance(path, (str, Path)):
            reads.append(Path(path))
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(builtins, 'open', record_open)
    store = SQLiteStateStore(selected / 'state.db', auto_migrate_json=True)
    assert host_file.read_text() == raw
    actual = {
        'tasks': store.get_task('host-owned-task'),
        'workflows': store.get_workflow('host-wf'),
        'steering': store.list_steers(task_id='host-owned-task'),
        'checkpoints': store.list_checkpoints('host-wf'),
    }[kind]
    assert host_file not in reads
    assert not actual


def test_explicit_opt_in_migration_source_remains_authoritative(namespaces, monkeypatch):
    host, selected = namespaces
    explicit = host / "requested-tasks.json"
    explicit.write_text(json.dumps({"tasks": [dict(task(), task_id="explicit-task")]}))
    monkeypatch.setenv("TASKS_FILE", str(explicit))
    (selected / "workflows.json").write_text('{"workflows": {}}')
    store = SQLiteStateStore(selected / "state.db", auto_migrate_json=True)
    assert store.get_task("explicit-task")["workflow_id"] == "selected-workflow"
