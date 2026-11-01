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


def test_delivery_ceiling_survives_template_project_registration(
    project_environment, tmp_path,
):
    from herdr.review_closure import closure_required

    store, template_root = project_environment
    definition = {
        "name": "ceiling-template",
        "delivery_ceiling_default": "workspace",
        "nodes": [{"id": "wrapup", "review_closure_required": True}],
    }
    (template_root / "ceiling-template.json").write_text(json.dumps(definition), encoding="utf-8")

    root = project_root(tmp_path, "git")
    project = projects.create_project(root, template_name="ceiling-template")
    assert project.get("delivery_ceiling_default") == "workspace"

    shared = json.loads(Path(project["workflow_file"]).read_text(encoding="utf-8"))
    assert shared.get("delivery_ceiling_default") == "workspace"

    projects.register_workflow("wf-ceiling-created", project)

    cfg = projects.workflow_config_for("wf-ceiling-created")
    assert cfg.get("delivery_ceiling_default") == "workspace"
    assert store.get_workflow("wf-ceiling-created")["config"].get("delivery_ceiling_default") == "workspace"

    wrapup_task = {
        "task_id": "wf-ceiling-created-wrapup",
        "workflow_id": "wf-ceiling-created",
        "node": "wrapup",
        "clone_path": str(root),
    }
    assert not closure_required(store, wrapup_task)


def test_delivery_ceiling_backfilled_for_existing_project(
    project_environment, tmp_path,
):
    from herdr.review_closure import closure_required

    store, template_root = project_environment
    definition = {
        "name": "software-dev-legacy",
        "delivery_ceiling_default": "workspace",
        "nodes": [{"id": "wrapup", "review_closure_required": True}],
    }
    (template_root / "software-dev-legacy.json").write_text(json.dumps(definition), encoding="utf-8")

    root = project_root(tmp_path, "git")
    control_root = tmp_path / "controller"
    project_dir = control_root / "projects" / "p-legacy"
    project_dir.mkdir(parents=True, exist_ok=True)
    wf_file = project_dir / "workflow.json"
    legacy_wf = {
        "project_id": "p-legacy",
        "project_name": "legacy",
        "project_root": str(root),
        "base_branch": "main",
        "workspace_id": "test-workspace",
        "workflow_template": "software-dev-legacy",
        "nodes": [{"id": "wrapup", "review_closure_required": True}],
    }
    wf_file.write_text(json.dumps(legacy_wf), encoding="utf-8")
    legacy_project = {
        "project_id": "p-legacy",
        "project_name": "legacy",
        "project_root": str(root),
        "base_branch": "main",
        "workspace_id": "test-workspace",
        "coordinator_pane_id": "coordinator-pane",
        "workflow_file": str(wf_file),
    }

    projects.register_workflow("wf-legacy-01", legacy_project)

    cfg = projects.workflow_config_for("wf-legacy-01")
    assert cfg.get("delivery_ceiling_default") == "workspace"

    wrapup_task = {
        "task_id": "wf-legacy-01-wrapup",
        "workflow_id": "wf-legacy-01",
        "node": "wrapup",
        "clone_path": str(root),
    }
    assert not closure_required(store, wrapup_task)


def test_delivery_ceiling_survives_workspace_adoption_and_reprovisioning(
    project_environment, tmp_path,
):
    store, template_root = project_environment
    def_a = {
        "name": "tmpl-a",
        "delivery_ceiling_default": "workspace",
        "nodes": [{"id": "step_a"}],
    }
    def_b = {
        "name": "tmpl-b",
        "delivery_ceiling_default": "worktree",
        "nodes": [{"id": "step_b"}],
    }
    (template_root / "tmpl-a.json").write_text(json.dumps(def_a), encoding="utf-8")
    (template_root / "tmpl-b.json").write_text(json.dumps(def_b), encoding="utf-8")

    root = project_root(tmp_path, "git")
    proj_a = projects.adopt_workspace_as_project("test-workspace", root, template_name="tmpl-a")
    assert proj_a.get("delivery_ceiling_default") == "workspace"
    shared_a = json.loads(Path(proj_a["workflow_file"]).read_text(encoding="utf-8"))
    assert shared_a.get("delivery_ceiling_default") == "workspace"

    proj_b = projects.reprovision_project_template(proj_a, "tmpl-b")
    assert proj_b.get("delivery_ceiling_default") == "worktree"
    shared_b = json.loads(Path(proj_b["workflow_file"]).read_text(encoding="utf-8"))
    assert shared_b.get("delivery_ceiling_default") == "worktree"


def test_explicit_wrapup_delivery_ceiling_not_overridden_by_template_default(
    project_environment, tmp_path,
):
    from herdr.review_closure import closure_required

    store, template_root = project_environment
    definition = {
        "name": "ceiling-template",
        "delivery_ceiling_default": "workspace",
        "nodes": [{"id": "wrapup", "review_closure_required": True}],
    }
    (template_root / "ceiling-template.json").write_text(json.dumps(definition), encoding="utf-8")

    root = project_root(tmp_path, "git")
    project = projects.create_project(root, template_name="ceiling-template")

    # Author custom workflow definition on project's workflow_file
    custom_wf = {
        "project_id": project["project_id"],
        "workflow_template": "ceiling-template",
        "nodes": [{"id": "wrapup", "delivery_ceiling": "pr", "review_closure_required": True}],
    }
    Path(project["workflow_file"]).write_text(json.dumps(custom_wf), encoding="utf-8")

    projects.register_workflow("wf-wrapup-explicit-pr", project)

    cfg = projects.workflow_config_for("wf-wrapup-explicit-pr")
    assert "delivery_ceiling_default" not in cfg

    wrapup_task = {
        "task_id": "wf-wrapup-explicit-pr-wrapup",
        "workflow_id": "wf-wrapup-explicit-pr",
        "node": "wrapup",
        "clone_path": str(root),
    }
    assert closure_required(store, wrapup_task) is True
def test_replay_run_registration_preserves_frozen_snapshot_without_backfill(
    project_environment, tmp_path,
):
    store, template_root = project_environment
    definition = {
        "name": "ceiling-template",
        "delivery_ceiling_default": "workspace",
        "nodes": [{"id": "wrapup", "review_closure_required": True}],
    }
    (template_root / "ceiling-template.json").write_text(json.dumps(definition), encoding="utf-8")

    root = project_root(tmp_path, "git")
    project = projects.create_project(root, template_name="ceiling-template")

    frozen_file = tmp_path / "replay_workflow.json"
    frozen_def = {
        "project_id": project["project_id"],
        "workflow_template": "ceiling-template",
        "nodes": [{"id": "wrapup", "review_closure_required": True}],
    }
    frozen_file.write_text(json.dumps(frozen_def), encoding="utf-8")

    projects.register_workflow(
        "wf-replay-run",
        project,
        workflow_file=str(frozen_file),
    )

    registered_cfg = projects.workflow_config_for("wf-replay-run")
    assert "delivery_ceiling_default" not in registered_cfg
    assert "delivery_ceiling" not in registered_cfg


def test_ensure_project_backfill_failure_preserves_original_file(
    project_environment, tmp_path, monkeypatch,
):
    store, template_root = project_environment
    definition = {
        "name": "atomic-template",
        "delivery_ceiling_default": "workspace",
        "nodes": [{"id": "wrapup", "review_closure_required": True}],
    }
    (template_root / "atomic-template.json").write_text(json.dumps(definition), encoding="utf-8")

    root = project_root(tmp_path, "git")
    project = projects.create_project(root, template_name="atomic-template")
    wf_path = Path(project["workflow_file"])

    # Simulate legacy project file without ceiling
    original_cfg = json.loads(wf_path.read_text(encoding="utf-8"))
    original_cfg.pop("delivery_ceiling_default", None)
    wf_path.write_text(json.dumps(original_cfg), encoding="utf-8")

    monkeypatch.setattr(projects, "_coordinator_alive", lambda pane_id: True)

    # Simulate write failure during _save
    def failing_save(path, data):
        raise OSError("Simulated disk error during save")

    monkeypatch.setattr(projects, "_save", failing_save)

    # ensure_project catches exception; original file must remain intact and valid JSON
    projects.ensure_project(root, template_name="atomic-template")

    reloaded = json.loads(wf_path.read_text(encoding="utf-8"))
    assert reloaded == original_cfg


def test_create_project_preserves_explicit_wrapup_ceiling_over_template_default(
    project_environment, tmp_path,
):
    from herdr.review_closure import closure_required

    store, template_root = project_environment
    definition = {
        "name": "explicit-wrapup-template",
        "delivery_ceiling_default": "workspace",
        "nodes": [
            {
                "id": "wrapup",
                "delivery_ceiling": "pr",
                "review_closure_required": True,
            }
        ],
    }
    (template_root / "explicit-wrapup-template.json").write_text(
        json.dumps(definition), encoding="utf-8"
    )

    root = project_root(tmp_path, "git")
    project = projects.create_project(root, template_name="explicit-wrapup-template")
    projects.register_workflow("wf-explicit-wrapup-created", project)

    wrapup_task = {
        "task_id": "wf-explicit-wrapup-created-wrapup",
        "workflow_id": "wf-explicit-wrapup-created",
        "node": "wrapup",
        "clone_path": str(root),
    }
    assert closure_required(store, wrapup_task) is True
def test_ensure_project_backfills_explicit_template_ceiling_alongside_default(
    project_environment, tmp_path, monkeypatch,
):
    from herdr.review_closure import closure_required

    store, template_root = project_environment
    definition = {
        "name": "explicit-template",
        "delivery_ceiling_default": "workspace",
        "delivery_ceiling": "pr",
        "nodes": [{"id": "wrapup", "review_closure_required": True}],
    }
    (template_root / "explicit-template.json").write_text(
        json.dumps(definition), encoding="utf-8"
    )

    root = project_root(tmp_path, "git")
    project = projects.create_project(root, template_name="explicit-template")
    wf_path = Path(project["workflow_file"])

    # Simulate legacy project file that lost ceilings
    cfg = json.loads(wf_path.read_text(encoding="utf-8"))
    cfg.pop("delivery_ceiling_default", None)
    cfg.pop("delivery_ceiling", None)
    wf_path.write_text(json.dumps(cfg), encoding="utf-8")

    monkeypatch.setattr(projects, "_coordinator_alive", lambda pane_id: True)
    updated = projects.ensure_project(root, template_name="explicit-template")
    assert updated.get("delivery_ceiling") == "pr"
    assert updated.get("delivery_ceiling_default") == "workspace"

    projects.register_workflow("wf-legacy-explicit-ceiling", updated)
    registered_cfg = projects.workflow_config_for("wf-legacy-explicit-ceiling")
    assert registered_cfg.get("delivery_ceiling") == "pr"

    wrapup_task = {
        "task_id": "wf-legacy-explicit-ceiling-wrapup",
        "workflow_id": "wf-legacy-explicit-ceiling",
        "node": "wrapup",
        "clone_path": str(root),
    }
    assert closure_required(store, wrapup_task) is True

def test_ensure_project_and_register_workflow_backfill_template_wrapup_ceiling(
    project_environment, tmp_path, monkeypatch,
):
    from herdr.review_closure import closure_required

    store, template_root = project_environment
    definition = {
        "name": "template-wrapup-pr",
        "delivery_ceiling_default": "workspace",
        "nodes": [
            {
                "id": "wrapup",
                "delivery_ceiling": "pr",
                "review_closure_required": True,
            }
        ],
    }
    (template_root / "template-wrapup-pr.json").write_text(
        json.dumps(definition), encoding="utf-8"
    )

    root = project_root(tmp_path, "git")
    project = projects.create_project(root, template_name="template-wrapup-pr")
    wf_path = Path(project["workflow_file"])

    # Simulate legacy definition created by old normalizer that lost wrapup.delivery_ceiling
    legacy_cfg = json.loads(wf_path.read_text(encoding="utf-8"))
    legacy_cfg.pop("delivery_ceiling_default", None)
    legacy_cfg.pop("delivery_ceiling", None)
    for node in legacy_cfg.get("nodes", []):
        if node.get("id") == "wrapup":
            node.pop("delivery_ceiling", None)
            node.pop("delivery_ceiling_default", None)
    wf_path.write_text(json.dumps(legacy_cfg), encoding="utf-8")

    monkeypatch.setattr(projects, "_coordinator_alive", lambda pane_id: True)

    # ensure_project should backfill wrapup's delivery_ceiling from template
    updated_project = projects.ensure_project(root, template_name="template-wrapup-pr")
    projects.register_workflow("wf-legacy-wrapup-pr", updated_project)

    wrapup_task = {
        "task_id": "wf-legacy-wrapup-pr-wrapup",
        "workflow_id": "wf-legacy-wrapup-pr",
        "node": "wrapup",
        "clone_path": str(root),
    }
    assert closure_required(store, wrapup_task) is True


def test_explicit_node_ceiling_not_overridden_by_template_root_explicit_ceiling(
    project_environment, tmp_path, monkeypatch,
):
    from herdr.review_closure import closure_required

    store, template_root = project_environment
    template_def = {
        "name": "template-root-workspace",
        "delivery_ceiling": "workspace",
        "nodes": [
            {
                "id": "wrapup",
                "review_closure_required": True,
            }
        ],
    }
    (template_root / "template-root-workspace.json").write_text(
        json.dumps(template_def), encoding="utf-8"
    )

    root = project_root(tmp_path, "git")
    project = projects.create_project(root, template_name="template-root-workspace")
    wf_path = Path(project["workflow_file"])

    cfg = json.loads(wf_path.read_text(encoding="utf-8"))
    cfg.pop("delivery_ceiling", None)
    cfg.pop("delivery_ceiling_default", None)
    for node in cfg.get("nodes", []):
        if node.get("id") == "wrapup":
            node["delivery_ceiling"] = "pr"
    wf_path.write_text(json.dumps(cfg), encoding="utf-8")

    # Case 1: project record has delivery_ceiling="workspace" (cached on record)
    assert project.get("delivery_ceiling") == "workspace"
    projects.register_workflow("wf-explicit-node-with-project-ceiling", project)
    registered_cfg_1 = projects.workflow_config_for("wf-explicit-node-with-project-ceiling")
    assert registered_cfg_1.get("delivery_ceiling") is None
    wrapup_task_1 = {
        "task_id": "wf-explicit-node-with-project-ceiling-wrapup",
        "workflow_id": "wf-explicit-node-with-project-ceiling",
        "node": "wrapup",
        "clone_path": str(root),
    }
    assert closure_required(store, wrapup_task_1) is True

    # Case 2: project record has no delivery_ceiling, so register_workflow falls back to template
    project_no_ceiling = dict(project)
    project_no_ceiling.pop("delivery_ceiling", None)
    projects.register_workflow("wf-explicit-node-with-template-ceiling", project_no_ceiling)
    registered_cfg_2 = projects.workflow_config_for("wf-explicit-node-with-template-ceiling")
    assert registered_cfg_2.get("delivery_ceiling") is None
    wrapup_task_2 = {
        "task_id": "wf-explicit-node-with-template-ceiling-wrapup",
        "workflow_id": "wf-explicit-node-with-template-ceiling",
        "node": "wrapup",
        "clone_path": str(root),
    }
    assert closure_required(store, wrapup_task_2) is True

    # Case 3: ensure_project legacy backfill should also not inject root delivery_ceiling
    monkeypatch.setattr(projects, "_coordinator_alive", lambda pane_id: True)
    updated_project = projects.ensure_project(root, template_name="template-root-workspace")
    assert updated_project.get("delivery_ceiling") is None
    cfg_after_ensure = json.loads(wf_path.read_text(encoding="utf-8"))
    assert cfg_after_ensure.get("delivery_ceiling") is None
