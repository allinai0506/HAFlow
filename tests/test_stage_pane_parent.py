"""A stage tab keeps a pane, and does not keep a spare idle Anchor beside work."""

import importlib.machinery
import importlib.util
import subprocess
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_worker(name):
    path = ROOT / "services" / "herdr-worker.py"
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, str(path))
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolve(monkeypatch, panes, stage, project_root="/proj"):
    from herdr import topology

    monkeypatch.setattr(topology, "_panes", lambda _workspace: panes)
    calls = []

    def transport(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(topology.subprocess, "run", transport)
    pane_id, changed = topology._resolve_or_create_anchor(
        "w", "tab", project_root, stage
    )
    assert not any(cmd[1:3] == ["pane", "split"] for cmd in calls)
    assert not any(cmd[1:3] == ["pane", "rename"] for cmd in calls)
    return pane_id, changed, calls


def test_only_shell_stays_and_is_not_replaced(monkeypatch):
    pane_id, changed, calls = resolve(
        monkeypatch,
        [{"pane_id": "root", "tab_id": "tab", "cwd": "/proj", "label": "Anchor"}],
        {"anchor_pane_id": "root"},
    )
    assert pane_id == "root" and changed is False and calls == []


def test_idle_shell_is_closed_when_a_worker_pane_exists(monkeypatch):
    pane_id, changed, calls = resolve(
        monkeypatch,
        [
            {"pane_id": "idle", "tab_id": "tab", "cwd": "/proj", "label": "Anchor"},
            {"pane_id": "work", "tab_id": "tab", "cwd": "/clones/task", "label": ""},
        ],
        {"anchor_pane_id": "idle"},
    )
    assert pane_id == "work" and changed is True
    assert calls == [["herdr", "pane", "close", "idle"]]


def test_cached_worker_is_kept_without_a_new_shell(monkeypatch):
    pane_id, changed, calls = resolve(
        monkeypatch,
        [{
            "pane_id": "work",
            "tab_id": "tab",
            "cwd": "/clones/task",
            "agent": {"name": "qodercli"},
        }],
        {"anchor_pane_id": "work"},
    )
    assert pane_id == "work" and changed is False and calls == []


def test_missing_tab_panes_are_not_replaced_by_a_split(monkeypatch):
    from herdr import topology

    monkeypatch.setattr(topology, "_panes", lambda _workspace: [])
    with pytest.raises(RuntimeError, match="no panes"):
        topology._resolve_or_create_anchor("w", "tab", "/proj", {})


def test_idle_parent_becomes_the_task_pane():
    worker = load_worker("worker_reuse_idle_parent")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["agent", "get"]:
            return subprocess.CompletedProcess(argv, 1, "", "no agent")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def run_json(argv, timeout=125):
        if argv[1:3] == ["pane", "get"]:
            return {"result": {"pane": {"pane_id": "parent", "label": "Anchor"}}}
        raise AssertionError(argv)

    with patch.object(worker, "run_json", side_effect=run_json), \
            patch.object(worker.subprocess, "run", side_effect=run), \
            patch.object(worker, "prepare_existing_pane") as prepare, \
            patch("herdr.task_resources.workflow_launch_lock", lambda *a, **k: nullcontext()), \
            patch("herdr.state_store.get_state_store", lambda: type("S", (), {"db_path": "db"})()):
        pane_id, source = worker.allocate_task_pane("parent", "/clone", None, "task-1")

    assert pane_id == "parent" and source == "prebuilt"
    prepare.assert_called_once_with("parent", "/clone")
    assert ["herdr", "pane", "rename", "parent", "task:task-1"] in calls
    assert not any(cmd[1:3] == ["pane", "split"] for cmd in calls)


def test_busy_parent_splits_a_new_pane():
    worker = load_worker("worker_split_busy_parent")

    def run(argv, **kwargs):
        if argv[1:3] == ["agent", "get"]:
            return subprocess.CompletedProcess(
                argv, 0, '{"result":{"agent":{"name":"qodercli"}}}', ""
            )
        raise AssertionError(argv)

    def run_json(argv, timeout=125):
        if argv[1:3] == ["pane", "get"]:
            return {"result": {"pane": {"pane_id": "parent", "label": "task:old"}}}
        if argv[1:3] == ["pane", "split"]:
            return {"result": {"pane": {"pane_id": "child"}}}
        raise AssertionError(argv)

    with patch.object(worker, "run_json", side_effect=run_json), \
            patch.object(worker.subprocess, "run", side_effect=run), \
            patch.object(worker, "prepare_existing_pane") as prepare, \
            patch("herdr.task_resources.workflow_launch_lock", lambda *a, **k: nullcontext()), \
            patch("herdr.state_store.get_state_store", lambda: type("S", (), {"db_path": "db"})()):
        pane_id, source = worker.allocate_task_pane("parent", "/clone", None, "task-2")

    assert pane_id == "child" and source == "dynamic"
    prepare.assert_not_called()
