"""Runtime Closure regression tests (PR #95 freeze, group 1).

No real panes, agents, or models. Covers:
1. `herdr-task launch --execution-id` inheritance.
2. Controller launch argv carrying `--execution-id`.
3. Context compiled after the dispatched transition (not at pending).
4. `herdr-task working-context get` loader round-trip.
5. implementation -> review Handoff chain on shared execution_id.
"""

from __future__ import annotations

import ast
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from herdr import state_db
from herdr.observation import ObservationStore

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_script_module(name, relative_path):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(
            name, str(ROOT / relative_path)
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_launch_help_lists_execution_id():
    result = subprocess.run(
        [sys.executable, "bin/herdr-task", "launch", "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--execution-id" in result.stdout


def test_apply_execution_identity_sets_task_field():
    mod = _load_script_module("herdr_task_cli_test", "bin/herdr-task")

    task = {"task_id": "t-exec", "workflow_id": "wf-exec"}
    args = types.SimpleNamespace(execution_id="exec-1")
    mod._apply_execution_identity(task, args)
    assert task["execution_id"] == "exec-1"

    legacy = {"task_id": "t-legacy", "workflow_id": "wf-exec"}
    mod._apply_execution_identity(legacy, types.SimpleNamespace(execution_id=None))
    assert "execution_id" not in legacy


def test_controller_launch_passes_execution_id(tmp_path):
    from unittest.mock import patch

    ctrl = _load_script_module("ctrl_runtime_closure_test", "services/herdr-controller.py")
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    from herdr import direct_dispatch as dd  # noqa: F401 (ensure planner importable)

    patchers = [
        patch.object(
            ctrl, "project_for_workflow",
            return_value={
                "startup_ready": True,
                "project_root": "/tmp/proj",
                "coordinator_pane_id": "w1:p1",
                "requirement": "runtime closure",
            },
        ),
        patch.object(ctrl, "load_tasks", return_value=[]),
        patch.object(ctrl, "get_stage_policy", return_value={}),
        patch.object(ctrl, "latest_branch_for_node", return_value=None),
        patch.object(ctrl, "mark_stage_advance_notified"),
        patch.object(ctrl, "maybe_compact_coordinator"),
        patch.object(ctrl, "maybe_dispatch_node_handoffs", return_value=[]),
        patch.object(ctrl.subprocess, "run", side_effect=fake_run),
    ]
    for patcher in patchers:
        patcher.start()
    try:
        item = {
            "kind": "stage_advance",
            "workflow_id": "wf-1",
            "stage": "implementation",
            "node_id": "test",
            "next_stage": "test",
            "node": {
                "id": "test",
                "label": "test",
                "purpose": "verify implementation output",
                "default_task_type": "test",
                "default_integration_mode": "none",
            },
        }
        assert ctrl.try_direct_stage_advance(item) is True
    finally:
        for patcher in patchers:
            patcher.stop()

    launch_cmds = [cmd for cmd in commands if "launch" in cmd]
    assert launch_cmds, "expected at least one launch argv"
    cmd = launch_cmds[0]
    assert "--execution-id" in cmd
    assert cmd[cmd.index("--execution-id") + 1] == "wf-1"


def test_dispatch_compiles_context_after_dispatched_transition():
    source = (ROOT / "bin" / "herdr-task").read_text(encoding="utf-8")
    tree = ast.parse(source)
    dispatch_fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "dispatch_task"
    )
    order = []

    class CallOrder(ast.NodeVisitor):
        def visit_Call(self, call):  # noqa: N802 (AST visitor convention)
            func = call.func
            if isinstance(func, ast.Name):
                order.append(func.id)
            self.generic_visit(call)

    CallOrder().visit(dispatch_fn)
    assert "set_status" in order
    assert "_compile_working_context_ref" in order
    assert order.index("set_status") < order.index("_compile_working_context_ref"), (
        "context must be compiled after the dispatched transition, "
        f"call order: {order}"
    )


def _seed_workflow(db_path: Path, workflow_id: str = "wf-e2e") -> None:
    state_db.save_workflow(
        {
            "workflow_id": workflow_id,
            "title": "runtime closure",
            "status": "running",
            "config": {"nodes": [{"id": "implementation"}, {"id": "review"}]},
        },
        db_path=db_path,
    )


def _seed_task(db_path: Path, task: dict) -> dict:
    state_db.save_task(task, db_path=db_path)
    return task


def test_working_context_loader_cli_roundtrip(tmp_path: Path):
    from herdr.context_compiler import compile_working_context
    from herdr.context_projection import _config

    db_path = tmp_path / "state.db"
    _seed_workflow(db_path)
    task = _seed_task(db_path, {
        "task_id": "task-loader", "run_id": "run-loader",
        "workflow_id": "wf-e2e", "execution_id": "exec-e2e",
        "node": "implementation", "agent": "developer",
        "status": "dispatched", "goal": "load me",
        "created_at": 1.0,
    })
    context = compile_working_context(
        workflow_id="wf-e2e", task_id=task["task_id"],
        agent_role="developer", store=ObservationStore(db_path),
    )
    payload = dict(context.to_mapping())
    state_db.save_working_context(
        payload, db_path=db_path, fingerprint_config=_config(None)
    )

    env = os.environ.copy()
    env["HERDR_STATE_DB"] = str(db_path)
    result = subprocess.run(
        [sys.executable, "bin/herdr-task", "working-context",
         "get", "--context-id", context.context_id],
        cwd=ROOT, env=env, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    loaded = json.loads(result.stdout)
    assert loaded["context_id"] == context.context_id
    assert loaded["task_id"] == "task-loader"

    missing = subprocess.run(
        [sys.executable, "bin/herdr-task", "working-context",
         "get", "--context-id", "wc_missing"],
        cwd=ROOT, env=env, text=True, capture_output=True, check=False,
    )
    assert missing.returncode == 2


def test_e2e_handoff_chain_with_execution_id(tmp_path: Path, monkeypatch):
    from herdr import state_db as _sdb
    from herdr.context_compiler import (
        compile_working_context,
        get_working_context,
    )

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(db_path))
    _seed_workflow(db_path)
    impl = _seed_task(db_path, {
        "task_id": "task-impl", "run_id": "run-impl",
        "workflow_id": "wf-e2e", "execution_id": "exec-e2e",
        "node": "implementation", "agent": "developer",
        "status": "completed", "goal": "implement",
        "created_at": 1.0,
        "artifacts": [{"ref": "impl-artifact"}],
    })
    review = _seed_task(db_path, {
        "task_id": "task-review", "run_id": "run-review",
        "workflow_id": "wf-e2e", "execution_id": "exec-e2e",
        "node": "review", "agent": "reviewer",
        "status": "dispatched", "goal": "review",
        "created_at": 2.0,
    })
    foreign = _seed_task(db_path, {
        "task_id": "task-foreign", "run_id": "run-foreign",
        "workflow_id": "wf-e2e", "execution_id": "exec-other",
        "node": "implementation", "agent": "developer",
        "status": "completed", "goal": "foreign",
        "created_at": 1.0,
        "artifacts": [{"ref": "foreign-artifact"}],
    })
    assert impl and review and foreign

    event = _sdb.create_collaboration_event({
        "run_id": "exec-e2e",
        "workflow_id": "wf-e2e",
        "from_task_id": "task-impl",
        "to_task_id": "task-review",
        "type": "HANDOFF",
        "source_fact_id": "wf-e2e:implementation:completed",
    }, db_path=db_path)
    context = compile_working_context(
        workflow_id="wf-e2e", task_id="task-review",
        agent_role="reviewer", store=ObservationStore(db_path),
        planned_links=[{
            "from_task_id": "task-impl", "to_task_id": "task-review",
        }],
    )
    event = _sdb.attach_working_context_ref(
        event["event_id"], context.context_id, db_path=db_path
    )

    refs = set(context.source_refs)
    assert "task:task-impl:artifact:0" in refs
    assert "foreign-artifact" not in json.dumps(context.to_mapping())
    assert context.context_id in (event.get("context_refs") or [])

    loaded = get_working_context(context.context_id, db_path=db_path)
    assert loaded is not None
    assert loaded.context_id == context.context_id


def test_execution_scope_handoff_finds_completed_upstream(tmp_path: Path, monkeypatch):
    ctrl = _load_script_module(
        "ctrl_runtime_closure_handoff_test", "services/herdr-controller.py"
    )
    monkeypatch.setenv("HERDR_STATE_DB", str(tmp_path / "state.db"))
    db_path = tmp_path / "state.db"
    _seed_workflow(db_path)
    _seed_task(db_path, {
        "task_id": "task-impl", "run_id": "run-impl",
        "workflow_id": "wf-e2e", "execution_id": "exec-e2e",
        "node": "implementation", "agent": "developer",
        "status": "completed", "goal": "implement",
        "pane_id": "pane-impl", "created_at": 1.0,
        "updated_at": 3.0,
    })
    _seed_task(db_path, {
        "task_id": "task-review", "run_id": "run-review",
        "workflow_id": "wf-e2e", "execution_id": "exec-e2e",
        "node": "review", "agent": "reviewer",
        "status": "dispatched", "goal": "review",
        "pane_id": "pane-review", "created_at": 2.0,
        "updated_at": 4.0,
    })

    calls = []

    def fake_sender(pane_id, prompt):
        calls.append((pane_id, prompt))
        return {"ok": True}

    results = ctrl.maybe_dispatch_node_handoffs(
        workflow_id="wf-e2e", ready_id="review",
        dep_ids=["implementation"], launched=["task-review"],
        prompt_sender=fake_sender, db_path=db_path,
    )
    assert results and results[0].get("status") != "failed", results
    assert not any(
        r.get("reason") == "no_completed_upstream" for r in results
    ), results
    assert calls and calls[0][0] == "pane-review"
