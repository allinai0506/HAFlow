"""Collaboration production-wiring tests (controller hooks, tmp DB).

Locks the accelerator behavior: deterministic node advance emits a HANDOFF,
target working ACKs it, unknown routes keep the Coordinator path, and the
wiring never breaks the main flow.
"""

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr import collaboration as collab
from herdr import state_db


def _load_controller(name="ctrl_collab_wiring_test"):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(
            name, str(HERDR_ROOT / "services" / "herdr-controller.py")
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeSender:
    def __init__(self):
        self.calls = []

    def __call__(self, pane_id, prompt):
        self.calls.append((pane_id, prompt))
        return {"ok": True}


def test_scope_is_workflow_execution_identity_not_task_run():
    # Collaboration scope is the shared workflow execution identity, NOT the
    # per-task run_id (each herdr-task launch mints its own run_id).
    assert collab.collab_scope_for_task({"run_id": "r", "workflow_id": "w"}) == "w"
    assert collab.collab_scope_for_task({"workflow_id": "w"}) == "w"
    assert collab.collab_scope_for_task({"workflow_run_id": "wr", "workflow_id": "w"}) == "wr"
    assert collab.collab_scope_for_task(
        {"execution_id": "e", "workflow_id": "w"}) == "e"
    assert collab.collab_scope_for_task({}) is None


def test_infer_trigger_only_known_edges():
    assert collab.infer_handoff_trigger("implementation", "test") == "implementation_completed"
    assert collab.infer_handoff_trigger("review", "wrapup") == "review_completed"
    assert collab.infer_handoff_trigger("plan", "implementation") is None
    assert collab.infer_handoff_trigger("", "review") is None


def _wf_tasks(db=None):
    tasks = {
        "wf-1-impl": {"task_id": "wf-1-impl", "workflow_id": "wf-1",
                      "node": "implementation", "stage": "implementation",
                      "status": "completed", "pane_id": "pane-impl",
                      "agent": "developer", "goal": "Build rate limiter.",
                      "updated_at": 200.0, "run_id": "run-impl"},
        "wf-1-test-auto": {"task_id": "wf-1-test-auto", "workflow_id": "wf-1",
                           "node": "test", "stage": "test",
                           "status": "dispatched", "pane_id": "pane-test",
                           "agent": "tester", "run_id": "run-test"},
    }
    if db is not None:
        if state_db.get_workflow("wf-1", db_path=db) is None:
            state_db.save_workflow(
                {"workflow_id": "wf-1", "title": "fixture", "status": "running",
                 "config": {"nodes": [{"id": "implementation", "depends_on": []},
                                      {"id": "test", "depends_on": ["implementation"]}] }},
                db_path=db,
            )
        for task in tasks.values():
            state_db.save_task(task, db_path=db)
    return tasks


def test_wiring_dispatches_handoff_on_deterministic_advance(tmp_path):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    tasks = _wf_tasks(db=db)
    for task in tasks.values():
        task["run_id"] = "legacy-shared"
        state_db.save_task(task, db_path=db)
    sender = FakeSender()
    out = ctrl.maybe_dispatch_node_handoffs(
        workflow_id="wf-1", ready_id="test",
        dep_ids=["implementation"], launched=["wf-1-test-auto"],
        tasks_by_id=tasks, prompt_sender=sender, db_path=db,
    )
    assert len(out) == 1 and out[0].get("dispatched") is True
    assert sender.calls[0][0] == "pane-test"
    rows = state_db.list_collaboration_events(run_id="wf-1", db_path=db)
    assert len(rows) == 1
    assert rows[0]["from_task_id"] == "wf-1-impl"


def test_wiring_rechecks_authoritative_upstream_status(tmp_path):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    state_db.save_workflow(
        {
            "workflow_id": "wf-1", "title": "fixture", "status": "running",
            "config": {"nodes": [
                {"id": "implementation", "depends_on": []},
                {"id": "test", "depends_on": ["implementation"]},
            ]},
        },
        db_path=db,
    )
    target = dict(_wf_tasks(db=db)["wf-1-test-auto"], run_id="run-target", workflow_run_id="scope-a", agent_role="tester")
    upstream = dict(_wf_tasks(db=db)["wf-1-impl"], run_id="run-upstream", workflow_run_id="scope-a", status="completed", agent_role="developer")
    state_db.save_task(target, db_path=db)
    state_db.save_task(upstream, db_path=db)
    stale_upstream = dict(upstream)
    state_db.save_task(dict(upstream, status="working"), db_path=db)
    sender = FakeSender()
    out = ctrl.maybe_dispatch_node_handoffs(
        workflow_id="wf-1", ready_id="test", dep_ids=["implementation"],
        launched=[target["task_id"]],
        tasks_by_id={target["task_id"]: target, upstream["task_id"]: stale_upstream},
        prompt_sender=sender, db_path=db,
    )
    assert out[0].get("skipped") is True
    assert sender.calls == []
    assert state_db.list_collaboration_events(run_id="scope-a", db_path=db) == []


def test_wiring_handoff_carries_target_working_context_ref(tmp_path):
    from herdr.context_compiler import get_working_context

    ctrl = _load_controller()
    db = tmp_path / "state.db"
    state_db.save_workflow(
        {
            "workflow_id": "wf-1",
            "title": "fixture",
            "status": "running",
            "config": {"nodes": [
                {"id": "implementation", "depends_on": []},
                {"id": "test", "depends_on": ["implementation"]},
            ]},
        },
        db_path=db,
    )
    tasks = _wf_tasks(db=db)
    for task_id, task in tasks.items():
        task = dict(task)
        task["run_id"] = f"run-{task_id}"
        task["workflow_run_id"] = "wf-exec-1"
        task["agent_role"] = "tester" if task_id.endswith("test-auto") else "developer"
        state_db.save_task(task, db_path=db)
        tasks[task_id] = task
    sender = FakeSender()
    out = ctrl.maybe_dispatch_node_handoffs(
        workflow_id="wf-1", ready_id="test",
        dep_ids=["implementation"], launched=["wf-1-test-auto"],
        tasks_by_id=tasks, prompt_sender=sender, db_path=db,
    )
    assert out[0].get("dispatched") is True
    row = state_db.list_collaboration_events(run_id="wf-exec-1", db_path=db)[0]
    assert len(row["context_refs"]) == 1
    loaded = get_working_context(row["context_refs"][0], db_path=db)
    assert loaded.task_id == "wf-1-test-auto"
    assert "WORKING_CONTEXT_REF:" in sender.calls[0][1]


def test_wiring_legacy_distinct_runs_do_not_cross_execute(tmp_path):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    tasks = _wf_tasks(db=db)
    tasks["wf-1-impl"]["run_id"] = "run-old"
    tasks["wf-1-test-auto"]["run_id"] = "run-new"
    out = ctrl.maybe_dispatch_node_handoffs(
        workflow_id="wf-1", ready_id="test", dep_ids=["implementation"],
        launched=["wf-1-test-auto"], tasks_by_id=tasks,
        prompt_sender=FakeSender(), db_path=db,
    )
    assert out[0].get("skipped") is True
    assert state_db.list_collaboration_events(run_id="wf-1", db_path=db) == []


def test_wiring_legacy_missing_run_ids_do_not_cross_execute(tmp_path):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    out = ctrl.maybe_dispatch_node_handoffs(
        workflow_id="wf-1", ready_id="test", dep_ids=["implementation"],
        launched=["wf-1-test-auto"], tasks_by_id=_wf_tasks(db=db),
        prompt_sender=FakeSender(), db_path=db,
    )
    assert out[0].get("skipped") is True
    assert state_db.list_collaboration_events(run_id="wf-1", db_path=db) == []


def test_wiring_legacy_handoff_planned_link_preloads_upstream_facts(tmp_path):
    from herdr.context_compiler import get_working_context

    ctrl = _load_controller()
    db = tmp_path / "state.db"
    state_db.save_workflow(
        {
            "workflow_id": "wf-legacy", "title": "fixture", "status": "running",
            "config": {"nodes": [
                {"id": "implementation", "depends_on": []},
                {"id": "test", "depends_on": ["implementation"]},
            ]},
        },
        db_path=db,
    )
    upstream = dict(
        _wf_tasks(db=db)["wf-1-impl"], task_id="legacy-impl", workflow_id="wf-legacy",
        run_id="run-legacy-shared", goal="Build upstream",
        artifacts=[{"ref": "legacy-artifact"}],
    )
    target = dict(
        _wf_tasks(db=db)["wf-1-test-auto"], task_id="legacy-test", workflow_id="wf-legacy",
        run_id="run-legacy-shared", agent_role="tester",
    )
    upstream.pop("workflow_run_id", None)
    target.pop("workflow_run_id", None)
    for task in (upstream, target):
        state_db.save_task(task, db_path=db)
    sender = FakeSender()
    out = ctrl.maybe_dispatch_node_handoffs(
        workflow_id="wf-legacy", ready_id="test", dep_ids=["implementation"],
        launched=[target["task_id"]], tasks_by_id={
            upstream["task_id"]: upstream, target["task_id"]: target,
        },
        prompt_sender=sender, db_path=db,
    )
    assert out[0].get("dispatched") is True
    row = state_db.list_collaboration_events(run_id="wf-legacy", db_path=db)[0]
    loaded = get_working_context(row["context_refs"][0], db_path=db)
    assert "legacy-artifact" in json.dumps(loaded.to_mapping(), ensure_ascii=False)
    assert loaded.handoffs


def test_wiring_selects_upstream_in_target_execution_scope(tmp_path):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    state_db.save_workflow(
        {
            "workflow_id": "wf-1", "title": "fixture", "status": "running",
            "config": {"nodes": [
                {"id": "implementation", "depends_on": []},
                {"id": "test", "depends_on": ["implementation"]},
            ]},
        },
        db_path=db,
    )
    target = dict(_wf_tasks(db=db)["wf-1-test-auto"], run_id="run-target", workflow_run_id="scope-a", agent_role="tester")
    upstream_a = dict(_wf_tasks(db=db)["wf-1-impl"], run_id="run-a", workflow_run_id="scope-a", updated_at=100.0, agent_role="developer")
    upstream_b = dict(upstream_a, task_id="wf-1-impl-b", run_id="run-b", workflow_run_id="scope-b", updated_at=200.0)
    for task in (target, upstream_a, upstream_b):
        state_db.save_task(task, db_path=db)
    sender = FakeSender()
    out = ctrl.maybe_dispatch_node_handoffs(
        workflow_id="wf-1", ready_id="test", dep_ids=["implementation"],
        launched=[target["task_id"]], tasks_by_id={
            target["task_id"]: target, upstream_a["task_id"]: upstream_a, upstream_b["task_id"]: upstream_b,
        },
        prompt_sender=sender, db_path=db,
    )
    assert out[0].get("dispatched") is True
    row = state_db.list_collaboration_events(run_id="scope-a", db_path=db)[0]
    assert row["from_task_id"] == upstream_a["task_id"]


def test_wiring_skips_unknown_route_keeps_coordinator(tmp_path):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    sender = FakeSender()
    tasks = _wf_tasks(db=db)
    tasks["wf-1-impl"]["node"] = "plan"
    tasks["wf-1-impl"]["stage"] = "plan"
    out = ctrl.maybe_dispatch_node_handoffs(
        workflow_id="wf-1", ready_id="implementation",
        dep_ids=["plan"], launched=["wf-1-test-auto"],
        tasks_by_id=tasks, prompt_sender=sender, db_path=db,
    )
    assert out[0].get("skipped") is True
    assert sender.calls == []
    assert state_db.list_collaboration_events(run_id="wf-1", db_path=db) == []


def test_wiring_never_breaks_stage_advance(tmp_path):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    out = ctrl.maybe_dispatch_node_handoffs(
        workflow_id="wf-1", ready_id="test",
        dep_ids=["implementation"], launched=["missing-task"],
        tasks_by_id={}, prompt_sender=FakeSender(), db_path=db,
    )
    assert out[0].get("status") in ("failed", "skipped")


def test_wiring_ack_on_working(tmp_path):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    ev = state_db.create_collaboration_event({
        "run_id": "wf-1", "workflow_id": "wf-1",
        "from_task_id": "wf-1-impl", "from_agent": "developer",
        "to_task_id": "wf-1-test-auto", "to_agent": "tester",
        "type": "HANDOFF", "summary": "Build done.",
        "source_fact_id": "wf-1:implementation:completed",
    }, db_path=db)
    sender = FakeSender()
    tasks = _wf_tasks(db=db)
    ctrl.dispatch_collaboration_event(ev["event_id"], tasks, sender, db_path=db)
    acked = ctrl.maybe_ack_on_working("wf-1-test-auto", db_path=db)
    assert len(acked) == 1 and acked[0]["status"] == "acknowledged"


def test_wiring_disabled_by_env_flag(tmp_path, monkeypatch):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    monkeypatch.setenv("HERDR_COLLABORATION_ENABLED", "0")
    sender = FakeSender()
    out = ctrl.maybe_dispatch_node_handoffs(
        workflow_id="wf-1", ready_id="test",
        dep_ids=["implementation"], launched=["wf-1-test-auto"],
        tasks_by_id=_wf_tasks(db=db), prompt_sender=sender, db_path=db,
    )
    assert out[0].get("skipped") is True
    assert sender.calls == []
