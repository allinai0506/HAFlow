"""Collaboration dispatch tests (fake sender, tmp DB, no real Herdr calls).

Covers task Tests A/B/C/D/E/F/H/J at the dispatch layer.
"""

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr import state_db


def _load_controller(name="ctrl_collab_dispatch_test"):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(
            name, str(HERDR_ROOT / "services" / "herdr-controller.py")
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tasks(run_id="run-1"):
    return {
        "task-a": {"task_id": "task-a", "run_id": run_id, "workflow_id": "wf-1",
                   "pane_id": "pane-a", "agent": "developer"},
        "task-b": {"task_id": "task-b", "run_id": run_id, "workflow_id": "wf-1",
                   "pane_id": "pane-b", "agent": "reviewer"},
        "coord": {"task_id": "coord", "run_id": run_id, "workflow_id": "wf-1",
                  "pane_id": "pane-coord", "agent": "coordinator"},
    }


def _make_event(db, **overrides):
    base = {
        "run_id": "run-1", "workflow_id": "wf-1",
        "from_task_id": "task-a", "from_agent": "developer",
        "to_task_id": "task-b", "to_agent": "reviewer",
        "type": "HANDOFF", "summary": "Rate-limit done. Review concurrency.",
        "artifact_refs": ["commit:abc123"],
        "evidence_refs": ["test:rate-limit-concurrency"],
        "source_fact_id": "fact-1",
    }
    base.update(overrides)
    return state_db.create_collaboration_event(base, db_path=db)


class FakeSender:
    def __init__(self, fail_panes=()):
        self.calls = []
        self.fail_panes = set(fail_panes)

    def __call__(self, pane_id, prompt):
        self.calls.append((pane_id, prompt))
        if pane_id in self.fail_panes:
            raise RuntimeError("herdr prompt failed")
        return {"ok": True}


def test_a_real_dispatch_hits_target_pane_with_refs(tmp_path):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    ev = _make_event(db)
    sender = FakeSender()
    out = ctrl.dispatch_collaboration_event(ev["event_id"], _tasks(), sender, db_path=db)
    assert out["dispatched"] is True
    assert len(sender.calls) == 1
    pane, prompt = sender.calls[0]
    assert pane == "pane-b"
    assert ev["event_id"] in prompt
    assert "commit:abc123" in prompt
    assert "test:rate-limit-concurrency" in prompt


def test_b_deterministic_path_never_touches_coordinator(tmp_path):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    ev = _make_event(db)
    sender = FakeSender()
    ctrl.dispatch_collaboration_event(ev["event_id"], _tasks(), sender, db_path=db)
    panes = [c[0] for c in sender.calls]
    assert panes == ["pane-b"]
    assert "pane-coord" not in panes


def test_c_duplicate_event_single_prompt(tmp_path):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    first = _make_event(db)
    second = _make_event(db)
    assert second["event_id"] == first["event_id"]
    sender = FakeSender()
    tasks = _tasks()
    ctrl.dispatch_collaboration_event(first["event_id"], tasks, sender, db_path=db)
    out = ctrl.dispatch_collaboration_event(second["event_id"], tasks, sender, db_path=db)
    assert out.get("recovered") is True
    assert len(sender.calls) == 1


def test_d_crash_recovery_no_second_prompt(tmp_path):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    ev = _make_event(db)
    sender = FakeSender()
    # Crash window: intent persisted + prompt reached Herdr, mark lost.
    state_db.record_event(
        {"event_type": "collaboration_dispatch_intent", "task_id": "task-b",
         "workflow_id": "wf-1", "run_id": "run-1",
         "payload": {"collaboration_event_id": ev["event_id"], "pane_id": "pane-b"},
         "source": "collaboration"},
        db_path=db,
    )
    sender("pane-b", f"manual prompt {ev['event_id']}")
    assert len(sender.calls) == 1
    out = ctrl.dispatch_collaboration_event(ev["event_id"], _tasks(), sender, db_path=db)
    assert out["dispatched"] is True and out.get("recovered") is True
    assert len(sender.calls) == 1
    fetched = state_db.get_collaboration_event(ev["event_id"], db_path=db)
    assert fetched["status"] == "dispatched"


def test_e_target_pane_missing_fails_without_fallback(tmp_path):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    ev = _make_event(db)
    tasks = _tasks()
    tasks["task-b"] = dict(tasks["task-b"], pane_id=None, runtime={})
    sender = FakeSender()
    out = ctrl.dispatch_collaboration_event(ev["event_id"], tasks, sender, db_path=db)
    assert out["status"] == "failed"
    assert sender.calls == []
    fetched = state_db.get_collaboration_event(ev["event_id"], db_path=db)
    assert fetched["status"] == "failed"


def test_f_cross_run_rejected(tmp_path):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    ev = _make_event(db)  # run-1
    tasks = _tasks(run_id="run-B")
    sender = FakeSender()
    out = ctrl.dispatch_collaboration_event(ev["event_id"], tasks, sender, db_path=db)
    assert out["status"] == "failed"
    assert sender.calls == []


def test_h_blocker_to_coordinator_allowed(tmp_path):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    ev = _make_event(db, type="BLOCKER", to_task_id="coord",
                     to_agent="coordinator", source_fact_id="fact-blocker")
    sender = FakeSender()
    out = ctrl.dispatch_collaboration_event(ev["event_id"], _tasks(), sender, db_path=db)
    assert out["dispatched"] is True
    assert sender.calls[0][0] == "pane-coord"


def test_j_parallel_handoffs_no_cross(tmp_path):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    ev1 = _make_event(db, from_task_id="task-a", to_task_id="task-b",
                      source_fact_id="fact-a")
    ev2 = _make_event(db, from_task_id="task-c", to_task_id="task-d",
                      to_agent="reviewer-2", source_fact_id="fact-c")
    tasks = _tasks()
    tasks["task-c"] = {"task_id": "task-c", "run_id": "run-1",
                       "workflow_id": "wf-1", "pane_id": "pane-c", "agent": "developer2"}
    tasks["task-d"] = {"task_id": "task-d", "run_id": "run-1",
                       "workflow_id": "wf-1", "pane_id": "pane-d", "agent": "reviewer2"}
    sender = FakeSender()
    ctrl.dispatch_collaboration_event(ev1["event_id"], tasks, sender, db_path=db)
    ctrl.dispatch_collaboration_event(ev2["event_id"], tasks, sender, db_path=db)
    assert len(sender.calls) == 2
    by_pane = {pane: prompt for pane, prompt in sender.calls}
    assert ev1["event_id"] in by_pane["pane-b"]
    assert ev2["event_id"] in by_pane["pane-d"]
    assert ev1["event_id"] not in by_pane["pane-d"]


class ExplodingSender(FakeSender):
    def __call__(self, pane_id, prompt):
        self.calls.append((pane_id, prompt))
        raise RuntimeError("herdr agent prompt exploded")


def test_sender_failure_marks_failed_no_ghost_dispatch(tmp_path):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    ev = _make_event(db)
    sender = ExplodingSender()
    out = ctrl.dispatch_collaboration_event(ev["event_id"], _tasks(), sender, db_path=db)
    assert out["status"] == "failed"
    assert len(sender.calls) == 1
    again = ctrl.dispatch_collaboration_event(ev["event_id"], _tasks(), sender, db_path=db)
    assert again["status"] == "failed"
    assert len(sender.calls) == 1


def test_missing_run_identity_fails_closed(tmp_path):
    ctrl = _load_controller()
    db = tmp_path / "state.db"
    ev = _make_event(db)
    tasks = _tasks()
    tasks["task-b"] = {"task_id": "task-b", "workflow_id": "wf-1",
                       "pane_id": "pane-b", "agent": "reviewer"}
    sender = FakeSender()
    out = ctrl.dispatch_collaboration_event(ev["event_id"], tasks, sender, db_path=db)
    assert out["status"] == "failed"
    assert sender.calls == []
