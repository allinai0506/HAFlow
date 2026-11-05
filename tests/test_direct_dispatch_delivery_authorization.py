"""Dispatch authorization must use the configuration store and fail closed."""
import sqlite3
from types import SimpleNamespace

import pytest

from herdr import projects, state_store
from herdr.direct_dispatch import _prompt


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch, tmp_path):
    for key in ("HERDR_STATE_DB", "TASKS_FILE", "WORKFLOWS_FILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(projects, "ROOT", tmp_path)
    monkeypatch.setattr(projects, "WORKFLOWS_FILE", tmp_path / "workflows.json")
    monkeypatch.setattr(projects, "TASKS_FILE", tmp_path / "tasks.json", raising=False)
    state_store.reset_state_store()
    monkeypatch.setenv("HERDR_STATE_DB", str(tmp_path / "state.db"))
    yield
    state_store.reset_state_store()


def prompt(skills="missing", **fields):
    node = dict(id="wrapup", label="收尾", workflow_id="wf-auth", purpose="wrapup",
                required_outputs=["report"], rules=[], **fields)
    if skills != "missing":
        node["skills"] = skills
    return _prompt(node, "req", "goal", ["acceptance"])


def assert_ceiling(text, ceiling):
    assert f"交付授权: {ceiling}（不得超越此上限）" in text
    if ceiling == "workspace":
        assert "交付授权: pr（不得超越此上限）" not in text


def fail(*args, **kwargs):
    raise RuntimeError("store unavailable")


@pytest.mark.parametrize("skills", ["missing", None, []])
def test_default_binding_node_config_read_failure_fails_closed_to_workspace(monkeypatch, skills):
    monkeypatch.setattr(projects, "workflow_config_for", fail)
    # Configuration failure is already conclusive; no store access is needed.
    monkeypatch.setattr(projects, "workflow_state_store", fail, raising=False)
    assert_ceiling(prompt(skills), "workspace")


@pytest.mark.parametrize("skills", ["missing", None, []])
@pytest.mark.parametrize("failure", ["open", "record", "snapshot"])
def test_default_binding_store_failures_fail_closed(monkeypatch, skills, failure):
    store = SimpleNamespace(get_workflow=lambda _: {"workflow_id": "wf-auth"})
    if failure == "snapshot":
        def bad_snapshot(_):
            raise ValueError("snapshot corrupt")
        store.read_workflow_snapshot = bad_snapshot
        # Real workflow_config_for must swallow ValueError before the probe.
    else:
        monkeypatch.setattr(projects, "workflow_config_for", lambda _: {
            "name": "software-development-v1", "delivery_ceiling_default": "pr"})
    if failure == "record":
        store.get_workflow = fail
    monkeypatch.setattr(projects, "_get_store", fail if failure == "open" else lambda: store)
    assert_ceiling(prompt(skills), "workspace")


@pytest.mark.parametrize("skills", ["missing", None, []])
def test_normal_nonstaged_does_not_inject(monkeypatch, skills):
    monkeypatch.setattr(projects, "workflow_config_for", lambda _: {"name": "custom"})
    monkeypatch.setattr(projects, "_get_store", fail)
    text = prompt(skills)
    assert "交付授权:" not in text
    assert "Stage Execution Context" not in text


@pytest.mark.parametrize("record", [None, {}, {"delivery_ceiling": ""}])
def test_missing_record_keeps_template_default_pr(monkeypatch, record):
    monkeypatch.setattr(projects, "workflow_config_for", lambda _: {
        "name": "software-development-v1", "delivery_ceiling_default": "pr"})
    monkeypatch.setattr(projects, "_get_store", lambda: SimpleNamespace(get_workflow=lambda _: record))
    assert_ceiling(prompt(), "pr")


@pytest.mark.parametrize("mode", ["custom", "workflows", "tasks", "explicit"])
def test_custom_workflows_file_reads_run_level_ceiling(monkeypatch, tmp_path, mode):
    monkeypatch.delenv("HERDR_STATE_DB")
    other = tmp_path / "other"
    other.mkdir()
    if mode == "custom":
        monkeypatch.setenv("WORKFLOWS_FILE", str(other / "custom.json"))
        selected = other / "custom.db"
    elif mode == "workflows":
        monkeypatch.setenv("WORKFLOWS_FILE", str(other / "workflows.json"))
        selected = other / "state.db"
    elif mode == "tasks":
        monkeypatch.setenv("TASKS_FILE", str(other / "custom-tasks.json"))
        selected = other / "custom-tasks.db"
    else:
        monkeypatch.setenv("WORKFLOWS_FILE", str(other / "ignored.json"))
        monkeypatch.setenv("TASKS_FILE", str(other / "also-ignored.json"))
        selected = other / "explicit.db"
        monkeypatch.setenv("HERDR_STATE_DB", str(selected))
    state_store.get_state_store(tmp_path / "state.db").save_workflow(
        {"workflow_id": "wf-auth", "delivery_ceiling": "pr"})
    store = state_store.get_state_store(selected)
    store.save_workflow({"workflow_id": "wf-auth", "delivery_ceiling": "workspace"})
    monkeypatch.setattr(projects, "workflow_config_for", lambda _: {
        "name": "software-development-v1", "delivery_ceiling_default": "pr"})
    assert projects.workflow_state_store().db_path == selected
    assert_ceiling(prompt(), "workspace")


def test_real_unreadable_snapshot_fails_closed(tmp_path):
    store = projects._get_store()
    store.save_workflow({"workflow_id": "wf-auth", "delivery_ceiling": "pr"})
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE workflows SET config_json = ? WHERE workflow_id = ?",
                     ("{broken", "wf-auth"))
    assert projects.workflow_config_for("wf-auth") is None
    assert_ceiling(prompt(), "workspace")


def test_legacy_empty_snapshot_is_default_not_failure():
    store = projects.workflow_state_store()
    store.save_workflow({"workflow_id": "wf-auth"})
    assert projects.workflow_config_for("wf-auth") is None
    assert not projects.workflow_snapshot_unreadable("wf-auth")
    assert_ceiling(prompt(["six-step-finish"]), "pr")
