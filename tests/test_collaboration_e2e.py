"""Collaboration e2e tests (lifecycle, ACK-once, metrics, isolation)."""

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr import collaboration as collab
from herdr import state_db


def _load_controller(name="ctrl_collab_e2e_test"):
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


def _tasks(run_id="run-1", workflow_id="wf-1", db=None, id_prefix=""):
    # Production-faithful: sibling tasks carry distinct per-launch run_ids;
    # the shared collaboration scope is the workflow.
    task_a = f"{id_prefix}task-a"
    task_b = f"{id_prefix}task-b"
    tasks = {
        task_a: {"task_id": task_a, "run_id": "run-A", "workflow_id": workflow_id,
                  "pane_id": f"pane-{id_prefix}a", "agent": "developer"},
        task_b: {"task_id": task_b, "run_id": "run-B", "workflow_id": workflow_id,
                  "pane_id": f"pane-{id_prefix}b", "agent": "reviewer"},
    }
    if db is not None:
        _persist_tasks(db, tasks)
    return tasks


def _persist_tasks(db, tasks):
    for workflow_id in sorted({task["workflow_id"] for task in tasks.values()}):
        if state_db.get_workflow(workflow_id, db_path=db) is None:
            state_db.save_workflow(
                {"workflow_id": workflow_id, "title": "fixture", "status": "running",
                 "config": {"nodes": [{"id": "implementation", "depends_on": []},
                                      {"id": "review", "depends_on": ["implementation"]}] }},
                db_path=db,
            )
    for task in tasks.values():
        state_db.save_task(task, db_path=db)
    return tasks


def test_e2e_lifecycle_dispatch_ack_complete_with_metrics(tmp_path):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    ev = state_db.create_collaboration_event({
        "run_id": "wf-1", "workflow_id": "wf-1",
        "from_task_id": "task-a", "from_agent": "developer",
        "to_task_id": "task-b", "to_agent": "reviewer",
        "type": "HANDOFF", "summary": "Implementation done.",
        "artifact_refs": ["commit:abc123"], "evidence_refs": ["test:x"],
        "source_fact_id": "fact-1",
    }, db_path=db)
    sender = FakeSender()
    ctrl.dispatch_collaboration_event(ev["event_id"], _tasks(db=db), sender, db_path=db)
    acked = ctrl.ack_collaboration_event_for_task("task-b", db_path=db)
    assert len(acked) == 1 and acked[0]["status"] == "acknowledged"
    done = state_db.mark_collaboration_completed(ev["event_id"], db_path=db)
    assert done["status"] == "completed"
    lat = collab.handoff_latency(done)
    assert lat["dispatch_latency"] is not None and lat["dispatch_latency"] >= 0
    assert lat["ack_latency"] is not None and lat["ack_latency"] >= 0
    assert lat["handoff_latency"] is not None and lat["handoff_latency"] >= 0


def test_e2e_ack_exactly_once(tmp_path):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    ev = state_db.create_collaboration_event({
        "run_id": "wf-1", "workflow_id": "wf-1",
        "from_task_id": "task-a", "from_agent": "developer",
        "to_task_id": "task-b", "to_agent": "reviewer",
        "type": "HANDOFF", "summary": "Done.",
        "source_fact_id": "fact-1",
    }, db_path=db)
    sender = FakeSender()
    ctrl.dispatch_collaboration_event(ev["event_id"], _tasks(db=db), sender, db_path=db)
    first = ctrl.ack_collaboration_event_for_task("task-b", db_path=db)
    second = ctrl.ack_collaboration_event_for_task("task-b", db_path=db)
    assert len(first) == 1
    assert second == []


def test_e2e_prompt_never_carries_full_history(tmp_path):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    big = "implementation detail line " * 200
    ev = state_db.create_collaboration_event({
        "run_id": "wf-1", "workflow_id": "wf-1",
        "from_task_id": "task-a", "from_agent": "developer",
        "to_task_id": "task-b", "to_agent": "reviewer",
        "type": "HANDOFF", "summary": big,
        "artifact_refs": ["commit:abc123"], "evidence_refs": ["verification:ver-456"],
        "source_fact_id": "fact-1",
    }, db_path=db)
    sender = FakeSender()
    ctrl.dispatch_collaboration_event(ev["event_id"], _tasks(db=db), sender, db_path=db)
    prompt = sender.calls[0][1]
    assert len(prompt) <= 2000
    assert "terminal transcript" not in prompt.lower()
    stored = state_db.get_collaboration_event(ev["event_id"], db_path=db)
    assert stored["artifact_refs"] == ["commit:abc123"]
    assert len(stored["summary"]) <= 500


def test_e2e_cross_run_parallel_isolation(tmp_path):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    ea = state_db.create_collaboration_event({
        "run_id": "wf-A", "workflow_id": "wf-A",
        "from_task_id": "a-task-a", "from_agent": "developer",
        "to_task_id": "a-task-b", "to_agent": "reviewer",
        "type": "HANDOFF", "summary": "A done.",
        "source_fact_id": "fact-1",
    }, db_path=db)
    eb = state_db.create_collaboration_event({
        "run_id": "wf-B", "workflow_id": "wf-B",
        "from_task_id": "b-task-a", "from_agent": "developer",
        "to_task_id": "b-task-b", "to_agent": "reviewer",
        "type": "HANDOFF", "summary": "B done.",
        "source_fact_id": "fact-1",
    }, db_path=db)
    tasks_a = _tasks(workflow_id="wf-A", db=db, id_prefix="a-")
    tasks_b = _tasks(workflow_id="wf-B", db=db, id_prefix="b-")
    # Same task names, different workflow executions: each resolves within its own scope.
    sender = FakeSender()
    ctrl.dispatch_collaboration_event(ea["event_id"], tasks_a, sender, db_path=db)
    ctrl.dispatch_collaboration_event(eb["event_id"], tasks_b, sender, db_path=db)
    assert len(sender.calls) == 2
    # Crossed lookup must fail, never cross-deliver.
    ec = state_db.create_collaboration_event({
        "run_id": "wf-A", "workflow_id": "wf-A",
        "from_task_id": "a-task-a", "from_agent": "developer",
        "to_task_id": "a-task-b", "to_agent": "reviewer",
        "type": "HANDOFF", "summary": "A2.",
        "source_fact_id": "fact-2",
    }, db_path=db)
    out = ctrl.dispatch_collaboration_event(ec["event_id"], tasks_b, sender, db_path=db)
    assert out["status"] == "dispatched"
    assert len(sender.calls) == 3


def _handoff(db, to_task="task-b", source_fact="fact-1", scope="wf-1"):
    return state_db.create_collaboration_event({
        "run_id": scope, "workflow_id": scope,
        "from_task_id": "task-a", "from_agent": "developer",
        "to_task_id": to_task, "to_agent": "reviewer",
        "type": "HANDOFF", "summary": "Done.",
        "source_fact_id": source_fact,
    }, db_path=db)


def test_e2e_complete_on_authoritative_task_done(tmp_path):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    ev = _handoff(db)
    sender = FakeSender()
    ctrl.dispatch_collaboration_event(ev["event_id"], _tasks(db=db), sender, db_path=db)
    ctrl.ack_collaboration_event_for_task("task-b", db_path=db)
    done = ctrl.maybe_complete_on_task_done("task-b", db_path=db)
    assert len(done) == 1 and done[0]["status"] == "completed"
    assert done[0]["completed_at"] is not None
    assert done[0]["handoff_completed_at"] is not None


def test_e2e_complete_leaves_unacked_and_unrelated_untouched(tmp_path):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    ev1 = _handoff(db, source_fact="fact-1")
    ev2 = _handoff(db, to_task="task-c", source_fact="fact-2")
    sender = FakeSender()
    tasks = _tasks(db=db)
    tasks["task-c"] = {"task_id": "task-c", "run_id": "run-C",
                       "workflow_id": "wf-1", "pane_id": "pane-c", "agent": "reviewer"}
    _persist_tasks(db, tasks)
    ctrl.dispatch_collaboration_event(ev1["event_id"], tasks, sender, db_path=db)
    ctrl.dispatch_collaboration_event(ev2["event_id"], tasks, sender, db_path=db)
    done = ctrl.maybe_complete_on_task_done("task-b", db_path=db)
    assert done == []
    assert state_db.get_collaboration_event(
        ev1["event_id"], db_path=db)["status"] == "dispatched"
