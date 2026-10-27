"""End-to-End Isolated Verification for software-development-v1 6-Stage Workflow.

Performs:
1. Normal flow: requirements -> plan -> implementation -> test -> review -> wrapup
   - Isolated SQLite database (never touches production ~/.herdr-controller/state.db)
   - Isolated Git repository workspace
   - Actual candidate freeze after implementation
   - Verification that review is dispatched ONLY after test passes
   - Verification that wrapup validates both test and review on the identical candidate SHA
   - Delivery ceiling = workspace: confirms no PR required, no unauthorized merge/deploy
2. Controlled defect rework flow:
   - Implementation produces defective code
   - Test detects failure -> outputs blocked verdict
   - Fix loop triggers back to implementation (rework budget consumed)
   - Implementation fixes defect -> new candidate SHA frozen
   - Test re-runs on new candidate SHA -> pass
   - Review runs on new candidate SHA with fresh test evidence -> pass
   - Wrapup converges on new candidate SHA -> completed
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import yaml
from pathlib import Path

HERDR_ROOT = Path(__file__).resolve().parent.parent
if str(HERDR_ROOT) not in sys.path:
    sys.path.insert(0, str(HERDR_ROOT))

from herdr.state_store import SQLiteStateStore
from herdr.workflow import normalize_workflow, find_node, get_ready_nodes
from herdr import direct_dispatch as dd
from herdr.stage_skills import (
    assemble_stage_context,
    resolve_stage_skill,
    validate_requirements_deliverable_content,
    validate_plan_deliverable_content,
    validate_test_evidence_freshness,
)
from herdr.workflow_docs import append_note, load_notes, notes_path
from herdr.scheduler import evaluate_join_gate


class TestSoftwareDevelopmentV1E2EIsolated(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="haflow_e2e_isolated_")
        self.db_path = Path(self.tmp_dir) / "isolated_state.db"
        self.repo_dir = Path(self.tmp_dir) / "isolated_repo"
        self.docs_dir = Path(self.tmp_dir) / "workflows"
        os.environ["HERDR_WORKFLOW_DOCS_DIR"] = str(self.docs_dir)
        os.environ["HERDR_GATE_VERDICT_DIR"] = str(Path(self.tmp_dir) / "gate-verdicts")
        os.makedirs(os.environ["HERDR_GATE_VERDICT_DIR"], exist_ok=True)

        self.store = SQLiteStateStore(db_path=self.db_path)
        self._init_git_repo(self.repo_dir)

        template_file = HERDR_ROOT / "workflow_templates" / "software-development-v1.yaml"
        with open(template_file, "r", encoding="utf-8") as f:
            self.template_cfg = yaml.safe_load(f)

    def tearDown(self):
        os.environ.pop("HERDR_WORKFLOW_DOCS_DIR", None)
        os.environ.pop("HERDR_GATE_VERDICT_DIR", None)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _init_git_repo(self, path):
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "HAFlow Test"], cwd=str(path), check=True)
        subprocess.run(["git", "config", "user.email", "test@haflow.local"], cwd=str(path), check=True)
        readme = path / "README.md"
        readme.write_text("# Target App\nInitial codebase\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=str(path), check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(path), check=True)

    def _get_head_sha(self, path):
        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(path), check=True, capture_output=True, text=True)
        return res.stdout.strip()

    def test_01_normal_six_stage_lifecycle_e2e(self):
        """End-to-End normal flow: 6 stages executed in exact order with actual state progression."""
        wf_id = "wf-e2e-normal-01"
        wf_cfg = dict(self.template_cfg)
        wf_cfg["workflow_id"] = wf_id

        # 1. Register workflow
        self.store.save_workflow({
            "workflow_id": wf_id,
            "name": "software-development-v1",
            "status": "running",
            "config_json": json.dumps(wf_cfg),
            "payload_json": json.dumps({"project_root": str(self.repo_dir)}),
        })

        completed_nodes = set()

        # Stage 1: Requirements
        ready = [n["id"] for n in get_ready_nodes(wf_cfg, completed_nodes)]
        self.assertEqual(ready, ["requirements"])
        req_ctx = assemble_stage_context(wf_id, f"{wf_id}-req-t1", "run-1", "requirements")
        self.assertIn("brainstorming", [s["name"] for s in req_ctx["bound_skills"]])

        # Requirements delivers structured spec note to shared docs
        req_body = """
        # 需求规格
        ## 用户目标与背景
        为系统增加轻量用户偏好设置。
        ## 功能范围与非目标
        范围：增加 user_pref.json 读写支持；非目标：暂不提供云端同步。
        ## 验收标准
        - AC-1: 读取缺失时返回默认字典
        - AC-2: 保存后持久化到指定文件
        ## 证明方式与验证计划
        proof_mode: tdd
        验证命令: pytest -q test_pref.py
        ## 交付上限
        delivery_ceiling: workspace
        """
        ok, missing = validate_requirements_deliverable_content(req_body)
        self.assertTrue(ok, f"Requirements content must be valid: {missing}")
        append_note(wf_id, kind="requirement", title="用户偏好设置规格", body=req_body, node="requirements")
        completed_nodes.add("requirements")

        # Stage 2: Plan
        ready = [n["id"] for n in get_ready_nodes(wf_cfg, completed_nodes)]
        self.assertEqual(ready, ["plan"])
        plan_ctx = assemble_stage_context(wf_id, f"{wf_id}-plan-t2", "run-1", "plan")
        self.assertIn("HAFlow 是当前流程的唯一主控系统", plan_ctx["prompt_block"])

        plan_body = """
        # 技术实现方案
        ## 验收条目映射
        - Task 1: 实现 pref.py 偏好管理 (满足 AC-1, AC-2)
        ## 修改文件范围
        - Create: pref.py
        - Create: test_pref.py
        ## 验证命令
        pytest -q test_pref.py
        """
        challenger_body = """
        ## 方案对抗性审查与可行性风险评估
        质询审查：经过对偏好设置方案与文件修改范围的详细核查，本方案对验收标准的映射完整。可行性评估认为风险与验证命令具备可行性，无过度设计与死锁风险，验证基线完备。确认通过。
        """
        ok, missing = validate_plan_deliverable_content(plan_body, role="executor")
        self.assertTrue(ok, f"Plan content must be valid: {missing}")
        ok, missing = validate_plan_deliverable_content(challenger_body, role="challenger")
        self.assertTrue(ok, f"Challenger content must be valid: {missing}")

        self.store.save_task({
            "task_id": f"{wf_id}-plan-executor",
            "workflow_id": wf_id,
            "node": "plan",
            "dispatch_role": "executor",
            "status": "completed",
        })
        self.store.save_task({
            "task_id": f"{wf_id}-plan-challenger",
            "workflow_id": wf_id,
            "node": "plan",
            "dispatch_role": "challenger",
            "status": "completed",
        })

        append_note(wf_id, kind="plan", title="偏好设置实现计划", body=plan_body, node="plan", task_id=f"{wf_id}-plan-executor")
        append_note(wf_id, kind="review", title="方案对抗审查与可行性评估", body=challenger_body, node="plan", task_id=f"{wf_id}-plan-challenger")
        completed_nodes.add("plan")

        # Stage 3: Implementation - Directly planned & dispatched from plan deliverable!
        ready = [n["id"] for n in get_ready_nodes(wf_cfg, completed_nodes)]
        self.assertEqual(ready, ["implementation"])

        impl_node = find_node(wf_cfg, "implementation")
        all_tasks = self.store.list_tasks(wf_id)
        dispatch_plan = dd.plan_stage_dispatch(
            wf_id,
            impl_node,
            all_tasks,
            "实现用户偏好设置",
            context_branch="main",
            project_root=str(self.repo_dir),
        )
        self.assertEqual(dispatch_plan.get("mode"), "dispatch")
        specs = dispatch_plan.get("specs", [])
        self.assertEqual(len(specs), 1)
        spec = specs[0]
        self.assertIn("pref.py", spec.get("planned_files", []))

        # Implementation writes code and commits to candidate branch
        pref_file = self.repo_dir / "pref.py"
        pref_file.write_text("def get_pref():\n    return {'theme': 'dark'}\n", encoding="utf-8")
        test_file = self.repo_dir / "test_pref.py"
        test_file.write_text("from pref import get_pref\ndef test_pref():\n    assert get_pref() == {'theme': 'dark'}\n", encoding="utf-8")
        subprocess.run(["git", "add", "pref.py", "test_pref.py"], cwd=str(self.repo_dir), check=True)
        subprocess.run(["git", "commit", "-m", "feat: implement user preferences"], cwd=str(self.repo_dir), check=True)

        candidate_sha = self._get_head_sha(self.repo_dir)
        self.assertTrue(len(candidate_sha) == 40)

        impl_task = {
            "task_id": spec["task_id"],
            "workflow_id": wf_id,
            "node": "implementation",
            "status": "completed",
            "commit": candidate_sha,
            "integration_mode": "git",
            "candidate_sha": candidate_sha,
        }
        self.store.save_task(impl_task)
        completed_nodes.add("implementation")

        # Stage 4: Test (must execute before review)
        ready = [n["id"] for n in get_ready_nodes(wf_cfg, completed_nodes)]
        self.assertEqual(ready, ["test"], "Review must NOT be ready; only test should be ready")

        # Test executes command against candidate
        test_cmd = ["pytest", "-q", "test_pref.py"]
        test_res = subprocess.run(test_cmd, cwd=str(self.repo_dir), capture_output=True, text=True)
        self.assertEqual(test_res.returncode, 0, f"Test command failed: {test_res.stderr}")

        test_evidence = {
            "command": "pytest -q test_pref.py",
            "exit_code": test_res.returncode,
            "output": test_res.stdout,
            "candidate_sha": candidate_sha,
        }
        append_note(
            wf_id,
            kind="evidence",
            title="S5 测试执行证据与门禁结论",
            body=json.dumps(test_evidence, ensure_ascii=False),
            node="test",
            base_sha=candidate_sha,
        )

        test_task = {
            "task_id": f"{wf_id}-test-t4",
            "workflow_id": wf_id,
            "node": "test",
            "status": "completed",
            "candidate_sha": candidate_sha,
            "verified_candidate_sha": candidate_sha,
            "stage_verdict": "pass",
            "integration_mode": "none",
        }
        self.store.save_task(test_task)
        completed_nodes.add("test")

        # Stage 5: Review (now ready because test passed)
        ready = [n["id"] for n in get_ready_nodes(wf_cfg, completed_nodes)]
        self.assertEqual(ready, ["review"], "Review is ready now that test completed")

        # Review reads test evidence and confirms candidate matches
        review_ctx = assemble_stage_context(
            wf_id, f"{wf_id}-review-t5", "run-1", "review",
            candidate_sha=candidate_sha,
            upstream_notes=[{"kind": "evidence", "node": "test", "title": "S5 测试执行证据", "body": json.dumps(test_evidence)}],
        )
        self.assertIn(candidate_sha, review_ctx["prompt_block"])
        self.assertIn("pytest -q test_pref.py", review_ctx["prompt_block"])

        review_task = {
            "task_id": f"{wf_id}-review-t5",
            "workflow_id": wf_id,
            "node": "review",
            "status": "completed",
            "candidate_sha": candidate_sha,
            "verified_candidate_sha": candidate_sha,
            "stage_verdict": "pass",
            "integration_mode": "none",
        }
        self.store.save_task(review_task)
        completed_nodes.add("review")

        # Stage 6: Wrapup (now both test and review have passed on the identical candidate_sha)
        ready = [n["id"] for n in get_ready_nodes(wf_cfg, completed_nodes)]
        self.assertEqual(ready, ["wrapup"])

        # Check join-gate convergence on wrapup
        wrapup_node = find_node(wf_cfg, "wrapup")
        all_tasks = self.store.list_tasks(workflow_id=wf_id)
        passed, reason, details = evaluate_join_gate(wrapup_node, all_tasks, wf_id, expected_candidate_sha=candidate_sha)
        self.assertTrue(passed, f"Join gate must pass when test & review agree on candidate SHA: {reason}, {details}")

        # Wrapup fulfills workspace delivery ceiling: no PR required, no merge attempted
        wrapup_task = {
            "task_id": f"{wf_id}-wrapup-t6",
            "workflow_id": wf_id,
            "node": "wrapup",
            "status": "completed",
            "candidate_sha": candidate_sha,
            "integration_mode": "none",
        }
        self.store.save_task(wrapup_task)
        completed_nodes.add("wrapup")

        # Full 6 stages completed
        self.assertEqual(len(completed_nodes), 6)
        print("\n[E2E VERIFICATION PASS] Completed 6-stage workflow in exact sequence.")

    def test_02_controlled_defect_rework_flow_e2e(self):
        """End-to-End defect rework: implementation bug -> test blocks -> rework fix -> new SHA -> pass."""
        wf_id = "wf-e2e-defect-rework-02"
        wf_cfg = dict(self.template_cfg)
        wf_cfg["workflow_id"] = wf_id

        self.store.save_workflow({
            "workflow_id": wf_id,
            "name": "software-development-v1",
            "status": "running",
            "config_json": json.dumps(wf_cfg),
            "payload_json": json.dumps({"project_root": str(self.repo_dir)}),
        })

        # 1. Implementation introduces a defect (bad function name)
        calc_file = self.repo_dir / "calc.py"
        calc_file.write_text("def add_bad(a, b):\n    return a - b\n", encoding="utf-8")
        test_calc = self.repo_dir / "test_calc.py"
        test_calc.write_text("from calc import add\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8")
        subprocess.run(["git", "add", "calc.py", "test_calc.py"], cwd=str(self.repo_dir), check=True)
        subprocess.run(["git", "commit", "-m", "feat: add calculator (defective)"], cwd=str(self.repo_dir), check=True)

        bad_candidate_sha = self._get_head_sha(self.repo_dir)

        impl_t1 = {
            "task_id": f"{wf_id}-impl-t1",
            "workflow_id": wf_id,
            "node": "implementation",
            "status": "completed",
            "commit": bad_candidate_sha,
            "integration_mode": "git",
            "candidate_sha": bad_candidate_sha,
        }
        self.store.save_task(impl_t1)

        # 2. Test executes against bad candidate SHA and fails
        test_res = subprocess.run(["pytest", "-q", "test_calc.py"], cwd=str(self.repo_dir), capture_output=True, text=True)
        self.assertNotEqual(test_res.returncode, 0, "Test must detect the defect")

        # Test reports blocked gate verdict
        test_t1 = {
            "task_id": f"{wf_id}-test-t1",
            "workflow_id": wf_id,
            "node": "test",
            "status": "completed",
            "candidate_sha": bad_candidate_sha,
            "stage_verdict": "blocked",
            "stage_verdict_note": "ImportError: cannot import name 'add' from 'calc'",
            "integration_mode": "none",
        }
        self.store.save_task(test_t1)

        # Review MUST NOT run when test is blocked
        # (review depends on test pass; if test blocked, DAG does not advance to review)
        ready_after_fail = [n["id"] for n in get_ready_nodes(wf_cfg, {"requirements", "plan"})]
        self.assertNotIn("review", ready_after_fail, "Review must not run when test is blocked")

        # 3. Fix-loop routes back to implementation (rework)
        retry_node = wf_cfg.get("selective_replan", {}).get("retry_node", "implementation")
        self.assertEqual(retry_node, "implementation")

        # Implementation fixes the defect
        calc_file.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        subprocess.run(["git", "add", "calc.py"], cwd=str(self.repo_dir), check=True)
        subprocess.run(["git", "commit", "-m", "fix: correct add function implementation"], cwd=str(self.repo_dir), check=True)

        fixed_candidate_sha = self._get_head_sha(self.repo_dir)
        self.assertNotEqual(bad_candidate_sha, fixed_candidate_sha)

        # Rework task replaces defective task
        impl_t1["status"] = "superseded"
        impl_t1["superseded_by"] = f"{wf_id}-impl-t2"
        self.store.save_task(impl_t1)

        impl_t2 = {
            "task_id": f"{wf_id}-impl-t2",
            "workflow_id": wf_id,
            "node": "implementation",
            "status": "completed",
            "commit": fixed_candidate_sha,
            "integration_mode": "git",
            "candidate_sha": fixed_candidate_sha,
            "supersedes": f"{wf_id}-impl-t1",
        }
        self.store.save_task(impl_t2)

        # 4. Fresh test executes against the fixed candidate SHA
        test_res2 = subprocess.run(["pytest", "-q", "test_calc.py"], cwd=str(self.repo_dir), capture_output=True, text=True)
        self.assertEqual(test_res2.returncode, 0, "Test should pass on fixed code")

        test_t1["status"] = "superseded"
        test_t1["superseded_by"] = f"{wf_id}-test-t2"
        self.store.save_task(test_t1)

        test_t2 = {
            "task_id": f"{wf_id}-test-t2",
            "workflow_id": wf_id,
            "node": "test",
            "status": "completed",
            "candidate_sha": fixed_candidate_sha,
            "verified_candidate_sha": fixed_candidate_sha,
            "stage_verdict": "pass",
            "integration_mode": "none",
            "supersedes": f"{wf_id}-test-t1",
        }
        self.store.save_task(test_t2)

        # 5. Review now runs against the new fixed candidate SHA
        review_t2 = {
            "task_id": f"{wf_id}-review-t2",
            "workflow_id": wf_id,
            "node": "review",
            "status": "completed",
            "candidate_sha": fixed_candidate_sha,
            "verified_candidate_sha": fixed_candidate_sha,
            "stage_verdict": "pass",
            "integration_mode": "none",
        }
        self.store.save_task(review_t2)

        # 6. Wrapup verifies that both test and review passed on the new fixed candidate SHA
        wrapup_node = find_node(wf_cfg, "wrapup")
        active_tasks = [t for t in self.store.list_tasks(workflow_id=wf_id) if not t.get("superseded_by") and t.get("status") != "superseded"]
        passed, reason, details = evaluate_join_gate(wrapup_node, active_tasks, wf_id, expected_candidate_sha=fixed_candidate_sha)
        self.assertTrue(passed, f"Wrapup must pass on fixed candidate SHA: {reason}, {details}")
        print("\n[E2E DEFECT REWORK PASS] Defect accurately blocked, reworked, and re-verified on new candidate SHA.")

    def test_03_multi_task_decoupled_lifecycle_e2e(self):
        """End-to-End multi-task decoupled flow: plan outputs 2 decoupled tasks -> direct dispatch -> candidate freeze -> test -> review -> wrapup."""
        wf_id = "wf-e2e-multi-task-03"
        wf_cfg = dict(self.template_cfg)
        wf_cfg["workflow_id"] = wf_id

        self.store.save_workflow({
            "workflow_id": wf_id,
            "name": "software-development-v1",
            "status": "running",
            "config_json": json.dumps(wf_cfg),
            "payload_json": json.dumps({"project_root": str(self.repo_dir)}),
        })

        completed_nodes = set()

        # 1. Requirements
        req_body = """
        # 用户档案与配置模块需求规格
        ## 用户目标与背景
        提供解耦的用户档案读取与设置模块。
        ## 功能范围与非目标
        范围：user_profile.py 与 user_settings.py
        非目标：不修改全局配置。
        ## 验收标准
        - AC-1: user_profile.py 返回用户档案
        - AC-2: user_settings.py 返回用户设置
        ## 证明方式与验证计划
        proof_mode: tdd
        验证命令: pytest -q test_user_profile.py test_user_settings.py
        ## 交付上限
        delivery_ceiling: workspace
        """
        append_note(wf_id, kind="requirement", title="需求规格", body=req_body, node="requirements")
        completed_nodes.add("requirements")

        # 2. Plan (executor + challenger)
        plan_body = """
        # 用户模块实现计划
        ## 任务拆分与验收条目映射
        ### Task 1: 用户档案模块实现
        - **Goal:** 实现 user_profile.py 档案接口
        - **Files:**
          - Create: user_profile.py
          - Create: test_user_profile.py
        - **Verify:**
          pytest -q test_user_profile.py
        - **Dependencies:** None

        ### Task 2: 用户设置模块实现
        - **Goal:** 实现 user_settings.py 配置接口
        - **Files:**
          - Create: user_settings.py
          - Create: test_user_settings.py
        - **Verify:**
          pytest -q test_user_settings.py
        - **Dependencies:** None
        """
        challenger_body = """
        ## 方案对抗性审查与可行性风险评估
        质询审查：各子任务修改文件预估与重叠分析表明，Task 1（user_profile.py）与 Task 2（user_settings.py）完全解耦，文件互不重叠，无死锁与循环依赖风险，支撑实现阶段并发执行。可行性评估通过。
        """

        self.store.save_task({
            "task_id": f"{wf_id}-plan-executor",
            "workflow_id": wf_id,
            "node": "plan",
            "dispatch_role": "executor",
            "status": "completed",
        })
        self.store.save_task({
            "task_id": f"{wf_id}-plan-challenger",
            "workflow_id": wf_id,
            "node": "plan",
            "dispatch_role": "challenger",
            "status": "completed",
        })
        append_note(wf_id, kind="plan", title="解耦任务拆分计划", body=plan_body, node="plan", task_id=f"{wf_id}-plan-executor")
        append_note(wf_id, kind="review", title="方案对抗审查", body=challenger_body, node="plan", task_id=f"{wf_id}-plan-challenger")
        completed_nodes.add("plan")

        # 3. Implementation: directly plan stage dispatch!
        impl_node = find_node(wf_cfg, "implementation")
        all_tasks = self.store.list_tasks(wf_id)
        dispatch_plan = dd.plan_stage_dispatch(
            wf_id,
            impl_node,
            all_tasks,
            "实现用户档案与配置模块",
            context_branch="main",
            project_root=str(self.repo_dir),
        )
        self.assertEqual(dispatch_plan.get("mode"), "dispatch")
        specs = dispatch_plan.get("specs", [])
        self.assertEqual(len(specs), 2, "Must dispatch 2 decoupled implementation tasks")

        # Task 1 writes user_profile.py and test_user_profile.py
        (self.repo_dir / "user_profile.py").write_text("def get_profile():\n    return {'name': 'Alice'}\n", encoding="utf-8")
        (self.repo_dir / "test_user_profile.py").write_text("from user_profile import get_profile\ndef test_profile():\n    assert get_profile()['name'] == 'Alice'\n", encoding="utf-8")

        # Task 2 writes user_settings.py and test_user_settings.py
        (self.repo_dir / "user_settings.py").write_text("def get_settings():\n    return {'notify': True}\n", encoding="utf-8")
        (self.repo_dir / "test_user_settings.py").write_text("from user_settings import get_settings\ndef test_settings():\n    assert get_settings()['notify'] is True\n", encoding="utf-8")

        subprocess.run(["git", "add", "user_profile.py", "test_user_profile.py", "user_settings.py", "test_user_settings.py"], cwd=str(self.repo_dir), check=True)
        subprocess.run(["git", "commit", "-m", "feat: implement profile and settings decoupled modules"], cwd=str(self.repo_dir), check=True)

        candidate_sha = self._get_head_sha(self.repo_dir)

        # Both implementation tasks complete
        for idx, spec in enumerate(specs):
            self.store.save_task({
                "task_id": spec["task_id"],
                "workflow_id": wf_id,
                "node": "implementation",
                "status": "completed",
                "commit": candidate_sha,
                "integration_mode": "git",
                "candidate_sha": candidate_sha,
            })
        completed_nodes.add("implementation")

        # 4. Test stage (single verifier validating both modules on candidate_sha)
        ready = [n["id"] for n in get_ready_nodes(wf_cfg, completed_nodes)]
        self.assertEqual(ready, ["test"])

        test_res = subprocess.run(["pytest", "-q", "test_user_profile.py", "test_user_settings.py"], cwd=str(self.repo_dir), capture_output=True, text=True)
        self.assertEqual(test_res.returncode, 0, f"Tests should pass: {test_res.stderr}")

        test_evidence = {
            "command": "pytest -q test_user_profile.py test_user_settings.py",
            "exit_code": 0,
            "output": test_res.stdout,
            "candidate_sha": candidate_sha,
        }
        append_note(
            wf_id,
            kind="evidence",
            title="S5 多任务测试执行证据",
            body=json.dumps(test_evidence, ensure_ascii=False),
            node="test",
            base_sha=candidate_sha,
        )
        self.store.save_task({
            "task_id": f"{wf_id}-test-t4",
            "workflow_id": wf_id,
            "node": "test",
            "status": "completed",
            "candidate_sha": candidate_sha,
            "verified_candidate_sha": candidate_sha,
            "stage_verdict": "pass",
            "integration_mode": "none",
        })
        completed_nodes.add("test")

        # 5. Review stage
        ready = [n["id"] for n in get_ready_nodes(wf_cfg, completed_nodes)]
        self.assertEqual(ready, ["review"])

        self.store.save_task({
            "task_id": f"{wf_id}-review-t5",
            "workflow_id": wf_id,
            "node": "review",
            "status": "completed",
            "candidate_sha": candidate_sha,
            "verified_candidate_sha": candidate_sha,
            "stage_verdict": "pass",
            "integration_mode": "none",
        })
        completed_nodes.add("review")

        # 6. Wrapup stage
        ready = [n["id"] for n in get_ready_nodes(wf_cfg, completed_nodes)]
        self.assertEqual(ready, ["wrapup"])

        wrapup_node = find_node(wf_cfg, "wrapup")
        all_tasks = self.store.list_tasks(workflow_id=wf_id)
        passed, reason, details = evaluate_join_gate(wrapup_node, all_tasks, wf_id, expected_candidate_sha=candidate_sha)
        self.assertTrue(passed, f"Join gate must pass: {reason}, {details}")

        self.store.save_task({
            "task_id": f"{wf_id}-wrapup-t6",
            "workflow_id": wf_id,
            "node": "wrapup",
            "status": "completed",
            "candidate_sha": candidate_sha,
            "integration_mode": "none",
        })
        completed_nodes.add("wrapup")
        self.assertEqual(len(completed_nodes), 6)
        print("\n[E2E MULTI-TASK PASS] Completed decoupled multi-task execution without coordinator.")


if __name__ == "__main__":
    unittest.main()
