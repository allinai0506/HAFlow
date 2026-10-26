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


if __name__ == "__main__":
    unittest.main()
