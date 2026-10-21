"""Template policy survives real project assembly and immutable Run registration."""

import json
import subprocess
from pathlib import Path

import pytest

from herdr import projects, workflow, workflow_docs
from herdr.state_store import SQLiteStateStore


@pytest.fixture
def project_environment(tmp_path, monkeypatch):
    """Replace only terminal transport; keep template/Git/project/SQLite paths real."""
    control_root = tmp_path / "controller"
    template_root = tmp_path / "templates"
    template_root.mkdir()
    monkeypatch.setattr(projects, "ROOT", control_root)
    monkeypatch.setattr(projects, "PROJECTS_FILE", control_root / "projects.json")
    monkeypatch.setattr(projects, "WORKFLOWS_FILE", control_root / "workflows.json")
    monkeypatch.setattr(workflow, "USER_TEMPLATES_DIR", template_root)
    monkeypatch.setenv(workflow_docs.DOCS_DIR_ENV, str(control_root / "workflows"))
    store = SQLiteStateStore(db_path=control_root / "state.db")
    monkeypatch.setattr(projects, "_get_store", lambda: store)
    real_run = projects._run
    tab_number = 0

    def run(cmd, check=True):
        if cmd[0] == "git":
            return real_run(cmd, check=check)
        assert cmd[:3] in (
            ["herdr", "tab", "rename"], ["herdr", "pane", "rename"],
            ["herdr", "tab", "close"], ["herdr", "workspace", "get"],
            ["herdr", "pane", "get"],
        ), cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    def run_json(cmd):
        nonlocal tab_number
        if cmd[:3] == ["herdr", "workspace", "create"]:
            return {"result": {
                "workspace": {"workspace_id": "test-workspace"},
                "tab": {"tab_id": "coordinator-tab"},
                "root_pane": {"pane_id": "coordinator-pane"},
            }}
        if cmd[:3] == ["herdr", "tab", "create"]:
            tab_number += 1
            return {"result": {
                "tab": {"tab_id": f"node-tab-{tab_number}"},
                "root_pane": {"pane_id": f"node-pane-{tab_number}"},
            }}
        if cmd[:3] == ["herdr", "agent", "start"]:
            return {"result": {}}
        if cmd[:3] == ["herdr", "tab", "list"]:
            return {"result": {"tabs": [{"tab_id": "coordinator-tab", "label": "1总指挥"}]}}
        if cmd[:3] == ["herdr", "pane", "list"]:
            return {"result": {"panes": [{
                "tab_id": "coordinator-tab", "pane_id": "coordinator-pane", "label": "总指挥",
            }]}}
        raise AssertionError(f"unexpected terminal call: {cmd}")

    monkeypatch.setattr(projects, "_run", run)
    monkeypatch.setattr(projects, "_run_json", run_json)
    return store, template_root


def write_template(template_root, name, execution_mode, control_mode):
    definition = {"name": name, "execution": {"mode": execution_mode},
                  "nodes": [{"id": "build"}]}
    if control_mode is not None:
        definition["control_policy"] = {"mode": control_mode}
    (template_root / f"{name}.json").write_text(json.dumps(definition), encoding="utf-8")


def project_root(tmp_path, execution_mode):
    root = tmp_path / "project"
    root.mkdir()
    if execution_mode == "git":
        subprocess.run(["git", "init", "-b", "main", str(root)], check=True,
                       capture_output=True, text=True)
    return root


@pytest.mark.parametrize("execution_mode", ["git", "context"])
@pytest.mark.parametrize("control_mode,expected_mode", [
    ("supervised", "confirmation_required"), ("automatic", "automatic"),
])
def test_control_policy_survives_template_project_registration(
    project_environment, tmp_path, execution_mode, control_mode, expected_mode,
):
    store, template_root = project_environment
    write_template(template_root, "policy-template", execution_mode, control_mode)
    root = project_root(tmp_path, execution_mode)
    project = projects.create_project(root, template_name="policy-template")
    projects.register_workflow("wf-policy-created", project)

    cfg = projects.workflow_config_for("wf-policy-created")
    assert cfg.get("control_policy") == {"mode": control_mode}
    assert workflow.resolve_control_policy(cfg, "build", "advance") == {
        "mode": expected_mode, "source": "workflow",
    }
    shared = json.loads(Path(project["workflow_file"]).read_text(encoding="utf-8"))
    assert shared["control_policy"] == {"mode": control_mode}
    assert cfg["execution"] == {"mode": execution_mode}
    assert store.get_workflow("wf-policy-created")["workflow_file"] != project["workflow_file"]


@pytest.mark.parametrize("execution_mode", ["git", "context"])
@pytest.mark.parametrize("old_mode,new_mode,expected", [
    ("supervised", "automatic", {"mode": "automatic", "source": "workflow"}),
    ("automatic", "supervised", {"mode": "confirmation_required", "source": "workflow"}),
    ("supervised", None, {"mode": "automatic", "source": "default"}),
])
def test_control_policy_template_switch_pins_each_run(
    project_environment, tmp_path, execution_mode, old_mode, new_mode, expected,
):
    store, template_root = project_environment
    write_template(template_root, "policy-before", execution_mode, old_mode)
    write_template(template_root, "policy-after", execution_mode, new_mode)
    root = project_root(tmp_path, execution_mode)
    before = projects.create_project(root, template_name="policy-before")
    projects.register_workflow("wf-policy-before", before)
    store.transition_workflow("wf-policy-before", "completed", "test finished", source="test", force=True)

    after = projects.create_project(root, template_name="policy-after")
    projects.register_workflow("wf-policy-after", after)
    cfg_before = projects.workflow_config_for("wf-policy-before")
    cfg_after = projects.workflow_config_for("wf-policy-after")
    assert workflow.resolve_control_policy(cfg_after, "build", "advance") == expected
    assert cfg_after.get("control_policy") == ({"mode": new_mode} if new_mode else None)
    assert cfg_before.get("control_policy") == {"mode": old_mode}
    assert workflow.resolve_control_policy(cfg_before, "build", "advance") == {
        "mode": "confirmation_required" if old_mode == "supervised" else "automatic",
        "source": "workflow",
    }
    assert after["workspace_id"] == before["workspace_id"]
    assert after["coordinator_pane_id"] == before["coordinator_pane_id"]


@pytest.mark.parametrize("control_mode,expected_mode", [
    ("supervised", "confirmation_required"), ("automatic", "automatic"),
])
def test_control_policy_survives_workspace_adoption(
    project_environment, tmp_path, control_mode, expected_mode,
):
    _, template_root = project_environment
    write_template(template_root, "policy-adopt", "git", control_mode)
    root = project_root(tmp_path, "git")
    project = projects.adopt_workspace_as_project("test-workspace", root, template_name="policy-adopt")
    projects.register_workflow("wf-policy-adopted", project)
    cfg = projects.workflow_config_for("wf-policy-adopted")
    assert cfg.get("control_policy") == {"mode": control_mode}
    assert workflow.resolve_control_policy(cfg, "build", "advance") == {
        "mode": expected_mode, "source": "workflow",
    }
