"""Delivered-base selection must retain a proven frozen verification pin."""
import importlib.machinery
import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import Mock
import pytest
from herdr import direct_dispatch, scheduler_facts, workflow_docs
from herdr.state_store import SQLiteStateStore
from tests.test_frozen_base_candidate_dispatch import scene

REAL_PLAN = direct_dispatch.plan_stage_dispatch
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def actual_scene(scene, monkeypatch):
    controller, repo, db, sha, _, item, calls = scene
    monkeypatch.setattr(controller.direct_dispatch_planner, "plan_stage_dispatch", REAL_PLAN)
    impl = {"task_id": "impl", "workflow_id": "wf-candidate", "node": "implementation", "stage": "implementation", "status": "cleaned", "integration_mode": "git", "branch": "candidate"}
    monkeypatch.setattr(controller, "load_tasks", Mock(return_value=[impl]))
    monkeypatch.setattr(workflow_docs, "load_notes", lambda wid: [])
    item["node"].update(purpose="Verify the frozen candidate", default_task_type="test", default_integration_mode="none", depends_on=["implementation"])
    spec = importlib.util.spec_from_loader("cli_delivered_base_pin", importlib.machinery.SourceFileLoader("cli_delivered_base_pin", str(ROOT / "bin/herdr-task")))
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, "_get_store", lambda: SQLiteStateStore(db))
    return controller, repo, db, sha, item, calls, cli


@pytest.mark.parametrize("node", ["test", "review"])
def test_actual_dispatch_reaches_cli_frozen_identity(actual_scene, monkeypatch, node):
    controller, repo, db, sha, item, calls, cli = actual_scene
    item["node_id"] = node
    item["node"]["id"] = node
    scheduler_facts.record_candidate_frozen("wf-candidate", sha, db_path=db)
    assert controller.try_direct_stage_advance(item)
    assert len(calls) == 1
    command = calls[0]
    assert "--onto" not in command  # #125: do not fetch an unpublished task branch.
    claim = command[command.index("--candidate-sha") + 1] if "--candidate-sha" in command else ""
    identity = cli._preflight_delivery_identity("wf-candidate", node, command[command.index("--task-id") + 1], claim)
    assert identity == {"candidate_sha": sha, "identity_source": "frozen"}


@pytest.mark.parametrize("variant", ["absent", "short", "wrong", "other_workflow", "latest_wrong", "head_moved"])
def test_current_workflow_commit_proof_is_required_for_frozen_pin(actual_scene, variant):
    controller, repo, db, sha, item, calls, cli = actual_scene
    if variant != "absent":
        frozen = sha[:12] if variant == "short" else "a" * 40 if variant == "wrong" else sha
        if variant in {'short', 'other_workflow'}:
            with pytest.raises(ValueError):
                scheduler_facts.record_candidate_frozen('other' if variant == 'other_workflow' else 'wf-candidate', frozen, db_path=db)
        else:
            scheduler_facts.record_candidate_frozen('wf-candidate', frozen, db_path=db)
    if variant == "latest_wrong":
        scheduler_facts.record_candidate_frozen("wf-candidate", "a" * 40, db_path=db)
    if variant == "head_moved":
        (repo / "f").write_text("new HEAD")
        subprocess.run(["git", "-C", str(repo), "commit", "-am", "moved"], check=True, capture_output=True)
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()
        assert head != sha
        assert controller._scheduler_expected_candidate_sha("wf-candidate", str(repo), ["implementation"], None) == sha
        assert controller.try_direct_stage_advance(item)
        assert len(calls) == 1 and "--onto" not in calls[0]
        claim = calls[0][calls[0].index("--candidate-sha") + 1]
        assert claim == sha
        assert cli._preflight_delivery_identity("wf-candidate", "test", "test-current", claim) == {
            "candidate_sha": sha, "identity_source": "frozen"}
    else:
        assert controller._scheduler_expected_candidate_sha("wf-candidate", str(repo), ["implementation"], None) == ""
    with pytest.raises(SystemExit) as error:
        cli._preflight_delivery_identity("wf-candidate", "test", "test-current", "")
    assert error.value.code == 2
