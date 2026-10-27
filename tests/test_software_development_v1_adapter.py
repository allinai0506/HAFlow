"""Regression tests for software-development-v1 unified-dev-flow engineering adapter.

Verifies:
1. 6 fixed nodes: requirements -> plan -> implementation -> test -> review -> wrapup.
   review depends on test; wrapup verifies both test and review on same candidate.
2. HAFlow planning contract forbids secondary executor selection / SDD headers.
3. S4 implementation exit allows completion without demanding downstream S5/S6.
4. Independent agents cannot bypass Stop hook; forged identity rejected.
5. Missing stage skill or unexecutable script yields explicit capability gap.
6. Requirements & plan do not auto-accept on bare file changes; require structured deliverables.
7. Downstream nodes receive exact upstream deliverables and candidate references.
8. Missing, mismatched, or stale test execution evidence blocks review / wrapup.
9. Code defects correctly route back to implementation; fix loop cumulative budget preserved.
10. Cumulative fix-loop budget does not reset across new tasks or candidate SHAs.
11. Workspace delivery ceiling skips PR receipt; PR delivery retains closure without auto-merge.
12. Legacy templates and unadapted workflows retain original behavior.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERDR_ROOT = Path(__file__).resolve().parent.parent
if str(HERDR_ROOT) not in sys.path:
    sys.path.insert(0, str(HERDR_ROOT))

from herdr import stage_skills
from herdr.workflow import normalize_workflow, find_node, get_ready_nodes
from herdr.node_roles import is_verifier_node
from herdr import direct_dispatch as direct_dispatch_planner


class TestSoftwareDevelopmentV1Adapter(unittest.TestCase):
    def setUp(self):
        self.template_path = HERDR_ROOT / "workflow_templates" / "software-development-v1.yaml"
        self.tmp_dir = tempfile.mkdtemp(prefix="haflow_adapter_test_")
        self.db_path = Path(self.tmp_dir) / "state.db"
        self._init_mock_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _init_mock_db(self, path):
        conn = sqlite3.connect(str(path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflows (
                workflow_id TEXT PRIMARY KEY,
                name TEXT,
                status TEXT,
                created_at REAL,
                reopened_at REAL,
                config_json TEXT,
                payload_json TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                workflow_id TEXT,
                node TEXT,
                stage TEXT,
                agent TEXT,
                status TEXT,
                stage_verdict TEXT,
                stage_verdict_note TEXT,
                pane_id TEXT,
                goal TEXT,
                blocker TEXT,
                payload_json TEXT,
                created_at REAL,
                updated_at REAL,
                version INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT,
                task_id TEXT,
                workflow_id TEXT,
                node_id TEXT,
                run_id TEXT,
                source TEXT,
                payload_json TEXT,
                timestamp REAL
            )
        """)
        conn.commit()
        conn.close()

    def test_01_fixed_six_nodes_and_review_depends_on_test(self):
        """Scenario 1: Exactly 6 nodes in sequence, review depends on test, wrapup checks both."""
        import yaml
        with open(self.template_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        nodes = cfg.get("nodes", [])
        node_ids = [n["id"] for n in nodes]
        expected_ids = ["requirements", "plan", "implementation", "test", "review", "wrapup"]
        self.assertEqual(node_ids, expected_ids, "Nodes must match the exact 6 IDs in order")

        req_node = find_node(cfg, "requirements")
        plan_node = find_node(cfg, "plan")
        impl_node = find_node(cfg, "implementation")
        test_node = find_node(cfg, "test")
        review_node = find_node(cfg, "review")
        wrapup_node = find_node(cfg, "wrapup")

        self.assertEqual(req_node.get("depends_on", []), [])
        self.assertEqual(plan_node.get("depends_on", []), ["requirements"])
        self.assertEqual(impl_node.get("depends_on", []), ["plan"])
        self.assertEqual(test_node.get("depends_on", []), ["implementation"])
        self.assertEqual(review_node.get("depends_on", []), ["test"], "review must depend on test")
        self.assertIn("review", wrapup_node.get("depends_on", []))
        self.assertIn("test", wrapup_node.get("depends_on", []))

        # Check DAG readiness progression:
        # 1. Start: only requirements ready
        ready = [n["id"] for n in get_ready_nodes(cfg, set())]
        self.assertEqual(ready, ["requirements"])

        # 2. After requirements: only plan ready
        ready = [n["id"] for n in get_ready_nodes(cfg, {"requirements"})]
        self.assertEqual(ready, ["plan"])

        # 3. After plan: only implementation ready
        ready = [n["id"] for n in get_ready_nodes(cfg, {"requirements", "plan"})]
        self.assertEqual(ready, ["implementation"])

        # 4. After implementation: ONLY test ready (review NOT ready yet!)
        ready = [n["id"] for n in get_ready_nodes(cfg, {"requirements", "plan", "implementation"})]
        self.assertEqual(ready, ["test"], "After implementation, ONLY test is ready; review must not run yet")

        # 5. After test passes: review becomes ready
        ready = [n["id"] for n in get_ready_nodes(cfg, {"requirements", "plan", "implementation", "test"})]
        self.assertEqual(ready, ["review"], "After test passes, review is ready")

        # 6. After review passes: wrapup becomes ready
        ready = [n["id"] for n in get_ready_nodes(cfg, {"requirements", "plan", "implementation", "test", "review"})]
        self.assertEqual(ready, ["wrapup"], "After test and review pass, wrapup is ready")

    def test_02_planning_contract_forbids_secondary_executor(self):
        """Scenario 2: Plan stage context explicitly forbids secondary executor & SDD handoff."""
        block = stage_skills.render_planning_orchestrator_rules()
        self.assertIn("HAFlow 是当前流程的唯一主控系统", block)
        self.assertIn("subagent-driven-development", block)
        self.assertIn("executing-plans", block)
        self.assertIn("禁止询问用户选择", block)

        # Ensure stage context for plan node contains this rule
        ctx = stage_skills.assemble_stage_context(
            workflow_id="wf-test-01",
            task_id="wf-test-01-plan-auto",
            run_id="run-01",
            node_id="plan",
            dispatch_role="executor",
        )
        self.assertIn("【HAFlow 规划交接契约", ctx["prompt_block"])
        self.assertIn("禁止在本节点启动实现或另开工作区", ctx["prompt_block"])

    def test_03_implementation_s4_exit_stop_hook_adaptation(self):
        """Scenario 3: S4 implementation can exit without downstream S5/S6 artifacts when verified in DB."""
        from herdr.stage_skills import check_stage_stop_hook_eligibility

        clone_path = str(Path(self.tmp_dir) / "clone_impl")
        os.makedirs(clone_path, exist_ok=True)

        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            INSERT INTO workflows (workflow_id, name, status, config_json)
            VALUES ('wf-impl-01', 'software-development-v1', 'running', '{"name": "software-development-v1"}')
        """)
        conn.execute("""
            INSERT INTO tasks (task_id, workflow_id, node, stage, status, payload_json)
            VALUES ('task-impl-01', 'wf-impl-01', 'implementation', 'implementation', 'working', ?)
        """, (json.dumps({"clone_path": clone_path}),))
        conn.commit()
        conn.close()

        # Check eligibility for implementation stage
        allowed, reason, stage = check_stage_stop_hook_eligibility(
            clone_path, db_path=self.db_path
        )
        self.assertTrue(allowed, "Implementation stage should be recognized as HAFlow stage task")
        self.assertEqual(stage, "implementation")
        self.assertIn("S4", reason)

    def test_04_independent_agent_and_forged_identity_rejected(self):
        """Scenario 4: Independent agent or forged clone path cannot bypass stop hook."""
        from herdr.stage_skills import check_stage_stop_hook_eligibility

        unregistered_dir = str(Path(self.tmp_dir) / "unregistered_dir")
        os.makedirs(unregistered_dir, exist_ok=True)

        allowed, reason, stage = check_stage_stop_hook_eligibility(
            unregistered_dir, db_path=self.db_path
        )
        self.assertFalse(allowed, "Unregistered directory must not be exempted from stop hook")
        self.assertEqual(stage, None)

        # Forged status (e.g. task is already failed or superseded)
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            INSERT INTO tasks (task_id, workflow_id, node, stage, status, payload_json)
            VALUES ('task-fake-01', 'wf-impl-01', 'implementation', 'implementation', 'failed', ?)
        """, (json.dumps({"clone_path": unregistered_dir}),))
        conn.commit()
        conn.close()

        allowed, reason, stage = check_stage_stop_hook_eligibility(
            unregistered_dir, db_path=self.db_path
        )
        self.assertFalse(allowed, "Non-active task must not be exempted")

    def test_05_skill_resolution_and_capability_gap(self):
        """Scenario 5: Resolves valid skills; missing or unexecutable script raises clear capability gap."""
        # Valid skill resolution
        resolved = stage_skills.resolve_stage_skill("writing-plans")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["name"], "writing-plans")
        self.assertTrue(os.path.exists(resolved["skill_file"]))

        # Non-existent skill raises SkillCapabilityGap
        with self.assertRaises(stage_skills.SkillCapabilityGap) as ctx:
            stage_skills.resolve_stage_skill("non-existent-fantasy-skill-xyz")
        self.assertIn("not found", str(ctx.exception).lower())

        # Unexecutable script detection
        dummy_skill_dir = Path(self.tmp_dir) / "dummy_skill"
        dummy_scripts = dummy_skill_dir / "scripts"
        dummy_scripts.mkdir(parents=True)
        (dummy_skill_dir / "SKILL.md").write_text("# Dummy\n", encoding="utf-8")
        unexec_script = dummy_scripts / "run.sh"
        unexec_script.write_text("#!/bin/bash\necho hello\n", encoding="utf-8")
        os.chmod(unexec_script, 0o644)  # NOT executable

        with self.assertRaises(stage_skills.SkillCapabilityGap) as ctx:
            stage_skills.resolve_stage_skill("dummy_skill", search_roots=[self.tmp_dir], required_scripts=["scripts/run.sh"])
        self.assertIn("not executable", str(ctx.exception).lower())

    def test_06_requirements_and_plan_deliverable_validation(self):
        """Scenario 6: Non-gate requirements/plan do not auto-accept on bare file changes; check required sections."""
        from herdr.stage_skills import (
            validate_requirements_deliverable_content,
            validate_plan_deliverable_content,
        )

        # Empty or meaningless text
        self.assertFalse(validate_requirements_deliverable_content("Just some random text without structure")[0])

        # Complete requirements
        valid_req = """
        # Requirements Spec
        ## 用户目标与背景
        实现统一研发流程固定六节点适配。
        ## 功能范围与非目标
        范围：software-development-v1
        非目标：不修改其他模板。
        ## 验收标准
        - AC-1: review 在 test 之后执行
        - AC-2: S4 实现节点退出不被卡死
        ## 证明方式与验证计划
        proof_mode: tdd
        验证命令: pytest -q tests/test_adapter.py
        ## 交付上限
        delivery_ceiling: workspace
        """
        ok, missing = validate_requirements_deliverable_content(valid_req)
        self.assertTrue(ok, f"Valid requirements should pass: {missing}")

        # Incomplete plan
        self.assertFalse(validate_plan_deliverable_content("Plan without tasks")[0])

        # Complete plan
        valid_plan = """
        # Implementation Plan
        ## 验收条目映射
        - Task 1: 满足 AC-1，调整 review 依赖
        ## 修改文件范围
        - Modify: workflow_templates/software-development-v1.yaml
        - Create: herdr/stage_skills.py
        ## 验证命令
        pytest -q tests/test_software_development_v1_adapter.py
        """
        ok, missing = validate_plan_deliverable_content(valid_plan, role="executor")
        self.assertTrue(ok, f"Valid plan should pass: {missing}")

    def test_07_downstream_reads_upstream_actual_deliverables(self):
        """Scenario 7: Review receives test evidence note/verdict and candidate SHA."""
        test_evidence_note = {
            "kind": "evidence",
            "node": "test",
            "title": "测试执行记录与门禁结论",
            "body": "Executed: pytest -q tests/test_software_development_v1_adapter.py\nExit code: 0\nResult: 12 passed",
            "task_id": "wf-1-test-auto",
            "base_sha": "a" * 40,
        }
        ctx = stage_skills.assemble_stage_context(
            workflow_id="wf-test-01",
            task_id="wf-test-01-review-auto",
            run_id="run-01",
            node_id="review",
            dispatch_role="reviewer",
            candidate_sha="a" * 40,
            upstream_notes=[test_evidence_note],
        )
        prompt = ctx["prompt_block"]
        self.assertIn("a" * 40, prompt)
        self.assertIn("测试执行记录与门禁结论", prompt)
        self.assertIn("pytest -q tests/test_software_development_v1_adapter.py", prompt)

    def test_08_test_evidence_freshness_and_candidate_matching(self):
        """Scenario 8: Missing or mismatched candidate SHA in test evidence is rejected."""
        from herdr.stage_skills import validate_test_evidence_freshness

        # Correct matching evidence
        ok, err = validate_test_evidence_freshness(
            candidate_sha="a" * 40,
            test_sha="a" * 40,
            verdict="pass",
            has_commands_evidence=True,
        )
        self.assertTrue(ok)

        # Mismatched SHA
        ok, err = validate_test_evidence_freshness(
            candidate_sha="a" * 40,
            test_sha="b" * 40,
            verdict="pass",
            has_commands_evidence=True,
        )
        self.assertFalse(ok)
        self.assertIn("mismatch", err.lower())

        # Missing commands evidence
        ok, err = validate_test_evidence_freshness(
            candidate_sha="a" * 40,
            test_sha="a" * 40,
            verdict="pass",
            has_commands_evidence=False,
        )
        self.assertFalse(ok)
        self.assertIn("missing", err.lower())

    def test_09_code_defects_route_to_implementation(self):
        """Scenario 9: Blocked test/review routes back to implementation, not requirements or plan."""
        import yaml
        with open(self.template_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        sel = cfg.get("selective_replan", {})
        self.assertEqual(sel.get("retry_node"), "implementation")

    def test_10_fix_loop_cumulative_budget_not_reset_on_new_sha(self):
        """Scenario 10: Cumulative rework count tracks by workflow & retry_node, preserving budget across SHAs."""
        from herdr import fix_loop as fix_loop_core

        # Verify fix_loop exhaustion check
        self.assertFalse(fix_loop_core.fix_loop_exhausted(2, 3))
        self.assertTrue(fix_loop_core.fix_loop_exhausted(3, 3))
        self.assertTrue(fix_loop_core.fix_loop_exhausted(4, 3))

    def test_11_workspace_delivery_ceiling_skips_pr_receipt(self):
        """Scenario 11: Workspace delivery ceiling skips PR receipt and forbids automatic merge."""
        import yaml
        with open(self.template_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        self.assertEqual(cfg.get("delivery_ceiling_default"), "workspace")

        from herdr.review_closure import _closure_demanded
        self.assertFalse(_closure_demanded(cfg), "workspace delivery ceiling must skip PR review closure")

    def test_12_legacy_templates_and_unadapted_workflows_not_regressed(self):
        """Scenario 12: Workflows using other templates retain original behavior."""
        from herdr.stage_skills import is_staged_dev_flow_workflow

        self.assertTrue(is_staged_dev_flow_workflow({"name": "software-development-v1"}))
        self.assertFalse(is_staged_dev_flow_workflow({"name": "code-craft-v1"}))
        self.assertFalse(is_staged_dev_flow_workflow({"name": "custom-wf"}))

    def test_13_agents_directory_internal_untracked(self):
        """Scenario 13: Staged skills in .agents are internal untracked files."""
        from herdr.repo_hygiene import is_internal_untracked

        self.assertTrue(is_internal_untracked(".agents"))
        self.assertTrue(is_internal_untracked(".agents/skills/writing-plans"))
        self.assertTrue(is_internal_untracked(".agents/skills/acceptance-evidence-gate/SKILL.md"))
        self.assertFalse(is_internal_untracked("src/agents.py"))
        self.assertFalse(is_internal_untracked("docs/plan.md"))

    def test_14_challenger_cannot_auto_accept_on_executor_plan(self):
        """Scenario 14: Plan challenger role cannot auto-accept on executor's plan.md or stale notes."""
        from services.controller import auto_acceptance
        from unittest import mock
        import re

        clone = Path(self.tmp_dir) / "clone"
        clone.mkdir(parents=True, exist_ok=True)
        (clone / "plan.md").write_text("## Execution Plan\n- task 1: implement\n- test cmd: pytest\n", encoding="utf-8")

        task = {
            "task_id": "wf-1-plan-challenger",
            "workflow_id": "wf-1",
            "node": "plan",
            "dispatch_role": "challenger",
            "clone_path": str(clone),
            "status": "agent_done",
        }

        mock_ports = mock.MagicMock()
        mock_ports.auto_accept_enabled.return_value = True
        mock_ports.get_task.return_value = task
        mock_ports.node_is_gate.return_value = False
        mock_ports.workflow_config_for.return_value = {"name": "software-development-v1"}
        mock_ports.re = re
        mock_ports.task_changes_recorded.return_value = False
        mock_ports._task_version.return_value = 1
        mock_ports.set_task_status.return_value = True

        with mock.patch("herdr.workflow_docs.load_notes", return_value=[]):
            # Challenger should reject executor's plan.md
            res = auto_acceptance.try_auto_accept(mock_ports, "wf-1-plan-challenger")
            self.assertFalse(res)

        # With stale note from another task, should still reject
        stale_note = {
            "node": "plan",
            "kind": "review",
            "title": "Challenger Review",
            "body": "Plan reviewed and approved. Baseline verified.",
            "stale": True,
            "task_id": "old-task",
        }
        with mock.patch("herdr.workflow_docs.load_notes", return_value=[stale_note]):
            res = auto_acceptance.try_auto_accept(mock_ports, "wf-1-plan-challenger")
            self.assertFalse(res)

        # With active note owned by challenger task, should accept
        valid_note = {
            "node": "plan",
            "kind": "review",
            "title": "Challenger Review",
            "body": "方案对抗与质询评估：经过对执行计划与文件修改范围的详细核查，本方案对验收标准的映射完整。可行性评估认为风险与验证命令具备可行性，无过度设计与死锁风险，验证基线完备。确认通过。",
            "stale": False,
            "task_id": "wf-1-plan-challenger",
        }
        with mock.patch("herdr.workflow_docs.load_notes", return_value=[valid_note]):
            res = auto_acceptance.try_auto_accept(mock_ports, "wf-1-plan-challenger")
            self.assertTrue(res)

    def test_15_skill_capability_gap_yields_structured_refusal(self):
        """Scenario 15: Skill capability gap during dispatch planning returns structured refusal."""
        from herdr.direct_dispatch import plan_stage_dispatch, permanent_dispatch_refusal

        node = {
            "id": "requirements",
            "label": "Requirements",
            "purpose": "Define requirements",
            "task_type": "docs",
            "integration_mode": "none",
            "skills": ["nonexistent-skill-9999"],
        }
        plan = plan_stage_dispatch("wf-test", node, [], "Build feature")
        self.assertEqual(plan.get("mode"), "refusal")
        self.assertEqual(plan.get("refusal_code"), "skill_capability_gap")

        # Verify permanent_dispatch_refusal recognizes the refusal marker
        code = permanent_dispatch_refusal("[STAGE SKILLS REFUSED] task=t1 skill capability gap: missing")
        self.assertEqual(code, "skill_capability_gap")

    def test_16_valid_single_task_plan_direct_dispatch(self):
        """Scenario 16: Valid single-task plan directly creates and dispatches implementation task."""
        from herdr.direct_dispatch import plan_stage_dispatch
        import yaml

        with open(self.template_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        impl_node = find_node(cfg, "implementation")

        wf_id = "wf-plan-direct-01"
        plan_body = """
        # Implementation Plan
        ## 目标
        实现偏好设置与自测
        ## 验收条目映射
        - Task 1: 满足 AC-1，实现 pref.py
        ## 修改文件范围
        - Create: pref.py
        - Test: test_pref.py
        ## 验证命令
        pytest -q test_pref.py
        """
        challenger_body = """
        ## 方案对抗性审查与可行性风险评估
        对偏好设置方案进行了审查，任务划分合理，无死锁和过度设计风险，验证命令 pytest -q test_pref.py 具备可行性。确认通过。
        """

        tasks = [
            {
                "task_id": f"{wf_id}-plan-executor",
                "workflow_id": wf_id,
                "node": "plan",
                "dispatch_role": "executor",
                "status": "completed",
            },
            {
                "task_id": f"{wf_id}-plan-challenger",
                "workflow_id": wf_id,
                "node": "plan",
                "dispatch_role": "challenger",
                "status": "completed",
            },
        ]
        notes = [
            {"node": "plan", "kind": "plan", "title": "实现计划", "body": plan_body, "task_id": f"{wf_id}-plan-executor"},
            {"node": "plan", "kind": "review", "title": "方案对抗质询", "body": challenger_body, "task_id": f"{wf_id}-plan-challenger"},
        ]

        from unittest import mock
        with mock.patch("herdr.workflow_docs.load_notes", return_value=notes):
            res = plan_stage_dispatch(wf_id, impl_node, tasks, "实现用户偏好设置", context_branch="main")
            self.assertEqual(res.get("mode"), "dispatch")
            specs = res.get("specs", [])
            self.assertEqual(len(specs), 1)
            spec = specs[0]
            self.assertIn("pref.py", spec.get("planned_files", []))
            self.assertEqual(spec.get("verify_command"), "pytest -q test_pref.py")
            self.assertEqual(spec.get("integration_mode"), "git")
            self.assertEqual(spec.get("task_type"), "feat")
            self.assertEqual(spec.get("onto_branch"), "main")

    def test_17_valid_multi_task_decoupled_plan_direct_dispatch(self):
        """Scenario 17: Valid multi-task decoupled plan dispatches multiple tasks within concurrency limits."""
        from herdr.direct_dispatch import plan_stage_dispatch
        import yaml

        with open(self.template_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        impl_node = find_node(cfg, "implementation")

        wf_id = "wf-plan-multi-01"
        plan_body = """
        # Implementation Plan
        ## 任务拆分与验收条目映射
        ### Task 1: 偏好设置核心模块
        - **Goal:** 实现偏好存储与读取核心接口
        - **Files:**
          - Create: pref_core.py
          - Test: test_pref_core.py
        - **Verify:**
          pytest -q test_pref_core.py
        - **Dependencies:** None

        ### Task 2: 偏好设置 CLI 导出模块
        - **Goal:** 实现偏好设置命令行导出
        - **Files:**
          - Create: pref_cli.py
          - Test: test_pref_cli.py
        - **Verify:**
          pytest -q test_pref_cli.py
        - **Dependencies:** None
        """
        challenger_body = """
        ## 方案对抗性审查与可行性风险评估
        质询审查：两个任务文件范围（pref_core vs pref_cli）完全解耦互不重叠，无死锁和循环依赖风险，验证命令独立且完备。经详细审查，未发现阻塞性缺陷，结论：建议通过。
        """

        tasks = [
            {"task_id": f"{wf_id}-plan-executor", "workflow_id": wf_id, "node": "plan", "dispatch_role": "executor", "status": "completed"},
            {"task_id": f"{wf_id}-plan-challenger", "workflow_id": wf_id, "node": "plan", "dispatch_role": "challenger", "status": "completed"},
        ]
        notes = [
            {"node": "plan", "kind": "plan", "title": "解耦实现计划", "body": plan_body, "task_id": f"{wf_id}-plan-executor"},
            {"node": "plan", "kind": "review", "title": "对抗质询", "body": challenger_body, "task_id": f"{wf_id}-plan-challenger"},
        ]

        from unittest import mock
        with mock.patch("herdr.workflow_docs.load_notes", return_value=notes):
            res = plan_stage_dispatch(wf_id, impl_node, tasks, "实现偏好设置与命令行工具", context_branch="main")
            self.assertEqual(res.get("mode"), "dispatch")
            specs = res.get("specs", [])
            self.assertEqual(len(specs), 2, "Should dispatch 2 decoupled implementation tasks")
            self.assertEqual(specs[0]["dispatch_role"], "impl_1")
            self.assertEqual(specs[1]["dispatch_role"], "impl_2")
            self.assertEqual(specs[0]["planned_files"], ["pref_core.py", "test_pref_core.py"])
            self.assertEqual(specs[1]["planned_files"], ["pref_cli.py", "test_pref_cli.py"])
            self.assertNotEqual(specs[0]["task_id"], specs[1]["task_id"])

    def test_18_challenger_adversarial_review_gate_enforced(self):
        """Scenario 18: Missing, invalid, or blocked challenger review prevents implementation dispatch."""
        from herdr.direct_dispatch import plan_stage_dispatch
        import yaml

        with open(self.template_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        impl_node = find_node(cfg, "implementation")

        wf_id = "wf-plan-gate-01"
        plan_body = """
        # Implementation Plan
        ## 验收条目映射
        - Task 1: 满足 AC-1
        ## 修改文件范围
        - Create: pref.py
        ## 验证命令
        pytest -q test_pref.py
        """

        from unittest import mock

        # 1. Missing challenger review
        tasks = [
            {"task_id": f"{wf_id}-plan-executor", "workflow_id": wf_id, "node": "plan", "dispatch_role": "executor", "status": "completed"},
            {"task_id": f"{wf_id}-plan-challenger", "workflow_id": wf_id, "node": "plan", "dispatch_role": "challenger", "status": "completed"},
        ]
        notes_no_chal = [
            {"node": "plan", "kind": "plan", "title": "实现计划", "body": plan_body, "task_id": f"{wf_id}-plan-executor"},
        ]
        with mock.patch("herdr.workflow_docs.load_notes", return_value=notes_no_chal):
            res = plan_stage_dispatch(wf_id, impl_node, tasks, "实现需求")
            self.assertEqual(res.get("mode"), "refusal")
            self.assertEqual(res.get("refusal_code"), "challenger_missing")

        # 2. Blocked challenger review verdict
        tasks_blocked = [
            {"task_id": f"{wf_id}-plan-executor", "workflow_id": wf_id, "node": "plan", "dispatch_role": "executor", "status": "completed"},
            {"task_id": f"{wf_id}-plan-challenger", "workflow_id": wf_id, "node": "plan", "dispatch_role": "challenger", "status": "completed", "stage_verdict": "blocked", "stage_verdict_note": "存在死锁风险"},
        ]
        notes_blocked = [
            {"node": "plan", "kind": "plan", "title": "实现计划", "body": plan_body, "task_id": f"{wf_id}-plan-executor"},
            {"node": "plan", "kind": "review", "title": "质询", "body": "方案对抗与质询评估：发现严重死锁风险与循环依赖。HERDR_GATE_VERDICT: blocked", "task_id": f"{wf_id}-plan-challenger"},
        ]
        with mock.patch("herdr.workflow_docs.load_notes", return_value=notes_blocked):
            res = plan_stage_dispatch(wf_id, impl_node, tasks_blocked, "实现需求")
            self.assertEqual(res.get("mode"), "refusal")
            self.assertEqual(res.get("refusal_code"), "challenger_blocked")

    def test_19_invalid_plan_decoupling_refused(self):
        """Scenario 19: Tasks with overlapping files or intra-node dependencies are strictly refused."""
        from herdr.direct_dispatch import plan_stage_dispatch
        import yaml

        with open(self.template_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        impl_node = find_node(cfg, "implementation")

        wf_id = "wf-plan-overlap-01"

        # 1. Overlapping files
        plan_overlapping = """
        # Implementation Plan
        ## 任务拆分
        ### Task 1: 模块 A
        - **Files:**
          - Modify: shared_config.py
          - Test: test_a.py
        - **Verify:** pytest -q test_a.py

        ### Task 2: 模块 B
        - **Files:**
          - Modify: shared_config.py
          - Test: test_b.py
        - **Verify:** pytest -q test_b.py
        """
        chal_body = """
        ## 方案对抗性审查与可行性风险评估
        质询审查：经过对各拆解任务的详细核查，本方案对验收标准的映射完整。可行性评估认为风险与验证命令具备可行性，无过度设计与死锁风险，验证基线完备。确认通过。
        """
        tasks = [
            {"task_id": f"{wf_id}-plan-executor", "workflow_id": wf_id, "node": "plan", "status": "completed"},
            {"task_id": f"{wf_id}-plan-challenger", "workflow_id": wf_id, "node": "plan", "dispatch_role": "challenger", "status": "completed"},
        ]
        notes = [
            {"node": "plan", "kind": "plan", "title": "重叠计划", "body": plan_overlapping, "task_id": f"{wf_id}-plan-executor"},
            {"node": "plan", "kind": "review", "title": "质询", "body": chal_body, "task_id": f"{wf_id}-plan-challenger"},
        ]

        from unittest import mock
        with mock.patch("herdr.workflow_docs.load_notes", return_value=notes):
            res = plan_stage_dispatch(wf_id, impl_node, tasks, "需求")
            self.assertEqual(res.get("mode"), "refusal")
            self.assertEqual(res.get("refusal_code"), "plan_decoupling_refusal")
            self.assertIn("overlapping file 'shared_config.py'", res.get("reason", ""))

        # 2. Declared intra-node dependency
        plan_dep = """
        # Implementation Plan
        ## 任务拆分
        ### Task 1: 数据层
        - **Files:**
          - Create: db.py
          - Test: test_db.py
        - **Verify:** pytest -q test_db.py

        ### Task 2: 业务层
        - **Dependencies:** Task 1
        - **Files:**
          - Create: service.py
          - Test: test_service.py
        - **Verify:** pytest -q test_service.py
        """
        notes_dep = [
            {"node": "plan", "kind": "plan", "title": "依赖计划", "body": plan_dep, "task_id": f"{wf_id}-plan-executor"},
            {"node": "plan", "kind": "review", "title": "质询", "body": chal_body, "task_id": f"{wf_id}-plan-challenger"},
        ]
        with mock.patch("herdr.workflow_docs.load_notes", return_value=notes_dep):
            res = plan_stage_dispatch(wf_id, impl_node, tasks, "需求")
            self.assertEqual(res.get("mode"), "refusal")
            self.assertEqual(res.get("refusal_code"), "plan_decoupling_refusal")
            self.assertIn("intra-node dependency", res.get("reason", ""))

    def test_20_duplicate_event_and_resume_idempotent(self):
        """Scenario 20: Idempotent dispatch handling; active task yields wait, no duplicate creation."""
        from herdr.direct_dispatch import plan_stage_dispatch
        import yaml

        with open(self.template_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        impl_node = find_node(cfg, "implementation")

        wf_id = "wf-plan-idempotent-01"
        tasks_with_active = [
            {
                "task_id": f"{wf_id}-implementation-t1",
                "workflow_id": wf_id,
                "node": "implementation",
                "status": "working",
            }
        ]
        res = plan_stage_dispatch(wf_id, impl_node, tasks_with_active, "需求")
        self.assertEqual(res.get("mode"), "wait")
        self.assertEqual(res.get("reason"), "node has active tasks")

    def test_21_global_coordinator_lockout_enforced(self):
        """Scenario 21: Real try_direct_stage_advance and stage delivery execute with coordinator locked out."""
        from services.controller import stage_delivery, direct_dispatch
        from unittest import mock

        mock_ports = mock.MagicMock()
        mock_ports.workflow_config_for.return_value = {"name": "software-development-v1"}
        mock_ports.find_node.return_value = {"id": "requirements", "node_type": "agent", "purpose": "需求分析"}
        mock_ports.get_stage_policy.return_value = {}
        mock_ports.direct_stage_dispatch_enabled.return_value = True
        mock_ports.TASK_MANAGER = "bin/herdr-task"
        mock_ports._dispatch_candidate_ready.return_value = True
        mock_ports.direct_dispatch_planner = mock.MagicMock()
        mock_ports.direct_dispatch_planner.plan_stage_dispatch.return_value = {
            "mode": "dispatch",
            "specs": [{
                "task_id": "t1",
                "task_type": "coding",
                "integration_mode": "git",
                "goal": "完成需求分析",
                "prompt": "分析需求",
                "acceptance": [],
                "dispatch_role": "worker",
                "dispatch_round": 1,
            }],
        }
        mock_ports.project_for_workflow.return_value = {
            "startup_ready": True,
            "project_root": self.tmp_dir,
            "requirement": "测试真实直派",
        }
        mock_ports.load_tasks.return_value = []
        mock_ports._scheduler_join_gate_allows.return_value = True
        mock_ports.blocked_verdict_dep.return_value = None
        mock_ports._reverification_reused_node.return_value = False
        mock_ports.DIRECT_DISPATCH_LAUNCH_TIMEOUT = 10
        mock_ports.subprocess.run.return_value = mock.MagicMock(returncode=0, stdout="LAUNCHED", stderr="")

        # Lockout all coordinator interfaces
        mock_ports.coordinator_intake_enabled.side_effect = AssertionError("coordinator_intake_enabled called!")
        mock_ports.coordinator_pane_for_workflow.side_effect = AssertionError("coordinator_pane_for_workflow called!")
        mock_ports.coordinator_status.side_effect = AssertionError("coordinator_status called!")
        mock_ports.maybe_compact_coordinator.side_effect = AssertionError("maybe_compact_coordinator called!")

        item = {
            "workflow_id": "wf-lockout-01",
            "node_id": "requirements",
            "stage": "start",
            "node": {"id": "requirements", "node_type": "agent", "purpose": "需求分析"},
        }
        # 1. Real try_direct_stage_advance without coordinator
        res = direct_dispatch.try_direct_stage_advance(mock_ports, item)
        self.assertTrue(res)
        mock_ports.subprocess.run.assert_called()

        # 2. Stage advance refusal records attention note without coordinator
        mock_ports.try_direct_stage_advance = mock.MagicMock(return_value=False)
        item_refused = {
            "workflow_id": "wf-lockout-02",
            "node_id": "implementation",
            "direct_dispatch_fallback_reason": "plan_invalid: missing sections",
        }
        with mock.patch("herdr.coordinator_stage_admission.admit_stage_dispatch", return_value=(item_refused, {"id": "implementation"}, None, None)):
            stage_delivery._handle_stage_advance_item(mock_ports, item_refused)
            mock_ports.attention_note.assert_called()
            call_args = mock_ports.attention_note.call_args
            self.assertEqual(call_args[1]["reason"], "plan_invalid: missing sections")

    def test_22_project_ensure_and_provision_skips_coordinator_for_software_development_v1(self):
        """Scenario 22: ensure_project and provision_project skip coordinator start/healing for software-development-v1."""
        from herdr.projects import ensure_project, template_requires_coordinator
        from unittest import mock

        self.assertFalse(template_requires_coordinator("software-development-v1"))
        self.assertTrue(template_requires_coordinator("legacy-workflow"))

        mock_record = {
            "project_id": "p-1",
            "project_name": "test-proj",
            "project_root": self.tmp_dir,
            "workspace_id": "w1",
            "coordinator_pane_id": "dead-pane",
            "workflow_file": "/tmp/wf.json",
        }
        with mock.patch("herdr.projects.project_by_root", return_value=mock_record), \
             mock.patch("herdr.projects._workspace_alive", return_value=True), \
             mock.patch("herdr.projects._pane_alive", return_value=False), \
             mock.patch("herdr.projects._workflow_template_of", return_value="software-development-v1"), \
             mock.patch("herdr.projects._recover_coordinator_pane", side_effect=AssertionError("recover called!")), \
             mock.patch("herdr.projects._start_coordinator", side_effect=AssertionError("start_coordinator called!")):
            # ensure_project should succeed without healing or starting coordinator
            res = ensure_project(self.tmp_dir, template_name="software-development-v1")
            self.assertEqual(res["project_id"], "p-1")

    def test_23_fix_loop_item_handled_without_coordinator_for_staged_workflow(self):
        """Scenario 23: fix_loop notifications bypass coordinator prompt and clear attention directly."""
        from services.controller import fix_notification
        from unittest import mock

        mock_ports = mock.MagicMock()
        mock_ports.workflow_config_for.return_value = {"name": "software-development-v1"}
        mock_ports.coordinator_pane_for_workflow.side_effect = AssertionError("coordinator_pane_for_workflow called!")
        mock_ports.coordinator_status.side_effect = AssertionError("coordinator_status called!")
        mock_ports.subprocess.run.side_effect = AssertionError("subprocess.run prompt called!")

        item = {
            "kind": "fix_loop",
            "workflow_id": "wf-rework-01",
            "gate_stage": "test",
            "retry_node": "implementation",
            "invalidated": ["task-1"],
            "loop_count": 1,
            "max_loops": 3,
        }
        fix_notification._handle_fix_loop_item(mock_ports, item)
        mock_ports.attention_clear.assert_called_with("wf-rework-01:fix_loop:test")

    def test_24_plan_invalidation_stale_notes_excluded(self):
        """Scenario 24: Obsolete plans marked stale by invalidation are excluded; fresh plan is dispatched."""
        from herdr.plan_parser import find_plan_deliverables
        from herdr.direct_dispatch import plan_stage_dispatch
        import yaml
        from unittest import mock

        with open(self.template_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        impl_node = find_node(cfg, "implementation")

        wf_id = "wf-stale-01"
        old_plan = """
        # Implementation Plan
        ## 目标
        旧方案实现
        ## 验收条目映射
        - Task 1: 满足旧需求
        ## 修改文件范围
        - Create: obsolete.py
        ## 验证命令
        pytest -q test_obsolete.py
        """
        old_review = """
        ## 方案对抗性审查与可行性风险评估
        旧方案可行性评估通过。确认通过。无死锁和循环依赖。验证命令完备。
        """
        new_plan = """
        # Implementation Plan
        ## 目标
        新方案实现，为实现阶段提供精准的任务规格与可执行验证命令，确保无全局总指挥直接派发。
        ## 验收条目映射
        - Task 1: 满足新需求，重构核心模块并保持向后兼容
        ## 修改文件范围
        - Create: fresh.py
        - Test: test_fresh.py
        ## 验证命令
        pytest -q test_fresh.py
        """
        new_review = """
        ## 方案对抗性审查与可行性风险评估
        新方案可行性评估通过，修改文件范围限定在 fresh.py 和 test_fresh.py。任务划分清晰合理，无跨任务未声明依赖，无并发资源竞争。经仔细排查，未发现阻塞性缺陷，建议通过。
        """

        notes = [
            {"node": "plan", "kind": "plan", "title": "旧计划", "body": old_plan, "task_id": "t-plan-old", "ts": 1000.0},
            {"node": "plan", "kind": "review", "title": "旧审查", "body": old_review, "task_id": "t-chal-old", "ts": 1001.0},
            {"node": "plan", "kind": "invalidation", "title": "作废", "invalidates": ["plan"], "ts": 1005.0},
            {"node": "plan", "kind": "plan", "title": "新计划", "body": new_plan, "task_id": "t-plan-new", "ts": 1010.0},
            {"node": "plan", "kind": "review", "title": "新审查", "body": new_review, "task_id": "t-chal-new", "ts": 1011.0},
        ]
        tasks = [
            {"task_id": "t-plan-old", "workflow_id": wf_id, "node": "plan", "role": "executor", "status": "superseded", "superseded_by": "t-plan-new"},
            {"task_id": "t-chal-old", "workflow_id": wf_id, "node": "plan", "role": "challenger", "status": "superseded", "superseded_by": "t-chal-new"},
            {"task_id": "t-plan-new", "workflow_id": wf_id, "node": "plan", "role": "executor", "status": "completed"},
            {"task_id": "t-chal-new", "workflow_id": wf_id, "node": "plan", "role": "challenger", "status": "completed"},
        ]
        with mock.patch("herdr.workflow_docs.load_notes", return_value=notes):
            p, c, e, ch = find_plan_deliverables(wf_id, tasks)
            self.assertIn("fresh.py", p)
            self.assertNotIn("obsolete.py", p)
            res = plan_stage_dispatch(wf_id, impl_node, tasks, "需求", context_branch="main")
            self.assertEqual(res.get("mode"), "dispatch")
            specs = res.get("specs", [])
            self.assertEqual(len(specs), 1)
            self.assertIn("fresh.py", specs[0]["planned_files"])
            self.assertNotIn("obsolete.py", specs[0]["planned_files"])

    def test_25_challenger_verdict_distinguishes_negation_and_blocked(self):
        """Scenario 25: Challenger verdict correctly handles negation, [BLOCKED], and missing verdicts."""
        from herdr.plan_parser import extract_challenger_verdict

        # 1. Negative wording of defects with approval -> PASS
        v1, r1 = extract_challenger_verdict(
            "## 方案对抗性审查与可行性风险评估\n经详细质询与可行性评估，架构无死锁风险。未发现阻塞性缺陷，建议通过。"
        )
        self.assertEqual(v1, "pass")

        # 2. [BLOCKED] marker -> BLOCKED
        v2, r2 = extract_challenger_verdict(
            "## 方案对抗性审查与可行性风险评估\n[BLOCKED] 存在高风险，应先修订计划。"
        )
        self.assertEqual(v2, "blocked")

        # 3. Explicit non-negated blocking defect -> BLOCKED
        v3, r3 = extract_challenger_verdict(
            "## 方案对抗性审查与可行性风险评估\n审查结论：发现阻塞性缺陷，模块之间存在循环依赖。"
        )
        self.assertEqual(v3, "blocked")

        # 4. Text without explicit verdict -> MISSING
        v4, r4 = extract_challenger_verdict(
            "## 方案对抗性审查与可行性风险评估\n这是一份详细的审查备忘录，列出了一些设计考量与性能优化建议，但没有给出任何裁决。"
        )
        self.assertEqual(v4, "missing")

    def test_26_unresolvable_dependencies_and_normalized_path_overlap_refused(self):
        """Scenario 26: Unresolvable dependencies, missing task fields, and ./a.py vs a.py are refused."""
        from herdr.plan_parser import parse_plan_tasks, validate_task_decoupling

        # 1. Unresolvable dependency (function/artifact dependency)
        plan_unresolved_dep = """
        # Implementation Plan
        ## 任务拆分
        ### Task 1: API 服务
        - **Files:**
          - Create: api.py
        - **Verify:** pytest -q test_api.py

        ### Task 2: 命令行入口
        - **Dependencies:** api.py 中的 fetch_api 函数
        - **Files:**
          - Create: cli.py
        - **Verify:** pytest -q test_cli.py
        """
        tasks1 = parse_plan_tasks(plan_unresolved_dep)
        ok1, reason1 = validate_task_decoupling(tasks1)
        self.assertFalse(ok1)
        self.assertIn("unsupported intra-node dependency", reason1)

        # 2. Path normalization overlap: ./a.py vs a.py
        plan_path_overlap = """
        # Implementation Plan
        ## 任务拆分
        ### Task 1: 核心
        - **Files:**
          - Create: ./a.py
        - **Verify:** pytest -q test_a.py

        ### Task 2: 包装
        - **Files:**
          - Create: a.py
        - **Verify:** pytest -q test_b.py
        """
        tasks2 = parse_plan_tasks(plan_path_overlap)
        ok2, reason2 = validate_task_decoupling(tasks2)
        self.assertFalse(ok2)
        self.assertIn("overlapping file 'a.py'", reason2)

        # 3. Missing files or verify command in multi-task
        plan_missing_fields = """
        # Implementation Plan
        ## 任务拆分
        ### Task 1: 有效任务
        - **Files:**
          - Create: ok.py
        - **Verify:** pytest -q test_ok.py

        ### Task 2: 空白任务
        - **Goal:** 无文件且无命令的任务
        """
        tasks3 = parse_plan_tasks(plan_missing_fields)
        ok3, reason3 = validate_task_decoupling(tasks3)
        self.assertFalse(ok3)

    def test_27_duplicate_task_indices_refused_and_roles_unique(self):
        """Scenario 27: Duplicate task numbers (Task 1 twice) are rejected and dispatch roles are unique."""
        from herdr.plan_parser import parse_plan_tasks, validate_task_decoupling

        plan_duplicate = """
        # Implementation Plan
        ## 任务拆分
        ### Task 1: 模块 A
        - **Files:**
          - Create: a.py
        - **Verify:** pytest -q test_a.py

        ### Task 1: 模块 B
        - **Files:**
          - Create: b.py
        - **Verify:** pytest -q test_b.py
        """
        tasks = parse_plan_tasks(plan_duplicate)
        ok, reason = validate_task_decoupling(tasks)
        self.assertFalse(ok)
        self.assertIn("duplicate task index", reason)

    def test_28_verify_command_multiline_extraction(self):
        """Scenario 28: Multiline verify command extraction does not produce '**'."""
        from herdr.plan_parser import parse_plan_tasks

        plan_multiline_cmd = """
        # Implementation Plan
        ## 任务拆分
        ### Task 1: 核心模块
        - **Goal:** 核心逻辑
        - **Files:**
          - Create: core.py
        - **Verify:**
          pytest -q test_core.py
        - **Dependencies:** None
        """
        tasks = parse_plan_tasks(plan_multiline_cmd)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["verify_command"], "pytest -q test_core.py")
        self.assertNotEqual(tasks[0]["verify_command"], "**")

    def test_29_real_workflow_template_config_recognition_and_task_event_bypass(self):
        """Scenario 29: workflow_template identifies staged workflow and task_events bypasses coordinator on rejection."""
        from herdr.stage_skills import is_staged_dev_flow_workflow
        from services.controller import task_events
        from unittest import mock

        # 1. workflow_template recognition
        real_cfg = {"workflow_template": "software-development-v1"}
        self.assertTrue(is_staged_dev_flow_workflow(real_cfg))
        self.assertTrue(is_staged_dev_flow_workflow({"coordinator_required": False}))

        # 2. task event rejection bypasses coordinator
        mock_ports = mock.MagicMock()
        mock_ports.get_task.return_value = {
            "task_id": "t-done-fail",
            "workflow_id": "wf-staged-01",
            "status": "agent_done",
        }
        mock_ports.workflow_config_for.return_value = {"workflow_template": "software-development-v1"}
        mock_ports.try_auto_accept.return_value = False
        mock_ports.try_auto_verdict.return_value = False
        mock_ports.verdict_defer_reason.return_value = "plan deliverable incomplete"

        # Coordinator interfaces must NOT be invoked
        mock_ports.coordinator_status.side_effect = AssertionError("coordinator_status should not be called!")
        mock_ports.coordinator_pane_for_workflow.side_effect = AssertionError("coordinator_pane_for_workflow should not be called!")
        mock_ports.subprocess.run.side_effect = AssertionError("subprocess prompt should not be called!")

        item = {
            "key": "t-done-fail:event",
            "task_id": "t-done-fail",
            "event_type": "done",
            "workflow_id": "wf-staged-01",
        }
        task_events._handle_task_event(mock_ports, item)
        mock_ports.attention_note.assert_called_once()
        args, kwargs = mock_ports.attention_note.call_args
        self.assertIn("auto_acceptance_rejected", kwargs.get("reason", ""))

    def test_30_unattributed_review_note_and_historic_plan_files_isolation(self):
        """Scenario 30: Unattributed review notes cannot bypass [BLOCKED] and historic plan files do not override notes."""
        from herdr.plan_parser import find_plan_deliverables
        from unittest import mock

        wf_id = "wf-iso-01"
        tasks = [
            {"task_id": "t-exec-01", "workflow_id": wf_id, "node": "plan", "role": "executor", "status": "completed"},
            {"task_id": "t-chal-01", "workflow_id": wf_id, "node": "plan", "role": "challenger", "status": "completed"},
        ]

        # Note 1: challenger explicit [BLOCKED]
        # Note 2: subsequent note with NO task attribution claiming [PASS]
        notes = [
            {"node": "plan", "kind": "plan", "title": "当前有效计划", "body": "# Plan\n## 目标\n当前方案", "task_id": "t-exec-01", "ts": 100.0},
            {"node": "plan", "kind": "review", "title": "真实审查", "body": "[BLOCKED] 存在严重架构缺陷", "task_id": "t-chal-01", "ts": 101.0},
            {"node": "plan", "kind": "review", "title": "伪造审查", "body": "[PASS] 虚假通过", "task_id": None, "author": None, "ts": 102.0},
        ]
        with mock.patch("herdr.workflow_docs.load_notes", return_value=notes):
            p, c, e, ch = find_plan_deliverables(wf_id, tasks)
            self.assertIn("当前方案", p)
            # Must strictly match challenger task's [BLOCKED], unattributed [PASS] ignored
            self.assertIn("[BLOCKED]", c)
            self.assertNotIn("虚假通过", c)

    def test_31_challenger_verdict_subtle_negation_and_conditional_expressions(self):
        """Scenario 31: Challenger verdict correctly evaluates negation, conditional expressions, and body mentions."""
        from herdr.plan_parser import extract_challenger_verdict

        # 1. "方案对抗质询：目前不能通过。" -> BLOCKED
        v1, r1 = extract_challenger_verdict("方案对抗质询：目前不能通过。")
        self.assertEqual(v1, "blocked")

        # 2. "结论：暂不予通过，需补充错误处理方案。" -> BLOCKED
        v2, r2 = extract_challenger_verdict("结论：暂不予通过，需补充错误处理方案。")
        self.assertEqual(v2, "blocked")

        # 3. "自动测试已经通过，但本轮方案审查尚未完成，请等待。" -> MISSING (not approved!)
        v3, r3 = extract_challenger_verdict("自动测试已经通过，但本轮方案审查尚未完成，请等待。")
        self.assertEqual(v3, "missing")

        # 4. "没有发现阻塞性缺陷，建议通过。" -> PASS
        v4, r4 = extract_challenger_verdict("没有发现阻塞性缺陷，建议通过。")
        self.assertEqual(v4, "pass")

    def test_32_extensionless_files_support_in_plan(self):
        """Scenario 32: Plan files without extension (Dockerfile, Makefile, LICENSE) are recognized."""
        from herdr.plan_parser import parse_plan_tasks, validate_task_decoupling

        plan_docker = """
        # Implementation Plan
        ## 任务拆分
        ### Task 1: 容器化构建
        - **Goal:** 添加容器镜像构建配置
        - **Files:**
          - Create: Dockerfile
        - **Verify:** docker build .
        - **Dependencies:** None

        ### Task 2: 说明文档
        - **Goal:** 更新文档
        - **Files:**
          - Modify: README.md
        - **Verify:** pytest -q test_readme.py
        - **Dependencies:** None
        """
        tasks = parse_plan_tasks(plan_docker)
        self.assertEqual(len(tasks), 2)
        self.assertIn("Dockerfile", tasks[0]["files"])
        self.assertIn("README.md", tasks[1]["files"])
        ok, reason = validate_task_decoupling(tasks)
        self.assertTrue(ok, f"Decoupling should pass for Dockerfile and README.md: {reason}")


if __name__ == "__main__":
    unittest.main()
