"""Snapshot-only Controller projection: attribution, safety and bounded output."""

from copy import deepcopy

import pytest

from herdr.controller_actions import generate_controller_actions, generate_progress_actions


def _inputs():
    return {
        "workflow": {"workflow_id": "wf-1", "status": "running", "execution_id": "exec-1"},
        "config": {"nodes": [{"id": "implementation"},
                              {"id": "test", "depends_on": ["implementation"]},
                              {"id": "review", "depends_on": ["implementation"]}]},
        "tasks": [{"task_id": "T1", "workflow_id": "wf-1", "node": "implementation",
                   "status": "working", "version": 7, "run_id": "run-9",
                   "execution_id": "exec-1", "pane_id": "w1:p2"}],
        "graph": {"nodes": [
            {"id": "implementation", "status": "working", "depends_on": []},
            {"id": "test", "status": "waiting", "depends_on": ["implementation"]},
            {"id": "review", "status": "waiting", "depends_on": ["implementation"]},
        ], "current_nodes": ["implementation"], "ready_nodes": []},
        "actions": [], "recovery": [], "controller": {"status": "running", "pid": 42},
        "events": [],
    }


def _project(**changes):
    from herdr.controller_cockpit import build_cockpit_projection
    values = _inputs()
    values.update(changes)
    return build_cockpit_projection(**values)


def test_judgment_reports_active_work_and_next_dependencies():
    projection = _project()
    assert projection["schema"] == "controller-cockpit/v1"
    assert projection["judgment"] == {"now": "implementation_running",
                                      "blocked_reason": "dependencies_incomplete",
                                      "next": "start_test_review", "requires_user": False}
    assert projection["read_state"] == "confirmed"
    assert projection["nodes"][0]["tasks"][0]["task_id"] == "T1"


def test_ready_nodes_are_reported_without_claiming_they_started():
    values = _inputs()
    values["graph"]["nodes"][0]["status"] = "completed"
    values["graph"]["current_nodes"] = []
    values["graph"]["ready_nodes"] = ["test", "review"]
    projection = _project(**values)
    assert projection["judgment"]["now"] == "ready"
    assert projection["judgment"]["next"] == "start_test_review"
    assert projection["ready_nodes"] == ["test", "review"]


@pytest.mark.parametrize("missing", ["controller", "tasks", "graph", "actions", "recovery", "events", "config"])
def test_absent_snapshot_is_unknown_and_blocks_automatic_actions(missing):
    values = _inputs()
    values["actions"] = generate_progress_actions(values["tasks"][0], values["workflow"])
    values[missing] = None
    projection = _project(**values)
    assert projection["read_state"] == "unknown"
    assert all(a["automation"]["mode"] == "blocked" for a in projection["actions"])
    if missing == "controller":
        assert projection["controller"]["status"] == "unknown"


def test_recovery_suppresses_conflicting_actions_and_retains_safe_controls():
    values = _inputs()
    task = {**values["tasks"][0], "status": "blocked"}
    values["actions"] = (generate_controller_actions(task, values["workflow"])
                         + generate_progress_actions(task, values["workflow"]))
    values["recovery"] = [{"operation_id": "op-1", "workflow_id": "wf-1",
                           "status": "waiting_human", "payload": {"task_ids": ["T1"]}}]
    projection = _project(**values)
    assert {a["api_payload"]["type"] for a in projection["actions"]} == {"redrive", "steer", "halt"}
    assert projection["judgment"]["requires_user"] is True
    assert projection["judgment"]["blocked_reason"] == "recovery_unresolved"
    assert projection["todos"][0]["operation_id"] == "op-1"


def test_resolved_and_foreign_recovery_do_not_suppress_current_actions():
    values = _inputs()
    task = {**values["tasks"][0], "status": "blocked"}
    values["actions"] = generate_controller_actions(task, values["workflow"])
    values["recovery"] = [{"workflow_id": "wf-1", "status": "resolved"},
                           {"workflow_id": "other", "status": "waiting_human"}]
    assert "rework" in {a["api_payload"]["type"] for a in _project(**values)["actions"]}


@pytest.mark.parametrize("change", [
    {"status": "superseded"}, {"superseded_by": "T2"}, {"status": "cleaned"},
    {"execution_id": "old"}, {"workflow_id": "foreign"},
])
def test_historical_or_foreign_task_actions_are_excluded(change):
    values = _inputs()
    values["actions"] = generate_progress_actions(values["tasks"][0], values["workflow"])
    values["tasks"][0].update(change)
    assert _project(**values)["actions"] == []


@pytest.mark.parametrize("change", [{"version": 8}, {"run_id": "new-run"}, {"pane_id": "w9:p9"}])
def test_actions_bound_to_stale_task_snapshot_are_excluded(change):
    values = _inputs()
    values["actions"] = generate_progress_actions(values["tasks"][0], values["workflow"])
    values["tasks"][0].update(change)
    assert _project(**values)["actions"] == []


@pytest.mark.parametrize("pane", [None, "", "absent"])
def test_missing_current_pane_evidence_safety_blocks_old_live_actions(pane):
    values = _inputs()
    values["actions"] = generate_progress_actions(values["tasks"][0], values["workflow"])
    if pane == "absent":
        values["tasks"][0].pop("pane_id")
    else:
        values["tasks"][0]["pane_id"] = pane
    actions = _project(**values)["actions"]
    assert {a["action_id"] for a in actions} == {"T1:redrive", "T1:steer", "T1:halt"}
    assert all(a["automation"] == {"mode": "blocked", "source": "safety", "reason": "identity_unknown"}
               for a in actions)


@pytest.mark.parametrize("missing_from", ["config", "graph", "both"])
def test_current_task_node_missing_from_definition_or_graph_is_safety_blocked(missing_from):
    from herdr.workflow_graph import workflow_graph_projection
    values = _inputs()
    values["tasks"][0]["node"] = "removed"
    values["actions"] = generate_progress_actions(values["tasks"][0], values["workflow"])
    if missing_from in {"config", "both"}:
        values["config"] = {"nodes": [{"id": "other"}]}
    else:
        values["config"] = {"nodes": [{"id": "other"}, {"id": "removed"}]}
    graph_config = {"nodes": [{"id": "other"}]} if missing_from in {"graph", "both"} else {
        "nodes": [{"id": "other"}, {"id": "removed"}]}
    values["graph"] = workflow_graph_projection({**graph_config, **values["workflow"]}, values["tasks"])
    actions = _project(**values)["actions"]
    assert actions
    assert all(a["automation"] == {"mode": "blocked", "source": "safety", "reason": "node_unknown"}
               for a in actions)


def test_action_for_absent_task_is_preserved_but_safety_blocked():
    values = _inputs()
    values["actions"] = generate_progress_actions(values["tasks"][0], values["workflow"])
    projection = _project(**{**values, "tasks": []})
    assert projection["actions"][0]["action_id"] == "T1:redrive"
    assert projection["actions"][0]["automation"] == {
        "mode": "blocked", "source": "safety", "reason": "identity_unknown"}


def test_projection_resolves_policy_from_source_task_node_and_action_type():
    values = _inputs()
    values["config"]["nodes"][0]["confirm_before"] = ["redrive"]
    values["actions"] = generate_progress_actions(values["tasks"][0], values["workflow"])
    actions = {a["action_id"]: a for a in _project(**values)["actions"]}
    assert actions["T1:redrive"]["automation"] == {"mode": "confirmation_required", "source": "action"}
    assert actions["T1:steer"]["automation"] == {"mode": "automatic", "source": "default"}
    assert actions["T1:halt"]["automation"] == {
        "mode": "confirmation_required", "source": "safety", "reason": "destructive"}


def test_bypass_and_unknown_action_cannot_become_automatic():
    values = _inputs()
    task = {**values["tasks"][0], "status": "failed"}
    bypass = generate_controller_actions(task, values["workflow"])[-1].to_dict()
    unknown = {**bypass, "action_id": "T1:future", "category": "fix", "is_destructive": False,
               "api_payload": {"type": "future", "workflow_id": "wf-1", "task_id": "T1"}}
    values["actions"] = [bypass, unknown]
    actions = _project(**values)["actions"]
    assert actions[0]["automation"]["mode"] == "confirmation_required"
    assert actions[1]["automation"] == {"mode": "blocked", "source": "safety", "reason": "action_unknown"}


def test_events_are_workflow_scoped_bounded_and_ordered_newest_first():
    events = [{"id": n, "workflow_id": "wf-1", "timestamp": n} for n in range(150)]
    projection = _project(events=events + [{"id": 999, "workflow_id": "foreign", "timestamp": 999}])
    assert [e["id"] for e in projection["timeline"]] == list(range(149, 49, -1))
    assert projection["timeline_truncated"] is True


def test_task_and_action_caps_do_not_make_unseen_tasks_look_obsolete():
    values = _inputs()
    values["tasks"] = [{**values["tasks"][0], "task_id": f"T{n}"} for n in range(250)]
    values["actions"] = [generate_progress_actions(t, values["workflow"])[0] for t in values["tasks"]]
    values["actions"].reverse()
    projection = _project(**values)
    assert len(projection["nodes"][0]["tasks"]) == 200
    assert len(projection["actions"]) == 100
    assert projection["actions"][0]["action_id"] == "T249:redrive"
    assert projection["tasks_truncated"] is True
    assert projection["actions_truncated"] is True


def test_projection_is_pure_and_does_not_mutate_or_alias_inputs(monkeypatch):
    from herdr.controller_cockpit import build_cockpit_projection
    import builtins
    import socket
    import sqlite3
    import subprocess
    values = _inputs()
    values["actions"] = generate_progress_actions(values["tasks"][0], values["workflow"])
    original = deepcopy(values)

    def forbidden(*args, **kwargs):
        raise AssertionError("projection attempted I/O")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    projection = build_cockpit_projection(**values)
    assert values == original
    projection["nodes"][0]["tasks"][0]["status"] = "changed"
    projection["actions"][0]["api_payload"]["type"] = "changed"
    assert values == original


def test_read_error_cannot_be_mistaken_for_empty_healthy_cockpit():
    projection = _project(errors={"tasks": "snapshot unavailable"})
    assert projection["read_state"] == "unknown"
    assert projection["judgment"]["blocked_reason"] == "read_unknown"
    assert projection["errors"] == {"tasks": "snapshot unavailable"}


@pytest.mark.parametrize("target", [
    {"workflow_id": "wf-1"},
    {"workflow_id": "wf-1", "task_id": "T1", "node_id": "implementation"},
])
def test_partial_task_target_cannot_become_automatically_executable(target):
    values = _inputs()
    action = generate_progress_actions(values["tasks"][0], values["workflow"])[0].to_dict()
    action["target"] = target
    projection = _project(**{**values, "actions": [action]})
    assert projection["actions"][0]["automation"] == {
        "mode": "blocked", "source": "safety", "reason": "identity_unknown"}


def test_missing_expected_task_version_blocks_action_even_if_task_is_known():
    values = _inputs()
    action = generate_progress_actions(values["tasks"][0], values["workflow"])[0].to_dict()
    action["expected_identity"] = {}
    assert _project(**{**values, "actions": [action]})["actions"][0]["automation"]["mode"] == "blocked"


def test_conflicting_workflow_identity_is_suppressed():
    values = _inputs()
    action = generate_progress_actions(values["tasks"][0], values["workflow"])[0].to_dict()
    action["api_payload"]["workflow_id"] = "foreign"
    assert _project(**{**values, "actions": [action]})["actions"] == []


def test_legacy_action_without_contract_fields_remains_visible_but_blocked():
    values = _inputs()
    action = generate_progress_actions(values["tasks"][0], values["workflow"])[0].to_dict()
    for field in ("target", "expected_identity", "automation", "risk", "evidence_refs"):
        action.pop(field)
    projected = _project(**{**values, "actions": [action]})["actions"][0]
    assert projected["action_id"] == "T1:redrive"
    assert projected["automation"]["mode"] == "blocked"


@pytest.mark.parametrize("source", ["config", "graph"])
def test_empty_required_definition_is_unknown(source):
    assert _project(**{source: {}})["read_state"] == "unknown"


def test_event_order_uses_numeric_ids_for_equal_time_and_utc_for_iso_time():
    events = [
        {"id": 1, "workflow_id": "wf-1", "timestamp": 0},
        {"id": 2, "workflow_id": "wf-1", "timestamp": "1970-01-01T00:00:00Z"},
        {"id": 3, "workflow_id": "wf-1", "timestamp": "1970-01-01T00:00:01+00:00"},
    ]
    assert [e["id"] for e in _project(events=events)["timeline"]] == [3, 2, 1]


def test_unresolved_recovery_for_another_task_still_suppresses_launch():
    values = _inputs()
    task = {**values["tasks"][0], "status": "blocked"}
    values["actions"] = generate_controller_actions(task, values["workflow"])
    values["recovery"] = [{"operation_id": "op-1", "workflow_id": "wf-1", "status": "pending",
                           "payload": {"task_ids": ["different-task"]}}]
    assert _project(**values)["actions"] == []


def test_cleaned_task_with_unresolved_gate_is_not_obsolete():
    values = _inputs()
    values["tasks"][0].update(status="cleaned", stage_verdict="blocked")
    values["actions"] = generate_controller_actions(values["tasks"][0], values["workflow"])
    actions = _project(**values)["actions"]
    assert [a["action_id"] for a in actions] == ["T1:force_pass_advance"]
    assert actions[0]["automation"]["mode"] == "confirmation_required"


def test_workflow_action_cannot_resume_a_different_execution():
    from herdr.controller_actions import collect_workflow_actions
    values = _inputs()
    values["workflow"]["status"] = "paused"
    values["actions"] = collect_workflow_actions([], values["workflow"], workflow_paused=True)
    values["workflow"]["execution_id"] = "exec-new"
    assert _project(**values)["actions"] == []


def test_workflow_action_with_missing_expected_execution_stays_blocked():
    from herdr.controller_actions import collect_workflow_actions
    values = _inputs()
    values["workflow"]["status"] = "paused"
    action = collect_workflow_actions([], values["workflow"], workflow_paused=True)[0].to_dict()
    action["expected_identity"] = {}
    assert _project(**{**values, "actions": [action]})["actions"][0]["automation"]["mode"] == "blocked"


@pytest.mark.parametrize("kind,flags,reason", [
    ("halt", {}, "destructive"), ("supersede", {}, "destructive"),
    ("close_workflow", {"accept_escalated": True}, "evidence_bypass"),
])
def test_action_semantics_enforce_safety_even_without_legacy_flags(kind, flags, reason):
    values = _inputs()
    action = generate_progress_actions(values["tasks"][0], values["workflow"])[0].to_dict()
    action["api_payload"].update(type=kind, **flags)
    action["is_destructive"] = False
    action["category"] = "recovery"
    projected = _project(**{**values, "actions": [action]})["actions"][0]
    assert projected["automation"] == {"mode": "confirmation_required", "source": "safety", "reason": reason}


def test_real_sqlite_snapshot_and_kernel_transition_invalidate_old_action(tmp_path):
    from herdr import kernel
    from herdr.controller_actions import collect_workflow_actions
    from herdr.controller_cockpit import build_cockpit_projection
    from herdr.state_store import SQLiteStateStore
    from herdr.workflow_graph import workflow_graph_projection

    store = SQLiteStateStore(tmp_path / "state.db")
    values = _inputs()
    store.save_workflow({**values["workflow"], "config": values["config"]})
    store.save_task(values["tasks"][0])
    values["config"]["nodes"][0]["control_mode"] = "supervised"
    store.save_workflow({**values["workflow"], "config": values["config"]})
    workflow, config, tasks = store.read_workflow_snapshot("wf-1")
    old_actions = generate_progress_actions(tasks[0], workflow)
    assert old_actions[0].expected_identity["task_version"] == 1

    kernel.transition_task("T1", "blocked", reason="fixture review", store=store)
    workflow, config, tasks = store.read_workflow_snapshot("wf-1")
    assert tasks[0]["version"] == 2
    graph = workflow_graph_projection({**config, **workflow}, tasks)
    inputs = {"workflow": workflow, "config": config, "tasks": tasks, "graph": graph,
              "controller": {"status": "running"}, "recovery": [],
              "events": store.list_events(workflow_id="wf-1", desc=True, limit=100)}
    assert build_cockpit_projection(**inputs, actions=old_actions)["actions"] == []
    projection = build_cockpit_projection(**inputs, actions=collect_workflow_actions(tasks, workflow))
    rework = next(a for a in projection["actions"] if a["action_id"] == "T1:rework")
    assert rework["automation"] == {"mode": "confirmation_required", "source": "node"}
    assert rework["expected_identity"]["task_version"] == 2
    assert rework["target"]["run_id"] == "run-9"
    assert projection["read_state"] == "confirmed"
    assert projection["judgment"]["requires_user"] is True
    assert projection["timeline"]
