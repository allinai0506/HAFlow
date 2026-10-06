"""A proven frozen candidate remains valid when already on the base branch."""
import importlib
from pathlib import Path
import subprocess
from unittest.mock import Mock
import pytest
from herdr import scheduler_facts
from herdr.direct_dispatch import plan_stage_dispatch


@pytest.fixture
def scene(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_CONTROLLER_TEST", "1")
    controller = importlib.import_module("services.herdr-controller")
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              text=True, capture_output=True).stdout.strip()
    git("init", "-q", "-b", "main")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "fixture")
    (repo / "f").write_text("candidate")
    git("add", ".")
    git("commit", "-qm", "candidate", "--no-gpg-sign")
    sha = git("rev-parse", "HEAD")
    git("branch", "candidate")
    db = tmp_path / "state.db"
    monkeypatch.setenv('HERDR_STATE_DB', str(db))
    from herdr.state_store import SQLiteStateStore
    SQLiteStateStore(db).save_workflow({'workflow_id': 'wf-candidate', 'status': 'running',
        'execution_id': 'gen', 'project_root': str(repo)})
    read_facts = scheduler_facts.list_candidate_frozen_events
    monkeypatch.setattr(scheduler_facts, "list_candidate_frozen_events",
                        lambda workflow_id, db_path=None: read_facts(workflow_id, db_path=db))
    spec = {"task_id": "test-current", "task_type": "test", "integration_mode": "none",
            "goal": "verify", "prompt": "verify", "acceptance": [],
            "onto_branch": "candidate", "candidate_sha": sha}
    for name, value in {
        "project_for_workflow": {"project_root": str(repo), "base_branch": "main",
                                 "coordinator_pane_id": "coordinator", "requirement": "verify"},
        "load_tasks": [], "get_stage_policy": {}, "node_is_gate": False,
        "shared_docs_block": "", "mark_stage_advance_notified": True,
        "maybe_compact_coordinator": False, "maybe_dispatch_node_handoffs": [],
    }.items():
        monkeypatch.setattr(controller, name, Mock(return_value=value))
    monkeypatch.setattr(controller.direct_dispatch_planner, "plan_stage_dispatch",
                        Mock(return_value={"mode": "dispatch", "specs": [spec]}))
    real_run = subprocess.run
    launches = []
    def external_only(cmd, **kwargs):
        if cmd[0] == "git":
            return real_run(cmd, **kwargs)
        launches.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "Task dispatched: test-current", "")
    monkeypatch.setattr(controller.subprocess, "run", external_only)
    item = {"workflow_id": "wf-candidate", "node_id": "test",
            "node": {"id": "test", "depends_on": []}}
    return controller, repo, db, sha, spec, item, launches


def test_frozen_base_candidate_can_dispatch(scene):
    controller, repo, db, sha, spec, item, launches = scene
    scheduler_facts.record_candidate_frozen("wf-candidate", sha, delivery_branch="candidate", db_path=db)
    assert controller.try_direct_stage_advance(item)
    assert len(launches) == 1
    cmd = launches[0]
    assert cmd[cmd.index("--candidate-sha") + 1] == sha
    assert cmd[cmd.index("--onto") + 1] == "candidate"


def test_empty_unfrozen_branch_is_still_refused(scene):
    controller, repo, db, sha, spec, item, launches = scene
    assert not controller.try_direct_stage_advance(item)
    assert launches == []


@pytest.mark.parametrize("variant", ["short_freeze", "short_spec", "stale_spec", "other_workflow", "mixed_specs"])
def test_empty_candidate_requires_current_full_identity(scene, variant):
    controller, repo, db, sha, spec, item, launches = scene
    frozen = sha[:12] if variant == "short_freeze" else sha
    workflow = "other-workflow" if variant == "other_workflow" else "wf-candidate"
    if variant in {'short_freeze', 'other_workflow'}:
        with pytest.raises(ValueError):
            scheduler_facts.record_candidate_frozen(workflow, frozen, db_path=db)
    else:
        scheduler_facts.record_candidate_frozen(workflow, frozen, db_path=db)
    if variant == "short_spec":
        spec["candidate_sha"] = sha[:12]
    elif variant == "stale_spec":
        spec["candidate_sha"] = "a" * 40
    elif variant == "mixed_specs":
        other = dict(spec, task_id="test-stale", candidate_sha="a" * 40)
        controller.direct_dispatch_planner.plan_stage_dispatch.return_value["specs"].append(other)
    assert not controller.try_direct_stage_advance(item)
    assert launches == []


def test_latest_freeze_rotation_wins(scene):
    controller, repo, db, sha, spec, item, launches = scene
    scheduler_facts.record_candidate_frozen("wf-candidate", sha, db_path=db)
    scheduler_facts.record_candidate_frozen("wf-candidate", "a" * 40, db_path=db)
    assert not controller.try_direct_stage_advance(item)
    assert launches == []
    scheduler_facts.record_candidate_frozen("wf-candidate", sha, db_path=db)
    assert controller.try_direct_stage_advance(item)
    assert len(launches) == 1


def test_onto_commit_must_match_frozen_pin_even_if_empty_relative_to_base(scene):
    controller, repo, db, sha, spec, item, launches = scene
    (repo / "f").write_text("next")
    subprocess.run(["git", "-C", str(repo), "commit", "-am", "next", "--no-gpg-sign"], check=True)
    newer = subprocess.run(["git", "-C", str(repo), "rev-parse", "main"], check=True,
                           text=True, capture_output=True).stdout.strip()
    scheduler_facts.record_candidate_frozen("wf-candidate", newer, db_path=db)
    spec["candidate_sha"] = newer
    # candidate still references the ancestor; main..candidate has zero commits.
    assert not controller.try_direct_stage_advance(item)
    assert launches == []


def test_nonempty_unfrozen_candidate_preserves_legacy_dispatch(scene):
    controller, repo, db, sha, spec, item, launches = scene
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "candidate"], check=True)
    (repo / "f").write_text("new feature")
    subprocess.run(["git", "-C", str(repo), "commit", "-am", "feature", "--no-gpg-sign"], check=True)
    assert controller.try_direct_stage_advance(item)
    assert len(launches) == 1


def test_unknown_git_reference_preserves_legacy_policy(scene):
    controller, repo, db, sha, spec, item, launches = scene
    spec["onto_branch"] = "missing-ref"
    assert controller.try_direct_stage_advance(item)
    assert len(launches) == 1


@pytest.mark.parametrize("variant", ["missing_onto", "missing_pin", "both_missing"])
def test_frozen_exception_requires_every_spec_to_have_identity(scene, variant):
    controller, repo, db, sha, spec, item, launches = scene
    scheduler_facts.record_candidate_frozen("wf-candidate", sha, db_path=db)
    other = dict(spec, task_id="test-unproven")
    if variant in {"missing_onto", "both_missing"}:
        other["onto_branch"] = ""
    if variant in {"missing_pin", "both_missing"}:
        other["candidate_sha"] = ""
    controller.direct_dispatch_planner.plan_stage_dispatch.return_value["specs"].append(other)
    assert not controller.try_direct_stage_advance(item)
    assert launches == []


def test_proven_multi_spec_multi_onto_batch_can_dispatch(scene):
    controller, repo, db, sha, spec, item, launches = scene
    scheduler_facts.record_candidate_frozen("wf-candidate", sha, db_path=db)
    other = dict(spec, task_id="review-current", onto_branch="main")
    controller.direct_dispatch_planner.plan_stage_dispatch.return_value["specs"].append(other)
    assert controller.try_direct_stage_advance(item)
    assert len(launches) == 2
    assert all(cmd[cmd.index("--candidate-sha") + 1] == sha for cmd in launches)


@pytest.mark.parametrize("node_id", ["test", "review"])
def test_integrated_task_ref_off_base_dispatches_frozen_sha_without_borrowing_branch(scene, monkeypatch, node_id):
    controller, repo, db, base_sha, spec, item, launches = scene
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("checkout", "-q", "candidate")
    (repo / "f").write_text("implementation candidate")
    git("commit", "-qam", "implementation", "--no-gpg-sign")
    frozen = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    assert git("rev-parse", "HEAD") == base_sha != frozen
    scheduler_facts.record_candidate_frozen("wf-candidate", frozen,
        source_node="implementation", delivery_branch="candidate", db_path=db)
    controller.load_tasks.return_value = [{"task_id": "impl", "workflow_id": "wf-candidate",
        "node": "implementation", "status": "integrated", "integration_mode": "git",
        "branch": "candidate", "integrated_commit": frozen}]
    monkeypatch.setattr(controller.direct_dispatch_planner, "plan_stage_dispatch", plan_stage_dispatch)
    item.update(node_id=node_id, node={"id": node_id, "depends_on": ["implementation"],
        "purpose": "Verify the frozen implementation candidate",
        "default_task_type": "test", "default_integration_mode": "none"})
    assert controller.try_direct_stage_advance(item)
    assert len(launches) == 1
    cmd = launches[0]
    assert "--onto" not in cmd
    assert "--candidate-sha" in cmd
    assert cmd[cmd.index("--candidate-sha") + 1] == frozen


@pytest.mark.parametrize("frozen", ["a" * 40, "abc123"])
def test_frozen_off_base_candidate_must_be_a_real_full_commit(scene, frozen):
    controller, repo, db, sha, spec, item, launches = scene
    if len(frozen) != 40:
        with pytest.raises(ValueError):
            scheduler_facts.record_candidate_frozen('wf-candidate', frozen, db_path=db)
    else:
        scheduler_facts.record_candidate_frozen('wf-candidate', frozen, db_path=db)
    assert controller._scheduler_expected_candidate_sha("wf-candidate", str(repo), [], None) == ""
