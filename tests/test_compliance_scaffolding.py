import json
import os
import tempfile
import unittest
from pathlib import Path

from herdr.compliance_scaffolding import (
    build_bug_report_template,
    ensure_compliance_scaffolding,
    is_fix_task,
    update_contract_whitelist,
)
from herdr import task_delivery as delivery


class TestComplianceScaffolding(unittest.TestCase):
    def test_is_fix_task_detection(self):
        # Case 1: task_type is fix / bugfix / hotfix
        self.assertTrue(is_fix_task(task_type="fix"))
        self.assertTrue(is_fix_task(task_type="bugfix"))
        self.assertTrue(is_fix_task(task_type="hotfix"))

        # Case 2: branch contains fix
        self.assertTrue(is_fix_task(branch="agent/claude/fix-auth-token"))
        self.assertTrue(is_fix_task(branch="fix-1234"))
        self.assertTrue(is_fix_task(branch="hotfix/patch-1"))

        # Case 3: task_id contains fix
        self.assertTrue(is_fix_task(task_id="impl-atomic-fix"))
        self.assertTrue(is_fix_task(task_id="fix-bug-1002"))

        # Case 4: non-fix tasks
        self.assertFalse(is_fix_task(task_type="feat", branch="agent/opencode/feat-ui", task_id="ui-feat"))
        self.assertFalse(is_fix_task(task_type="docs", branch="agent/pi/docs-update", task_id="doc-update"))
        self.assertFalse(is_fix_task(task_type="test", branch="agent/agy/test-gate", task_id="cleanroom-test"))

    def test_build_bug_report_template(self):
        meta = {
            "task_id": "impl-atomic-fix",
            "branch": "agent/opencode/fix-atomic",
            "agent": "opencode",
        }
        rel_path, content = build_bug_report_template(meta)

        self.assertTrue(rel_path.startswith("docs/bug-reports/"))
        self.assertTrue(rel_path.endswith(".md"))
        self.assertIn("impl-atomic-fix", rel_path)

        # Must contain the required headings
        self.assertIn("## 根因", content)
        self.assertIn("## 防复发", content)
        self.assertIn("## 验证与回归", content)

        # Must have non-empty text under each heading
        lines = content.splitlines()
        for h in ("## 根因", "## 防复发", "## 验证与回归"):
            self.assertIn(h, lines)
            idx = lines.index(h)
            next_lines = lines[idx + 1:idx + 4]
            self.assertTrue(any(line.strip() for line in next_lines))

    def test_update_contract_whitelist_adds_bug_reports_pattern(self):
        contract = {
            "version": 1,
            "allowed_paths": ["src/**/*.java", "tests/**/*.java"],
            "required_files": [],
            "checks": [],
            "auto_rework": False,
        }
        rel_path = "docs/bug-reports/2026-10-11-fix-1.md"
        updated = update_contract_whitelist(contract, rel_path=rel_path)

        self.assertIsNotNone(updated)
        # Whitelist must include docs/bug-reports/* or the specific path
        self.assertTrue(
            any("docs/bug-reports" in p or "docs/*" in p for p in updated["allowed_paths"])
        )
        # Required files must register the retrospective document
        req_paths = [item["path"] for item in updated["required_files"]]
        self.assertIn(rel_path, req_paths)

        # Retrospective is now satisfied!
        self.assertTrue(delivery.retrospective_satisfied(updated))
        # Scope conflict is avoided!
        self.assertEqual(delivery.scope_conflicts(updated), [])

    def test_ensure_compliance_scaffolding_in_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            clone_path = Path(tmpdir)
            meta = {
                "task_id": "fix-nexus-auth",
                "task_type": "fix",
                "branch": "agent/codex/fix-nexus-auth",
                "agent": "codex",
            }
            contract = {
                "version": 1,
                "allowed_paths": ["src/*"],
                "required_files": [],
                "checks": [],
                "auto_rework": False,
            }

            res = ensure_compliance_scaffolding(
                clone_path=clone_path,
                task_metadata=meta,
                contract=contract,
            )

            self.assertTrue(res["scaffolded"])
            doc_path = clone_path / res["path"]
            self.assertTrue(doc_path.exists())
            content = doc_path.read_text(encoding="utf-8")
            self.assertIn("## 根因", content)
            self.assertIn("## 防复发", content)
            self.assertIn("## 验证与回归", content)

            # Contract whitelist was updated
            updated_contract = res["contract"]
            self.assertIn("docs/bug-reports/*", updated_contract["allowed_paths"])
            self.assertEqual(delivery.scope_conflicts(updated_contract), [])
            self.assertTrue(delivery.retrospective_satisfied(updated_contract))

    def test_non_fix_task_does_not_scaffold(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            clone_path = Path(tmpdir)
            meta = {
                "task_id": "feat-user-profile",
                "task_type": "feat",
                "branch": "agent/claude/feat-user-profile",
                "agent": "claude",
            }

            res = ensure_compliance_scaffolding(
                clone_path=clone_path,
                task_metadata=meta,
            )

            self.assertFalse(res["scaffolded"])
            self.assertFalse((clone_path / "docs" / "bug-reports").exists())

    def test_delivery_check_passes_with_scaffolded_compliance_doc(self):
        """Verify delivery-check recognizes pre-embedded compliance docs without Double Bind."""
        import subprocess
        from herdr.state_store import SQLiteStateStore
        from herdr import state_db

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            db_path = tmp / "state.db"
            state_db.init_db(db_path)
            store = SQLiteStateStore(db_path)

            # Initialize a real git repo
            repo_dir = tmp / "clone"
            repo_dir.mkdir()
            subprocess.run(["git", "-C", str(repo_dir), "init", "-b", "main"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Tester"], check=True)
            subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@test.local"], check=True)

            # Create initial file & commit
            (repo_dir / "app.py").write_text("print('hello')", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "init"], check=True, capture_output=True)
            base_sha = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()

            meta = {
                "task_id": "fix-bug-42",
                "task_type": "fix",
                "branch": "agent/opencode/fix-bug-42",
                "agent": "opencode",
            }
            contract = {
                "version": 1,
                "allowed_paths": ["app.py"],
                "required_files": [],
                "checks": [],
                "auto_rework": False,
            }

            # Scaffold compliance doc in workspace and update contract whitelist
            scaffold_res = ensure_compliance_scaffolding(
                clone_path=repo_dir,
                task_metadata=meta,
                contract=contract,
            )
            self.assertTrue(scaffold_res["scaffolded"])
            updated_contract = scaffold_res["contract"]

            # Workflow & Task in store
            store.save_workflow({
                "workflow_id": "wf-1",
                "status": "running",
                "execution_id": "exec-1",
            })
            task_dict = {
                "task_id": "fix-bug-42",
                "workflow_id": "wf-1",
                "run_id": "run-1",
                "execution_id": "exec-1",
                "clone_path": str(repo_dir),
                "integration_mode": "git",
                "execution_mode": "git",
                "status": "working",
                "baseline_commit": base_sha,
                "delivery_contract": updated_contract,
                "version": 1,
            }
            store.save_task(task_dict)

            # Run check_delivery - should be ready, with NO outside_scope or required_section_missing issues!
            receipt = delivery.check_delivery("fix-bug-42", store=store)
            self.assertEqual(receipt["status"], "ready")
            self.assertEqual(receipt["issues"], [])

    def test_worker_scaffolding_commit_and_integrate_chain(self):
        """Verify that when compliance scaffolding is embedded in worker sandbox:
        1. baseline_fingerprint reflects clean pre-task state (does not treat scaffolded doc as inherited);
        2. herdr-task commit automatically stages the scaffolded doc;
        3. after commit, working tree matches baseline_fingerprint so verify-baseline & integrate succeed.
        """
        import subprocess
        from pathlib import Path
        import sys
        cli = Path(__file__).resolve().parents[1] / "bin" / "herdr-task"

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo_dir = tmp / "clone"
            repo_dir.mkdir()
            subprocess.run(["git", "-C", str(repo_dir), "init", "-b", "main"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Tester"], check=True)
            subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@test.local"], check=True)

            (repo_dir / "app.py").write_text("print('hello')", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "init"], check=True, capture_output=True)
            base_sha = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()

            # Create task branch
            subprocess.run(["git", "-C", str(repo_dir), "checkout", "-b", "agent/codex/fix-login"], check=True, capture_output=True)

            # In services/herdr-worker.py:
            # Baseline fingerprint must be captured BEFORE compliance scaffolding
            # so the scaffolded file is recognized as newly produced untracked file, NOT inherited.
            import importlib.util
            worker_path = Path(__file__).resolve().parents[1] / "services" / "herdr-worker.py"

            spec = importlib.util.spec_from_file_location("herdr_worker_mod", str(worker_path))
            worker_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(worker_mod)

            baseline_fingerprint = worker_mod.build_baseline_fingerprint(repo_dir)
            # The baseline must NOT contain the scaffolded file
            self.assertEqual(baseline_fingerprint["untracked"], {})

            meta = {
                "task_id": "fix-login-1",
                "task_type": "fix",
                "branch": "agent/codex/fix-login",
                "agent": "codex",
            }
            scaffold_res = ensure_compliance_scaffolding(repo_dir, meta)
            self.assertTrue(scaffold_res["scaffolded"])
            scaffolded_path = repo_dir / scaffold_res["path"]
            self.assertTrue(scaffolded_path.exists())

            # Now verify that commit_task stages the untracked scaffolded file
            # Setup tasks.json for herdr-task commit
            tasks_file = tmp / "tasks.json"
            task_obj = {
                "task_id": "fix-login-1",
                "clone_path": str(repo_dir),
                "status": "completed",
                "baseline_commit": base_sha,
                "baseline_fingerprint": baseline_fingerprint,
                "baseline_untracked": list(baseline_fingerprint["untracked"].keys()),
                "delivery_contract": scaffold_res["contract"],
            }
            tasks_file.write_text(json.dumps({"tasks": [task_obj]}), encoding="utf-8")

            # Call herdr-task commit
            proc_commit = subprocess.run(
                [str(cli), "commit", "fix-login-1", "--message", "fix: resolve login bug"],
                capture_output=True,
                text=True,
                env={**os.environ, "TASKS_FILE": str(tasks_file)},
            )
            self.assertEqual(proc_commit.returncode, 0, f"Commit failed: {proc_commit.stderr}\n{proc_commit.stdout}")

            # Verify that the scaffolded doc was committed into Git!
            diff_names = subprocess.run(
                ["git", "-C", str(repo_dir), "diff", "--name-only", "HEAD~1", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip().splitlines()
            self.assertIn(scaffold_res["path"], diff_names)

            # Working tree should now be clean and match baseline fingerprint!
            current_fp = worker_mod.build_baseline_fingerprint(repo_dir)
            self.assertEqual(current_fp["untracked"], {})
            self.assertEqual(current_fp["tracked"], {})


if __name__ == "__main__":
    unittest.main()

