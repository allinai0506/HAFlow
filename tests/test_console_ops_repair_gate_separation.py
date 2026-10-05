#!/usr/bin/env python3
"""Regression tests: ops repair and gate pass separation.

Verifies:
1. 'ops_repair' / 'retry' on unrepairable blocked/failed task MUST NOT force pass the gate or advance.
2. Recovery commands failing or timing out preserve error and do not escalate to force pass.
3. Requests with missing/mismatched workflow, task, node, or version are rejected.
4. Valid recovery commands use legitimate recovery path without writing gate overrides.
5. Force pass without explicit confirmation, reason, or specific target node is rejected.
6. Legitimate force pass only affects confirmed scope and records audit details.
7. Force pass success followed by advance failure reports partial completion accurately.
8. Front-end cancellation / confirmation behavior and feedback contracts.
9. Integrated HTTP route -> Handler -> SQLite state -> readback verification.
"""

import json
import socket
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
import pytest
from unittest.mock import patch, MagicMock

from console import herdr_factory_console as c
from herdr.state_store import get_state_store


@pytest.fixture
def ops_test_env(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    wf_file = tmp_path / "workflows.json"
    tasks_file = tmp_path / "tasks.json"

    monkeypatch.setenv("HERDR_STATE_DB", str(db_path))
    monkeypatch.setenv("WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setenv("TASKS_FILE", str(tasks_file))
    monkeypatch.setattr(c, "WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setattr(c, "TASKS_FILE", str(tasks_file))

    store = get_state_store(db_path)

    wf_data = {
        "workflow_id": "wf-ops-01",
        "title": "运维修复隔离测试",
        "status": "running",
        "project_root": str(tmp_path / "test-proj"),
        "config": {
            "nodes": [
                {"id": "plan", "type": "agent"},
                {"id": "implementation", "type": "agent"},
                {"id": "test", "type": "agent"},
            ]
        },
    }
    store.save_workflow(wf_data)

    task_data = {
        "task_id": "wf-ops-01-test",
        "workflow_id": "wf-ops-01",
        "stage": "test",
        "node": "test",
        "agent": "qodercli",
        "status": "failed",
        "stage_verdict": "blocked",
        "stage_verdict_note": "FAIL: 2 blocking defects",
        "pane_id": None,
        "version": 1,
    }
    store.save_task(task_data)

    # Add a reusable blocked task for testing legitimate rework
    task_rework = {
        "task_id": "wf-ops-01-reworkable",
        "workflow_id": "wf-ops-01",
        "stage": "implementation",
        "node": "implementation",
        "agent": "codex",
        "status": "blocked",
        "stage_verdict": "blocked",
        "stage_verdict_note": "rework needed",
        "pane_id": "pane-rework-01",
        "version": 1,
    }
    store.save_task(task_rework)

    # Add a working task with live pane for testing redrive
    task_redrive = {
        "task_id": "wf-ops-01-redrivable",
        "workflow_id": "wf-ops-01",
        "stage": "implementation",
        "node": "implementation",
        "agent": "codex",
        "status": "working",
        "pane_id": "pane-redrive-01",
        "version": 1,
    }
    store.save_task(task_redrive)

    # Sync files
    wf_file.write_text(json.dumps({"workflows": {"wf-ops-01": wf_data}}), encoding="utf-8")
    tasks_file.write_text(json.dumps({"tasks": [task_data, task_rework, task_redrive]}), encoding="utf-8")

    return {
        "db_path": db_path,
        "wf_file": wf_file,
        "tasks_file": tasks_file,
        "store": store,
    }


# 1. rework 不适用，且没有安全恢复动作：明确拒绝；不放行、不推进
def test_scenario_1_rework_not_applicable_refuses_without_bypass(ops_test_env):
    payload = {
        "type": "ops_repair",
        "workflow_id": "wf-ops-01",
        "task_id": "wf-ops-01-test",
        "node": "test",
    }
    with pytest.raises(RuntimeError) as exc_info:
        c.api_controller_execute_action(payload)

    err = str(exc_info.value)
    assert "无安全可执行" in err or "无法自动修复" in err or "没有可执行" in err

    store = ops_test_env["store"]
    t = store.get_task("wf-ops-01-test")
    assert t["stage_verdict"] == "blocked"
    wf = store.get_workflow("wf-ops-01")
    assert "test" not in (wf.get("gate_overrides") or {})


# 2. 恢复命令失败或超时：保留失败或未知结果；不继续执行其他动作，绝不降级放行
def test_scenario_2_recovery_command_failure_or_timeout_preserves_error(ops_test_env):
    payload = {
        "type": "ops_repair",
        "workflow_id": "wf-ops-01",
        "task_id": "wf-ops-01-reworkable",
        "node": "implementation",
    }
    # Case A: command returns non-zero error
    with patch.object(c, "run", side_effect=RuntimeError("工位通信中断")):
        with pytest.raises(RuntimeError) as exc_info:
            c.api_controller_execute_action(payload)
        assert "工位通信中断" in str(exc_info.value)

    # Case B: timeout
    with patch.object(c, "run", side_effect=RuntimeError("命令超时: herdr rework")):
        with pytest.raises(RuntimeError) as exc_info:
            c.api_controller_execute_action(payload)
        assert "超时" in str(exc_info.value)

    # In both cases, gate must not be overridden
    store = ops_test_env["store"]
    t = store.get_task("wf-ops-01-reworkable")
    assert t["stage_verdict"] == "blocked"
    wf = store.get_workflow("wf-ops-01")
    assert "implementation" not in (wf.get("gate_overrides") or {})


# 3. 任务不存在、跨工作流、节点错配或请求过期：拒绝请求；没有越界状态修改
def test_scenario_3_mismatched_or_expired_requests_rejected(ops_test_env):
    # 3.1 Task not found
    with pytest.raises(RuntimeError) as exc:
        c.api_controller_execute_action({
            "type": "ops_repair",
            "workflow_id": "wf-ops-01",
            "task_id": "non-existent-task",
            "node": "test",
        })
    assert "未找到任务" in str(exc.value)

    # 3.2 Cross-workflow mismatch
    # Seed another workflow with a task
    store = ops_test_env["store"]
    store.save_workflow({"workflow_id": "wf-ops-02", "title": "Other WF", "status": "running"})
    store.save_task({
        "task_id": "wf-ops-02-task",
        "workflow_id": "wf-ops-02",
        "stage": "test",
        "node": "test",
        "status": "failed",
    })
    with pytest.raises(RuntimeError) as exc:
        c.api_controller_execute_action({
            "type": "ops_repair",
            "workflow_id": "wf-ops-01",
            "task_id": "wf-ops-02-task",
            "node": "test",
        })
    assert "不属于工作流" in str(exc.value) or "未找到任务" in str(exc.value)

    # 3.3 Node mismatch
    with pytest.raises(RuntimeError) as exc:
        c.api_controller_execute_action({
            "type": "ops_repair",
            "workflow_id": "wf-ops-01",
            "task_id": "wf-ops-01-test",
            "node": "wrong-node",
        })
    assert "节点错配" in str(exc.value)

    # 3.4 Version mismatch
    with pytest.raises(RuntimeError) as exc:
        c.api_controller_execute_action({
            "type": "ops_repair",
            "workflow_id": "wf-ops-01",
            "task_id": "wf-ops-01-test",
            "node": "test",
            "expected_version": 999,
        })
    assert "版本已变化" in str(exc.value)

    # 3.5 Superseded task
    store.save_task({
        "task_id": "wf-ops-01-superseded",
        "workflow_id": "wf-ops-01",
        "stage": "test",
        "node": "test",
        "status": "superseded",
        "stage_verdict": "blocked",
    })
    with pytest.raises(RuntimeError) as exc:
        c.api_controller_execute_action({
            "type": "ops_repair",
            "workflow_id": "wf-ops-01",
            "task_id": "wf-ops-01-superseded",
            "node": "test",
        })
    assert "已被替换" in str(exc.value) or "作废" in str(exc.value)


# 4. 合法的普通恢复：走既有恢复入口；不写入人工放行记录
def test_scenario_4_legitimate_normal_recovery_succeeds_without_gate_override(ops_test_env):
    payload = {
        "type": "ops_repair",
        "workflow_id": "wf-ops-01",
        "task_id": "wf-ops-01-reworkable",
        "node": "implementation",
    }
    calls = []
    with patch.object(c, "run", side_effect=lambda cmd, timeout=20, check=False:
                      (calls.append(list(cmd)),
                       MagicMock(returncode=0, stdout="reworked", stderr=""))[1]):
        res = c.api_controller_execute_action(payload)

    assert res.get("ok") is True
    assert len(calls) == 1
    assert "rework" in calls[0]
    assert "wf-ops-01-reworkable" in calls[0]

    # No gate overrides written
    store = ops_test_env["store"]
    wf = store.get_workflow("wf-ops-01")
    assert "implementation" not in (wf.get("gate_overrides") or {})
    t = store.get_task("wf-ops-01-reworkable")
    assert t["stage_verdict"] == "blocked"


# 5. 人工放行缺少确认、原因或明确目标：服务端拒绝；状态不变
def test_scenario_5_force_pass_requires_confirmation_reason_and_target(ops_test_env):
    # 5.1 Missing confirmation
    with pytest.raises(RuntimeError) as exc:
        c.api_controller_execute_action({
            "type": "force_pass",
            "workflow_id": "wf-ops-01",
            "gate_node_id": "test",
            "reason": "人工核查无误",
        })
    assert "显式确认" in str(exc.value) or "缺少确认" in str(exc.value)

    # 5.2 Empty or default reason
    for invalid_reason in ["", "   ", "人类在控制台强制放行并推进", "运维驾驶舱异常中枢一键修复放行"]:
        with pytest.raises(RuntimeError) as exc:
            c.api_controller_execute_action({
                "type": "force_pass",
                "workflow_id": "wf-ops-01",
                "gate_node_id": "test",
                "confirmed": True,
                "reason": invalid_reason,
            })
        assert "原因" in str(exc.value)

    # 5.3 Missing specific target node
    with pytest.raises(RuntimeError) as exc:
        c.api_controller_execute_action({
            "type": "force_pass",
            "workflow_id": "wf-ops-01",
            "confirmed": True,
            "reason": "人工核查通过",
            "gate_node_id": "",
        })
    assert "明确的目标门禁节点" in str(exc.value) or "缺少" in str(exc.value)

    # 5.4 Foreign node not in workflow
    with pytest.raises(RuntimeError) as exc:
        c.api_controller_execute_action({
            "type": "force_pass",
            "workflow_id": "wf-ops-01",
            "confirmed": True,
            "reason": "人工核查通过",
            "gate_node_id": "unknown-nonexistent-node",
        })
    assert "不属于工作流" in str(exc.value)

    # State untouched
    store = ops_test_env["store"]
    t = store.get_task("wf-ops-01-test")
    assert t["stage_verdict"] == "blocked"
    wf = store.get_workflow("wf-ops-01")
    assert "test" not in (wf.get("gate_overrides") or {})


# 6. 合法人工放行：仅影响确认范围，并留下对应审计记录
def test_scenario_6_legitimate_force_pass_scoped_with_audit(ops_test_env):
    payload = {
        "type": "force_pass_advance",
        "workflow_id": "wf-ops-01",
        "gate_node_id": "test",
        "confirmed": True,
        "reason": "已知非核心偶发用例失败，主管核查允许放行",
        "operator": "lead_engineer",
    }
    with patch.object(c, "manual_advance", return_value={"completed_stage": "test", "next_stage": "wrapup"}):
        res = c.api_controller_execute_action(payload)

    assert res.get("ok") is True
    assert res.get("gate_passed") is True
    assert res.get("advanced") == {"completed_stage": "test", "next_stage": "wrapup"}

    store = ops_test_env["store"]
    t = store.get_task("wf-ops-01-test")
    assert t["stage_verdict"] == "pass"
    assert "lead_engineer" in t["stage_verdict_note"]
    assert "已知非核心偶发用例失败" in t["stage_verdict_note"]

    # Other task in implementation untouched
    t_impl = store.get_task("wf-ops-01-reworkable")
    assert t_impl["stage_verdict"] == "blocked"

    wf = store.get_workflow("wf-ops-01")
    overrides = wf.get("gate_overrides") or {}
    assert "test" in overrides
    assert overrides["test"]["operator"] == "lead_engineer"
    assert overrides["test"]["note"] == "已知非核心偶发用例失败，主管核查允许放行"


# 7. 放行已完成，但后续推进失败：如实返回部分完成，不吞错、不伪报全部成功
def test_scenario_7_force_pass_succeeds_but_advance_fails_returns_partial(ops_test_env):
    payload = {
        "type": "force_pass_advance",
        "workflow_id": "wf-ops-01",
        "gate_node_id": "test",
        "confirmed": True,
        "reason": "测试门禁人工豁免",
        "operator": "admin",
    }
    with patch.object(c, "manual_advance", side_effect=RuntimeError("后续阶段 wrapup 依赖未就绪")):
        res = c.api_controller_execute_action(payload)

    # Must report partial=True, ok=False, not pseudo-success
    assert res.get("ok") is False
    assert res.get("partial") is True
    assert res.get("gate_passed") is True
    assert "后续阶段 wrapup 依赖未就绪" in (res.get("advance_error") or res.get("error") or "")

    # Gate was indeed passed
    store = ops_test_env["store"]
    t = store.get_task("wf-ops-01-test")
    assert t["stage_verdict"] == "pass"


# 8. 前端取消确认或后端返回拒绝：不执行变更，或明确展示失败
def test_scenario_8_frontend_templates_and_safety_checks(ops_test_env):
    html = getattr(c, "HTML_TEMPLATE", "")
    # Check that confirmOpsForcePass exists in HTML/JS
    assert "confirmOpsForcePass" in html
    # Check that reason is required and cancelled clicks do not call api
    assert "forcePassReason" in html or "ctlReasonInput" in html
    # Check anti-double-click guard
    assert "_opsActionBusy" in html
    # Check differentiated feedback logic in runOpsAnomalyAction
    assert "res.partial" in html or "partial" in html


# 9. 真实 HTTP 路由 -> Handler -> SQLite 临时数据库 -> 状态回读端到端集成测试
def test_scenario_9_http_route_integration_to_sqlite(ops_test_env):
    """End-to-end integration test through real HTTP server, Handler, and SQLite."""
    # Find free port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = ThreadingHTTPServer(("127.0.0.1", port), c.Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        url = f"http://127.0.0.1:{port}/api/controller/execute-action"
        post_data = json.dumps({
            "type": "ops_repair",
            "workflow_id": "wf-ops-01",
            "task_id": "wf-ops-01-test",
            "node": "test",
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=post_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        resp_body = {}
        status_code = None
        try:
            with urllib.request.urlopen(req) as resp:
                status_code = resp.status
                resp_body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            status_code = e.code
            resp_body = json.loads(e.read().decode("utf-8"))

        # The request must be rejected (either 500 error or ok: False in body)
        assert status_code in (400, 500) or resp_body.get("ok") is False

        # Read back from authoritative SQLite
        store = ops_test_env["store"]
        task = store.get_task("wf-ops-01-test")
        assert task["stage_verdict"] == "blocked", "Integration test failed: stage_verdict was changed to pass!"
        wf = store.get_workflow("wf-ops-01")
        assert "test" not in (wf.get("gate_overrides") or {}), "Integration test failed: gate_overrides was modified!"
    finally:
        server.shutdown()
        server.server_close()
