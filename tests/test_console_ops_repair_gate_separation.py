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
    assert "kernelForcePassReason" in html
    assert "btnSubmitKernelForcePass" in html
    # Check anti-double-click guard
    assert "_opsActionBusy" in html
    # Check differentiated feedback logic in runOpsAnomalyAction
    assert "res.partial" in html or "partial" in html
    # Check expected_version and expected_pane_id bindings in frontend
    assert "expected_version" in html
    assert "expected_pane_id" in html


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


# 10. 目标节点参数冲突拒绝，无状态副作用（校验与执行统一单一目标节点）
def test_scenario_10_conflicting_node_parameters_rejected(ops_test_env):
    """Rejection when node, stage, or gate_node_id have conflicting distinct values."""
    # 10.1 In api_controller_execute_action
    payload = {
        "type": "force_pass",
        "workflow_id": "wf-ops-01",
        "task_id": "wf-ops-01-test",
        "node": "test",
        "gate_node_id": "implementation",
        "confirmed": True,
        "reason": "测试参数冲突防护",
    }
    with pytest.raises(RuntimeError) as exc:
        c.api_controller_execute_action(payload)
    assert "目标节点参数冲突" in str(exc.value)

    # Verify zero side-effects
    store = ops_test_env["store"]
    assert store.get_task("wf-ops-01-test")["stage_verdict"] == "blocked"
    assert store.get_task("wf-ops-01-reworkable")["stage_verdict"] == "blocked"
    wf = store.get_workflow("wf-ops-01")
    assert not (wf.get("gate_overrides") or {})

    # 10.2 In api_kernel_force_pass
    k_payload = {
        "workflow_id": "wf-ops-01",
        "gate_node_id": "test",
        "node": "plan",
        "confirmed": True,
        "reason": "测试参数冲突防护",
    }
    with pytest.raises(RuntimeError) as exc2:
        c.api_kernel_force_pass(k_payload)
    assert "目标节点参数冲突" in str(exc2.value)
    assert not (store.get_workflow("wf-ops-01").get("gate_overrides") or {})


# 11. /api/kernel/force-pass 与控制台放行遵循完全一致的人工豁免校验
def test_scenario_11_kernel_force_pass_parity_validation(ops_test_env):
    """Parity of /api/kernel/force-pass with manual gate force pass constraints."""
    store = ops_test_env["store"]

    # 11.1 Missing confirmation
    with pytest.raises(RuntimeError) as exc:
        c.api_kernel_force_pass({
            "workflow_id": "wf-ops-01",
            "gate_node_id": "test",
            "reason": "人工核验",
        })
    assert "显式确认" in str(exc.value)

    # 11.2 Default or empty note
    for dis in ["human forced pass", "经人工核验，次要阻断项已评估无害，特批放行", "", "   "]:
        with pytest.raises(RuntimeError) as exc:
            c.api_kernel_force_pass({
                "workflow_id": "wf-ops-01",
                "gate_node_id": "test",
                "confirmed": True,
                "note": dis,
            })
        assert "原因" in str(exc.value)

    # 11.3 Missing gate node
    with pytest.raises(RuntimeError) as exc:
        c.api_kernel_force_pass({
            "workflow_id": "wf-ops-01",
            "confirmed": True,
            "reason": "合法人工原因",
            "gate_node_id": "",
        })
    assert "缺少明确的目标门禁节点" in str(exc.value)

    # 11.4 Gate node does not belong to workflow
    with pytest.raises(RuntimeError) as exc:
        c.api_kernel_force_pass({
            "workflow_id": "wf-ops-01",
            "gate_node_id": "nonexistent_node",
            "confirmed": True,
            "reason": "合法人工原因",
        })
    assert "不属于工作流" in str(exc.value)

    # 11.5 Task attribution and node mismatch if task_id provided
    with pytest.raises(RuntimeError) as exc:
        c.api_kernel_force_pass({
            "workflow_id": "wf-ops-01",
            "gate_node_id": "implementation",
            "task_id": "wf-ops-01-test",  # belongs to 'test'
            "confirmed": True,
            "reason": "合法人工原因",
        })
    assert "节点错配" in str(exc.value)

    # 11.6 Valid manual pass via api_kernel_force_pass (releases gate without advancing workflow)
    res = c.api_kernel_force_pass({
        "workflow_id": "wf-ops-01",
        "gate_node_id": "test",
        "task_id": "wf-ops-01-test",
        "confirmed": True,
        "reason": "经过主管和QA联合签名，该测试缺陷已豁免",
        "operator": "lead_qa",
    })
    assert res.get("workflow_id") == "wf-ops-01"
    assert "wf-ops-01-test" in res.get("updated_tasks", [])

    # Check store
    t = store.get_task("wf-ops-01-test")
    assert t["stage_verdict"] == "pass"
    assert "lead_qa" in t["stage_verdict_note"]
    assert "经过主管和QA联合签名" in t["stage_verdict_note"]

    wf = store.get_workflow("wf-ops-01")
    overrides = wf.get("gate_overrides") or {}
    assert "test" in overrides
    assert overrides["test"]["note"] == "经过主管和QA联合签名，该测试缺陷已豁免"
    assert overrides["test"]["operator"] == "lead_qa"


# 12. 旧页面请求遇到新运行实例：必须拒绝
def test_scenario_12_stale_request_meets_new_running_instance(ops_test_env):
    """When a task has moved to a new running instance (pane), stale requests must be rejected."""
    store = ops_test_env["store"]
    # Task has pane-rework-01
    # 12.1 In ops_repair with stale pane
    payload = {
        "type": "ops_repair",
        "workflow_id": "wf-ops-01",
        "task_id": "wf-ops-01-reworkable",
        "node": "implementation",
        "expected_pane_id": "pane-old-99",
    }
    with pytest.raises(RuntimeError) as exc:
        c.api_controller_execute_action(payload)
    assert "任务运行实例已变化" in str(exc.value)

    # 12.2 In api_kernel_force_pass with stale pane
    with pytest.raises(RuntimeError) as exc:
        c.api_kernel_force_pass({
            "workflow_id": "wf-ops-01",
            "task_id": "wf-ops-01-reworkable",
            "gate_node_id": "implementation",
            "expected_pane_id": "pane-old-99",
            "confirmed": True,
            "reason": "人工豁免尝试",
        })
    assert "任务运行实例已变化" in str(exc.value)

    # Ensure no side effects
    t = store.get_task("wf-ops-01-reworkable")
    assert t["stage_verdict"] == "blocked"


# 13. 写入/执行边界版本检查：防止校验后状态再次变化的竞态
def test_scenario_13_write_boundary_version_conflict_rejection(ops_test_env):
    """State change between initial check and write boundary is blocked."""
    store = ops_test_env["store"]
    from herdr import kernel as herdr_kernel

    # 13.1 force_pass_gate write-boundary check with expected_version mismatch
    with pytest.raises(RuntimeError) as exc:
        herdr_kernel.force_pass_gate(
            workflow_id="wf-ops-01",
            gate_node_id="implementation",
            note="合法人工作废",
            store=store,
            expected_version=999,  # actual is 1
        )
    assert "任务版本已在写入边界发生变化" in str(exc.value)

    # Check that task metadata and gate overrides were NOT written
    t = store.get_task("wf-ops-01-reworkable")
    assert t["stage_verdict"] == "blocked"
    wf = store.get_workflow("wf-ops-01")
    assert "implementation" not in (wf.get("gate_overrides") or {})

    # 13.2 ops_repair execution boundary check
    with pytest.raises(RuntimeError) as exc:
        c.api_controller_execute_action({
            "type": "ops_repair",
            "workflow_id": "wf-ops-01",
            "task_id": "wf-ops-01-reworkable",
            "node": "implementation",
            "expected_version": 999,
        })
    assert "任务状态已在执行前发生变化" in str(exc.value) or "任务版本已变化" in str(exc.value)


# 14. 真实 HTTP 路由 -> Handler -> SQLite: /api/kernel/force-pass 集成校验
def test_scenario_14_http_route_kernel_force_pass_integration(ops_test_env):
    """Integration test: /api/kernel/force-pass over HTTP to SQLite."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = ThreadingHTTPServer(("127.0.0.1", port), c.Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        url = f"http://127.0.0.1:{port}/api/kernel/force-pass"

        # 1. Rejected request: default note and no confirmed flag
        bad_req = urllib.request.Request(
            url,
            data=json.dumps({
                "workflow_id": "wf-ops-01",
                "gate_node_id": "test",
                "note": "human forced pass",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        status = None
        try:
            with urllib.request.urlopen(bad_req) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        assert status in (400, 500)

        # Store untouched
        store = ops_test_env["store"]
        assert store.get_task("wf-ops-01-test")["stage_verdict"] == "blocked"

        # 2. Approved request: explicit confirmation and user-provided reason
        good_req = urllib.request.Request(
            url,
            data=json.dumps({
                "workflow_id": "wf-ops-01",
                "gate_node_id": "test",
                "task_id": "wf-ops-01-test",
                "confirmed": True,
                "reason": "集成测试真实放行",
                "operator": "tester",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(good_req) as resp:
            assert resp.status == 200
            body = json.loads(resp.read().decode("utf-8"))
            data = body.get("data") if "data" in body else body
            assert data.get("workflow_id") == "wf-ops-01"

        # Check SQLite store
        assert store.get_task("wf-ops-01-test")["stage_verdict"] == "pass"
        wf = store.get_workflow("wf-ops-01")
        assert "test" in (wf.get("gate_overrides") or {})
        assert (wf.get("gate_overrides") or {})["test"]["note"] == "集成测试真实放行"
    finally:
        server.shutdown()
        server.server_close()


# 15. 并发修改竞态拦截与原子事务保护：同一事务内核验版本、写放行与更新工作流
def test_scenario_15_concurrent_modification_race_rejection(ops_test_env):
    """Verify atomic SQLite transaction protection against concurrent modifications."""
    store = ops_test_env["store"]
    db_path = ops_test_env["db_path"]
    from herdr import kernel as herdr_kernel
    import sqlite3

    # Ensure task is at version 3 with pane 'run-a'
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE tasks SET version = 3, pane_id = 'run-a', stage_verdict = 'blocked' WHERE task_id = 'wf-ops-01-test'"
    )
    conn.commit()

    # Another concurrent writer updates task to version 4 and pane 'run-b' before or during execution
    conn2 = sqlite3.connect(db_path)
    conn2.execute(
        "UPDATE tasks SET version = 4, pane_id = 'run-b' WHERE task_id = 'wf-ops-01-test'"
    )
    conn2.commit()
    conn2.close()

    # Stale attempt with expected_version=3, expected_pane_id='run-a' MUST be rejected
    with pytest.raises(RuntimeError) as exc:
        herdr_kernel.force_pass_gate(
            workflow_id="wf-ops-01",
            gate_node_id="test",
            task_id="wf-ops-01-test",
            expected_version=3,
            expected_pane_id="run-a",
            note="竞态测试放行",
            store=store,
        )
    assert "已变化" in str(exc.value)

    # Verify zero side-effects in SQLite: task verdict not pass, gate_overrides not written
    cur = conn.execute("SELECT stage_verdict, version FROM tasks WHERE task_id = 'wf-ops-01-test'")
    row = cur.fetchone()
    assert row[0] == "blocked"
    assert row[1] == 4

    cur_wf = conn.execute("SELECT metadata_json FROM workflows WHERE workflow_id = 'wf-ops-01'")
    meta = json.loads(cur_wf.fetchone()[0] or "{}")
    assert not (meta.get("gate_overrides") or {})

    # Now with fresh snapshot (version=4, pane='run-b'), it succeeds atomically
    res = herdr_kernel.force_pass_gate(
        workflow_id="wf-ops-01",
        gate_node_id="test",
        task_id="wf-ops-01-test",
        expected_version=4,
        expected_pane_id="run-b",
        note="最新快照放行",
        store=store,
    )
    assert res["ok"] is True
    assert "wf-ops-01-test" in res["updated_tasks"]

    cur = conn.execute("SELECT stage_verdict, version FROM tasks WHERE task_id = 'wf-ops-01-test'")
    row = cur.fetchone()
    assert row[0] == "pass"
    assert row[1] == 5  # incremented by 1

    cur_wf = conn.execute("SELECT metadata_json FROM workflows WHERE workflow_id = 'wf-ops-01'")
    meta = json.loads(cur_wf.fetchone()[0] or "{}")
    assert "test" in (meta.get("gate_overrides") or {})
    conn.close()


# 16. 多任务节点版本独立性与放行范围界定
def test_scenario_16_multitask_node_version_isolation_and_scope(ops_test_env):
    """Verify nodes with multiple tasks of differing versions can be passed per-task without conflict."""
    store = ops_test_env["store"]
    db_path = ops_test_env["db_path"]
    from herdr import kernel as herdr_kernel
    import sqlite3

    conn = sqlite3.connect(db_path)
    # Seed two tasks under node 'test': Task A at version 3, Task B at version 7
    conn.execute("""
        INSERT INTO tasks (task_id, workflow_id, node, stage, agent, status, stage_verdict, pane_id, version, created_at, updated_at)
        VALUES ('wf-ops-01-test-a', 'wf-ops-01', 'test', 'test', 'auto', 'completed', 'blocked', 'pane-a', 3, 1000, 1000)
    """)
    conn.execute("""
        INSERT INTO tasks (task_id, workflow_id, node, stage, agent, status, stage_verdict, pane_id, version, created_at, updated_at)
        VALUES ('wf-ops-01-test-b', 'wf-ops-01', 'test', 'test', 'auto', 'completed', 'blocked', 'pane-b', 7, 1000, 1000)
    """)
    conn.commit()

    # 1. Target Task A with expected_version=3: must SUCCEED, not fail because Task B is version 7
    res_a = herdr_kernel.force_pass_gate(
        workflow_id="wf-ops-01",
        gate_node_id="test",
        task_id="wf-ops-01-test-a",
        expected_version=3,
        note="放行任务 A",
        store=store,
    )
    assert res_a["ok"] is True
    assert res_a["updated_tasks"] == ["wf-ops-01-test-a"]

    # Verify Task A is pass, Task B is still blocked at version 7
    t_a = store.get_task("wf-ops-01-test-a")
    t_b = store.get_task("wf-ops-01-test-b")
    assert t_a["stage_verdict"] == "pass"
    assert t_b["stage_verdict"] == "blocked"
    assert t_b["version"] == 7

    # 2. Target Task B with expected_version=7: must SUCCEED
    res_b = herdr_kernel.force_pass_gate(
        workflow_id="wf-ops-01",
        gate_node_id="test",
        task_id="wf-ops-01-test-b",
        expected_version=7,
        note="放行任务 B",
        store=store,
    )
    assert res_b["ok"] is True
    assert res_b["updated_tasks"] == ["wf-ops-01-test-b"]
    assert store.get_task("wf-ops-01-test-b")["stage_verdict"] == "pass"

    # 3. Node-level pass without task_id when tasks have different versions: rejected with clear guidance
    # Reset Task A to v3, Task B to v7, both blocked
    conn.execute("UPDATE tasks SET version = 3, stage_verdict = 'blocked' WHERE task_id = 'wf-ops-01-test-a'")
    conn.execute("UPDATE tasks SET version = 7, stage_verdict = 'blocked' WHERE task_id = 'wf-ops-01-test-b'")
    conn.commit()

    with pytest.raises(RuntimeError) as exc:
        herdr_kernel.force_pass_gate(
            workflow_id="wf-ops-01",
            gate_node_id="test",
            expected_version=3,
            note="尝试全局单一版本放行不同版本节点",
            store=store,
        )
    assert "存在多个不同版本的任务" in str(exc.value)

    # 4. Node-level pass with expected_task_versions mapping: succeeds for all tasks
    res_map = herdr_kernel.force_pass_gate(
        workflow_id="wf-ops-01",
        gate_node_id="test",
        expected_task_versions={"wf-ops-01-test-a": 3, "wf-ops-01-test-b": 7},
        note="通过版本映射放行节点所有任务",
        store=store,
    )
    assert res_map["ok"] is True
    assert "wf-ops-01-test-a" in res_map["updated_tasks"]
    assert "wf-ops-01-test-b" in res_map["updated_tasks"]
    conn.close()


# 17. Controller 动作生成快照绑定与服务端强制版本约束
def test_scenario_17_controller_action_snapshot_binding_and_rejection(ops_test_env):
    """Verify Controller actions carry version/pane snapshot and backend rejects unconstrained force_pass."""
    store = ops_test_env["store"]
    from herdr import controller_actions
    from unittest.mock import patch
    from pathlib import Path

    # Ensure task has version and pane
    task = store.get_task("wf-ops-01-reworkable")
    assert task is not None
    assert task.get("version") is not None
    cur_ver = task["version"]
    cur_pane = task.get("pane_id")

    wf = store.get_workflow("wf-ops-01")
    actions = controller_actions.generate_controller_actions(task, wf)

    # 1. Action generator binds expected_version and expected_pane_id
    rework_act = next((a for a in actions if a.category == "rework" and "rework" in a.action_id), None)
    assert rework_act is not None
    assert rework_act.api_payload.get("expected_version") == cur_ver
    assert rework_act.api_payload.get("expected_pane_id") == cur_pane

    pass_act = next((a for a in actions if a.category == "bypass"), None)
    assert pass_act is not None
    assert pass_act.api_payload.get("expected_version") == cur_ver
    assert pass_act.api_payload.get("expected_pane_id") == cur_pane

    # 2. Server-side api_controller_execute_action: missing expected_version when targeting task is REJECTED
    with pytest.raises(RuntimeError) as exc:
        c.api_controller_execute_action({
            "type": "force_pass_advance",
            "workflow_id": "wf-ops-01",
            "task_id": "wf-ops-01-reworkable",
            "gate_node_id": "implementation",
            "confirmed": True,
            "reason": "缺少版本约束的放行",
            # expected_version omitted
        })
    assert "缺少 expected_version" in str(exc.value)

    # 3. Server-side api_controller_execute_action: stale expected_version is REJECTED
    with pytest.raises(RuntimeError) as exc2:
        c.api_controller_execute_action({
            "type": "force_pass_advance",
            "workflow_id": "wf-ops-01",
            "task_id": "wf-ops-01-reworkable",
            "gate_node_id": "implementation",
            "expected_version": cur_ver + 999,
            "confirmed": True,
            "reason": "陈旧版本约束的放行",
        })
    assert "任务版本已变化" in str(exc2.value)

    # 4. Server-side api_controller_execute_action: matching version and pane SUCCEEDS
    with patch.object(c, "manual_advance") as mock_adv:
        mock_adv.return_value = {"ok": True, "advanced": True}
        res = c.api_controller_execute_action({
            "type": "force_pass_advance",
            "workflow_id": "wf-ops-01",
            "task_id": "wf-ops-01-reworkable",
            "gate_node_id": "implementation",
            "expected_version": cur_ver,
            "expected_pane_id": cur_pane,
            "confirmed": True,
            "reason": "快照完整且一致的合法放行",
            "operator": "controller_lead",
        })
        assert res.get("ok") is True
        assert res.get("gate_passed") is True

    # 5. Frontend template source verification: executeControllerAction and forcePassTask bind snapshot
    src = Path("console/herdr_factory_console.py").read_text(encoding="utf-8")
    assert "payload.expected_version=matchedTask.version" in src
    assert "payload.expected_pane_id=matchedTask.pane_id" in src
    assert "expVer=matchedTask.version" in src
