#!/usr/bin/env python3
"""Tests for Herdr Factory Console Controller Actions API endpoints.

Covers:
- GET  /api/workflow/controller-actions
- POST /api/controller/execute-action
- Exclusion of superseded tasks in blocker resolution
"""

import json
import pytest
from unittest.mock import patch, MagicMock

from console import herdr_factory_console as c
from tests.test_node_dispatch_contract import scene


@pytest.fixture
def console_actions_env(tmp_path, monkeypatch):
    wf_file = tmp_path / "workflows.json"
    tasks_file = tmp_path / "tasks.json"

    monkeypatch.setenv("WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setenv("TASKS_FILE", str(tasks_file))
    monkeypatch.setattr(c.common, "WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setattr(c.common, "TASKS_FILE", str(tasks_file))
    db = tmp_path / "state.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(db))

    # Seed a workflow with a superseded blocked task and an active failed task
    wf_data = {
        "workflows": {
            "wf-test-01": {
                "workflow_id": "wf-test-01",
                "title": "测试工作流",
                "status": "running",
                "project_root": "/tmp/test-proj",
                "config": {"nodes": []},
            }
        }
    }
    tasks_data = {
        "tasks": [
            {
                "task_id": "wf-test-01-old",
                "workflow_id": "wf-test-01",
                "stage": "plan",
                "status": "superseded",
                "stage_verdict": "blocked",
                "stage_verdict_note": "旧阻断",
            },
            {
                "task_id": "wf-test-01-test",
                "workflow_id": "wf-test-01",
                "stage": "test",
                "status": "failed",
                "stage_verdict": "blocked",
                "stage_verdict_note": "FAIL: 2 blocking defects",
                "agent": "qodercli",
                "version": 1,
            }
        ]
    }

    wf_file.write_text(json.dumps(wf_data), encoding="utf-8")
    tasks_file.write_text(json.dumps(tasks_data), encoding="utf-8")

    from herdr import state_db
    state_db.init_db(db)
    state_db.save_workflow(wf_data["workflows"]["wf-test-01"], db_path=db)
    for t in tasks_data["tasks"]:
        state_db.save_task(t, db_path=db)

    return {"wf_file": wf_file, "tasks_file": tasks_file}


def test_api_workflow_controller_actions_query(console_actions_env):
    res = c.api_workflow_controller_actions("wf-test-01")
    assert res["workflow_id"] == "wf-test-01"
    assert "blockers" in res
    assert "actions" in res

    # Superseded task must be excluded!
    blocker_ids = [b["task_id"] for b in res["blockers"]]
    assert "wf-test-01-old" not in blocker_ids
    assert "wf-test-01-test" in blocker_ids

    # Recovery still excludes redispatch/bypass actions; workflow controls are
    # now centralized here instead of executing from the page header.
    assert not any(a['api_payload'].get('type') in {'launch', 'rework', 'force_pass_advance'}
                   for a in res['actions'])
    assert all(a['target']['workflow_id'] == 'wf-test-01' for a in res['actions'])
    assert len(res['recovery']) == 1
    assert res['recovery'][0]['payload']['task_ids'] == ['wf-test-01-test']
    assert res['recovery'][0]['status'] == 'waiting_human'


def test_api_controller_execute_action_launch(console_actions_env):
    payload = {
        "type": "launch",
        "task_id": "wf-test-01-fix-1",
        "workflow_id": "wf-test-01",
        "stage": "implementation",
        "agent": "codex",
        "prompt": "修复测试缺陷",
    }
    with patch.object(c.common, "run") as mock_run:
        c.api_workflow_controller_actions('wf-test-01')
        with pytest.raises(RuntimeError, match='持久恢复'):
            c.api_controller_execute_action(payload)
        mock_run.assert_not_called()


def test_api_controller_execute_action_force_pass_advance(console_actions_env):
    # Missing confirmation must be refused
    with pytest.raises(RuntimeError):
        c.api_controller_execute_action({
            "type": "force_pass_advance",
            "workflow_id": "wf-test-01",
            "stage": "test",
            "gate_node_id": "test",
        })

    # Missing expected_version must be refused (isolate from recovery obligation check)
    with patch.object(c.workflow_api, "api_workflow_recovery", return_value={"operations": []}):
        with pytest.raises(RuntimeError) as exc:
            c.api_controller_execute_action({
                "type": "force_pass_advance",
                "workflow_id": "wf-test-01",
                "stage": "test",
                "gate_node_id": "test",
                "confirmed": True,
                "reason": "人工在控制台审核确认通过",
            })
    assert "版本快照保护字段" in str(exc.value)

    payload = {
        "type": "force_pass_advance",
        "workflow_id": "wf-test-01",
        "task_id": "wf-test-01-test",
        "stage": "test",
        "gate_node_id": "test",
        "expected_version": 1,
        "confirmed": True,
        "reason": "人工在控制台审核确认通过",
    }
    with patch.object(c.herdr_kernel, "force_pass_gate") as mock_gate, patch.object(c.workflow_api, "manual_advance") as mock_adv:
        c.api_workflow_controller_actions('wf-test-01')
        with pytest.raises(RuntimeError, match='持久恢复'):
            c.api_controller_execute_action(payload)
        mock_gate.assert_not_called()
        mock_adv.assert_not_called()


def test_advance_receipt_waits_for_durable_dispatch_not_http_success(scene, monkeypatch):
    from herdr import recovery_store
    def deliver(wid):
        scene.ctrl._handle_coordinator_item(scene.item)
        operation = next(op for op in recovery_store.list_operations(scene.store.db_path, wid)
                         if op['payload']['kind'] == 'node_dispatch')
        return {'ok': True, 'task_id': 'not-registered', 'operation_id': operation['id']}
    monkeypatch.setattr(c.workflow_api, 'manual_advance', deliver)
    result = c.api_controller_execute_action({'workflow_id': 'wf', 'type': 'advance'})
    assert result['receipt']['status'] == 'accepted_waiting'
    assert result['receipt']['task_id'] == 'not-registered'
    assert scene.store.get_task('not-registered') is None
    operation = next(op for op in recovery_store.list_operations(scene.store.db_path, 'wf')
                     if op['id'] == result['receipt']['operation_id'])
    assert operation['status'] == 'awaiting_result'
    assert operation['started'] == 1
    from services.controller.cockpit_automation import read_cockpit
    assert read_cockpit(scene.store, 'wf', controller={'status': 'running'})['receipts'][0] == result['receipt']


def test_legacy_version_rejection_stays_queryable_without_changing_exception_contract(console_actions_env):
    from services.controller.cockpit_automation import read_cockpit
    with pytest.raises(RuntimeError, match='版本已变化'):
        c.api_controller_execute_action({'workflow_id': 'wf-test-01', 'type': 'halt',
                                        'task_id': 'wf-test-01-test', 'expected_version': 999})
    cockpit = read_cockpit(c.herdr_kernel._get_store(), 'wf-test-01', controller={'status': 'running'})
    assert cockpit['receipts'][0]['status'] == 'rejected'
    assert cockpit['receipts'][0]['outcome'] == 'rejected'
    assert c.herdr_kernel._get_store().get_task('wf-test-01-test')['status'] == 'failed'
