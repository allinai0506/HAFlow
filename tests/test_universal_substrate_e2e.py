#!/usr/bin/env python3
"""Universal Human-Agent Collaborative Substrate End-to-End Integration Suite.

Validates the full 5-Phase substrate loop:
1. Dynamic Meta-Model & DAG Validation (Phase 4): business-research-v1.yaml, inputs, permissions
2. Kernel Control Primitives (Phase 1): pause, resume, step, rollback, force_pass
3. In-flight Steering Mesh (Phase 2): queue_steer, dispatch, halt
4. White-box Projection Engine (Phase 3): telemetry clean, intent, milestones, artifacts
5. Universal Studio & Signoff Chamber (Phase 5): approve, reject, rollback, attention hub
"""

import json
import os
import pytest
from pathlib import Path

from herdr import workflow
from herdr import kernel
from herdr import steering
from herdr import projection
from herdr import mcp
from console import herdr_factory_console as c


HERDR_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = HERDR_ROOT / "workflow_templates" / "business-research-v1.yaml"


@pytest.fixture
def e2e_env(tmp_path, monkeypatch):
    """Setup isolated test environment for substrate E2E."""
    wf_file = tmp_path / "workflows.json"
    tasks_file = tmp_path / "tasks.json"
    steer_file = tmp_path / "steering.json"
    mcp_file = tmp_path / "mcp-registry.json"
    cp_dir = tmp_path / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)

    db_file = tmp_path / "state.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(db_file))
    monkeypatch.setenv("WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setenv("TASKS_FILE", str(tasks_file))
    monkeypatch.setenv("STEERING_FILE", str(steer_file))
    monkeypatch.setenv("HERDR_MCP_REGISTRY", str(mcp_file))
    monkeypatch.setenv("CHECKPOINTS_DIR", str(cp_dir))

    monkeypatch.setattr(c, "WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setattr(c, "TASKS_FILE", str(tasks_file))
    from unittest.mock import MagicMock
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: MagicMock(returncode=0, stdout="", stderr=""))

    wf_file.write_text(json.dumps({"workflows": {}}), encoding="utf-8")
    tasks_file.write_text(json.dumps({"tasks": []}), encoding="utf-8")
    steer_file.write_text(json.dumps({}), encoding="utf-8")

    return {
        "tmp_path": tmp_path,
        "wf_file": wf_file,
        "tasks_file": tasks_file,
        "steer_file": steer_file,
        "mcp_file": mcp_file,
    }


class TestUniversalSubstrateEndToEnd:

    def test_e2e_full_business_research_lifecycle_approve(self, e2e_env):
        """Full lifecycle: Template -> Init -> Pause/Resume -> Steer/Halt -> Telemetry -> Signoff Approve."""
        # 1. Phase 4 Meta-model: Load & Validate Template
        assert TEMPLATE_PATH.exists(), f"Missing template {TEMPLATE_PATH}"
        raw_tmpl = workflow.load_template("business-research-v1")
        assert raw_tmpl is not None
        assert raw_tmpl["name"] == "business-research-v1"

        norm_tmpl = workflow.normalize_workflow(raw_tmpl)
        workflow.validate_workflow_dag(norm_tmpl["nodes"])

        nodes = norm_tmpl["nodes"]
        assert len(nodes) == 4
        node_ids = [n["id"] for n in nodes]
        assert node_ids == ["market_scope", "data_extraction", "comparative_analysis", "executive_briefing"]

        # 2. Seed Workflow in persistent storage
        wid = "wf-biz-research-dogfood-01"
        wf_data = {
            "workflow_id": wid,
            "title": "2026Q3 跨国竞品财报研报协同",
            "status": "running",
            "template_name": "business-research-v1",
            "config": norm_tmpl,
            "created_at": 1773479000,
        }
        workflows_db = {"workflows": {wid: wf_data}}
        e2e_env["wf_file"].write_text(json.dumps(workflows_db), encoding="utf-8")

        # 3. Phase 1 Kernel Primitives: Pause and Resume
        res_pause = c.api_kernel_pause({"workflow_id": wid, "reason": "例行风控抽查"})
        assert res_pause.get("ok") is True
        wf_paused = json.loads(e2e_env["wf_file"].read_text(encoding="utf-8"))["workflows"][wid]
        assert wf_paused["status"] == "paused"

        res_resume = c.api_kernel_resume({"workflow_id": wid})
        assert res_resume.get("ok") is True
        wf_resumed = json.loads(e2e_env["wf_file"].read_text(encoding="utf-8"))["workflows"][wid]
        assert wf_resumed["status"] == "running"

        # 4. Node 1: market_scope execution
        task_scope = {
            "task_id": "task-scope-001",
            "workflow_id": wid,
            "node": "market_scope",
            "stage": "market_scope",
            "status": "working",
            "pane_id": "pane-mock-scope",
            "goal": "界定分析目标与竞品范围",
            "started_at": 1773479010,
        }
        tasks_db = json.loads(e2e_env["tasks_file"].read_text(encoding="utf-8"))
        tasks_db["tasks"].append(task_scope)
        kernel.save_tasks_data(tasks_db)

        # Complete Node 1
        tasks_db["tasks"][0]["status"] = "completed"
        tasks_db["tasks"][0]["stage_verdict"] = "pass"
        kernel.save_tasks_data(tasks_db)

        # 5. Node 2: data_extraction with Phase 2 Steering & Halt
        task_data = {
            "task_id": "task-data-002",
            "workflow_id": wid,
            "node": "data_extraction",
            "stage": "data_extraction",
            "status": "working",
            "pane_id": "pane-mock-data",
            "agent": "codex",
            "goal": "抓取公开财务数据与物流附注",
            "started_at": 1773479050,
        }
        tasks_db["tasks"].append(task_data)
        kernel.save_tasks_data(tasks_db)

        # Verify Node 2 MCP capability resolution
        node_def = next(n for n in nodes if n["id"] == "data_extraction")
        mounted_mcp = mcp.resolve_node_mcp(node_def)
        mounted_names = [s["name"] for s in mounted_mcp]
        assert "web_search" in mounted_names or "data_extraction" in mounted_names
        perm_read_ok = mcp.check_node_permissions(node_def, "read")
        assert perm_read_ok is True
        perm_write_blocked = mcp.check_node_permissions(node_def, "write")
        assert perm_write_blocked is False

        # Phase 2: In-Flight Steer Queueing (Non-urgent -> Pending -> Dispatched at boundary)
        steer_res = c.api_task_steer({
            "task_id": "task-data-002",
            "instruction": "请格外关注北美区物流折旧政策变动",
            "urgent": False,
            "author": "ChiefAnalyst",
        })
        assert steer_res.get("ok") is True
        assert steer_res.get("steer_id") is not None

        # Verify steer queue state contains pending item
        q_res = c.api_task_steer_queue("task-data-002")
        assert isinstance(q_res, list)
        assert len(q_res) >= 1
        assert "物流折旧政策" in q_res[0]["instruction"]
        assert q_res[0]["status"] == "pending"

        # Simulate agent turn boundary: consume queued steer
        consumed = steering.dispatch_pending_steer("task-data-002")
        assert consumed is not None
        assert consumed.get("ok") is True
        assert consumed["status"] == "dispatched"

        # Verify steer is recorded in task steering history
        tasks_db = json.loads(e2e_env["tasks_file"].read_text(encoding="utf-8"))
        t_data_now = next(t for t in tasks_db["tasks"] if t["task_id"] == "task-data-002")
        assert len(t_data_now.get("steering_history", [])) >= 1
        assert "物流折旧政策" in t_data_now["steering_history"][0]["instruction"]

        # Phase 2: Emergency Halt
        halt_res = c.api_task_halt({"task_id": "task-data-002", "reason": "发现爬虫频次预警，人工紧急制动"})
        assert halt_res.get("ok") is True

        # Verify status became interrupted
        tasks_db = json.loads(e2e_env["tasks_file"].read_text(encoding="utf-8"))
        t_data_now = next(t for t in tasks_db["tasks"] if t["task_id"] == "task-data-002")
        assert t_data_now["status"] == "interrupted"

        # Resume and complete data_extraction
        t_data_now["status"] = "completed"
        t_data_now["stage_verdict"] = "pass"
        kernel.save_tasks_data(tasks_db)

        # 6. Node 3: comparative_analysis with Phase 3 Telemetry & Projection
        task_comp = {
            "task_id": "task-comp-003",
            "workflow_id": wid,
            "node": "comparative_analysis",
            "stage": "comparative_analysis",
            "status": "working",
            "pane_id": "pane-mock-comp",
            "goal": "产出横向毛利比对与供应链风险报告",
            "started_at": 1773479100,
            "trajectory": [
                "\x1b[32m[HERDR_INTENT]\x1b[0m 正在交叉核验三家竞品在亚太区的供应链成本",
                "[STEP 2] 已完成毛利率归因分解",
                "生成产物 docs/comparative_report.md (EVALUATION 94分)",
            ],
        }
        tasks_db["tasks"].append(task_comp)
        kernel.save_tasks_data(tasks_db)

        # Validate Phase 3 Projection
        p_data = c.api_task_projection("task-comp-003")
        assert "横向毛利比对" in p_data["intent"]
        assert len(p_data["milestones"]) > 0

        # Also verify explicit [HERDR_INTENT] extraction with ANSI terminal text
        term_sample = "\x1b[32m[HERDR_INTENT]\x1b[0m 正在交叉核验三家竞品在亚太区的供应链成本"
        direct_intent = projection.extract_task_intent(task_comp, term_sample)
        assert "亚太区的供应链成本" in direct_intent

        # Complete Node 3
        tasks_db["tasks"][-1]["status"] = "completed"
        tasks_db["tasks"][-1]["stage_verdict"] = "pass"
        kernel.save_tasks_data(tasks_db)

        # 7. Node 4: executive_briefing (Gate Blocked -> Signoff Chamber Approve)
        task_gate = {
            "task_id": "task-gate-004",
            "workflow_id": wid,
            "node": "executive_briefing",
            "stage": "executive_briefing",
            "status": "blocked",
            "stage_verdict": "blocked",
            "pane_id": "pane-mock-gate",
            "goal": "高管决策简报会签",
            "started_at": 1773479200,
        }
        tasks_db["tasks"].append(task_gate)
        kernel.save_tasks_data(tasks_db)

        # Check Attention Hub aggregation before signoff
        wp = c.api_workflow_projection(wid)
        assert wp["progress"]["total_tasks"] == 4
        assert wp["progress"]["blocked_tasks"] == 1

        # Phase 5: Signoff Chamber Approve Action
        signoff_res = c.api_task_signoff({
            "task_id": "task-gate-004",
            "action": "approve",
            "feedback": "商业洞察深刻，财务核算准确，准予交付！",
            "operator": "CEO",
        })
        assert signoff_res.get("ok") is True
        assert signoff_res.get("action") == "approve"

        # Verify Gate task status after approval
        tasks_db = json.loads(e2e_env["tasks_file"].read_text(encoding="utf-8"))
        t_gate_after = next(t for t in tasks_db["tasks"] if t["task_id"] == "task-gate-004")
        assert t_gate_after["stage_verdict"] == "pass"
        assert "准予交付" in t_gate_after["stage_verdict_note"]

    def test_e2e_business_research_reject_and_rollback_loop(self, e2e_env):
        """Verify Gate rejection triggers graceful rollback to upstream node with feedback recorded."""
        wid = "wf-biz-research-reject-02"
        raw_tmpl = workflow.load_template("business-research-v1")
        norm_tmpl = workflow.normalize_workflow(raw_tmpl)

        wf_data = {
            "workflow_id": wid,
            "title": "商业研报会签驳回与返工循环测试",
            "status": "running",
            "config": norm_tmpl,
        }
        e2e_env["wf_file"].write_text(json.dumps({"workflows": {wid: wf_data}}), encoding="utf-8")

        # Seed completed upstream and blocked gate
        tasks_data = {
            "tasks": [
                {
                    "task_id": "t-data-upstream",
                    "workflow_id": wid,
                    "node": "data_extraction",
                    "stage": "data_extraction",
                    "status": "completed",
                    "stage_verdict": "pass",
                },
                {
                    "task_id": "t-comp-midstream",
                    "workflow_id": wid,
                    "node": "comparative_analysis",
                    "stage": "comparative_analysis",
                    "status": "completed",
                    "stage_verdict": "pass",
                },
                {
                    "task_id": "t-gate-blocked",
                    "workflow_id": wid,
                    "node": "executive_briefing",
                    "stage": "executive_briefing",
                    "status": "blocked",
                    "stage_verdict": "blocked",
                },
            ]
        }
        e2e_env["tasks_file"].write_text(json.dumps(tasks_data), encoding="utf-8")

        # Phase 5 Signoff Reject -> Targets data_extraction
        signoff_reject = c.api_task_signoff({
            "task_id": "t-gate-blocked",
            "action": "reject",
            "retry_target": "data_extraction",
            "feedback": "海外分部收入拆解数据口径有误，必须重新抓取 Q3 附注！",
            "operator": "InvestmentVP",
        })
        assert signoff_reject.get("ok") is True
        assert signoff_reject.get("action") == "reject"
        assert signoff_reject.get("target_node") == "data_extraction"
        assert "重新抓取" in signoff_reject.get("feedback", "")

        # Verify rollback marked downstream as superseded
        tasks_db = json.loads(e2e_env["tasks_file"].read_text(encoding="utf-8"))
        t_gate = next(t for t in tasks_db["tasks"] if t["task_id"] == "t-gate-blocked")
        t_comp = next(t for t in tasks_db["tasks"] if t["task_id"] == "t-comp-midstream")
        assert t_gate["status"] == "superseded"
        assert t_comp["status"] == "superseded"

    def test_e2e_attention_hub_and_filter_telemetry(self, e2e_env):
        """Verify Attention Hub categorizes decisions, alerts, and active nodes with high SNR."""
        wid = "wf-attention-test-03"
        e2e_env["wf_file"].write_text(json.dumps({"workflows": {wid: {"workflow_id": wid, "status": "running"}}}), encoding="utf-8")

        tasks_db = {
            "tasks": [
                {"task_id": "t1", "workflow_id": wid, "status": "blocked", "node": "gate_review"},
                {"task_id": "t2", "workflow_id": wid, "status": "interrupted", "node": "agent_halted"},
                {"task_id": "t3", "workflow_id": wid, "status": "failed", "node": "agent_crash"},
                {"task_id": "t4", "workflow_id": wid, "status": "working", "node": "agent_active"},
                {"task_id": "t5", "workflow_id": wid, "status": "completed", "node": "agent_done"},
            ]
        }
        e2e_env["tasks_file"].write_text(json.dumps(tasks_db), encoding="utf-8")

        wp = c.api_workflow_projection(wid)
        assert wp["progress"]["total_tasks"] == 5
        assert wp["progress"]["blocked_tasks"] == 1
        assert wp["progress"]["completed_tasks"] == 1

        # Check Attention Hub frontend categorization contract
        ts = tasks_db["tasks"]
        decision_tasks = [t for t in ts if t.get("stage_verdict") == "blocked" or t.get("status") == "blocked"]
        attention_tasks = [t for t in ts if t.get("status") in {"failed", "interrupted"} or t.get("blocker")]
        active_tasks = [t for t in ts if t.get("status") in {"dispatched", "working", "rework", "paused"}]

        assert len(decision_tasks) == 1
        assert len(attention_tasks) == 2
        assert len(active_tasks) == 1

    def test_e2e_checkpoint_lifecycle_and_restoration(self, e2e_env):
        """Verify Kernel Checkpoint creation, listing, state mutation, and restoration."""
        wid = "wf-checkpoint-e2e-04"
        e2e_env["wf_file"].write_text(json.dumps({
            "workflows": {
                wid: {
                    "workflow_id": wid,
                    "title": "检查点回溯与时间旅行测试",
                    "status": "running",
                    "config": {"nodes": [{"id": "init", "label": "初始步骤"}]},
                }
            }
        }), encoding="utf-8")

        # 1. Create checkpoint at golden state
        cp_res = c.api_kernel_checkpoint_create({"workflow_id": wid, "tag": "golden_snapshot_v1"})
        assert cp_res.get("ok") is True
        cpid = cp_res["checkpoint_id"]

        # 2. List checkpoints
        cp_list = c.api_kernel_checkpoint_list(wid)
        assert len(cp_list) == 1
        assert cp_list[0]["checkpoint_id"] == cpid
        assert cp_list[0]["tag"] == "golden_snapshot_v1"

        # 3. Simulate workflow disaster mutation
        wf_db = json.loads(e2e_env["wf_file"].read_text(encoding="utf-8"))
        wf_db["workflows"][wid]["status"] = "failed"
        wf_db["workflows"][wid]["title"] = "崩溃损坏的状态"
        e2e_env["wf_file"].write_text(json.dumps(wf_db), encoding="utf-8")

        # 4. Time-travel restore from checkpoint
        restore_res = c.api_kernel_checkpoint_restore({"workflow_id": wid, "checkpoint_id": cpid})
        assert restore_res.get("ok") is True
        assert restore_res.get("status") == "running"

        # Verify restored DB state
        restored_wf = json.loads(e2e_env["wf_file"].read_text(encoding="utf-8"))["workflows"][wid]
        assert restored_wf["status"] == "running"
        assert restored_wf["title"] == "检查点回溯与时间旅行测试"
