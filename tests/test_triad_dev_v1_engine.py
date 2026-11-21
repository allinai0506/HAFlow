import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from herdr import agent_router, pr_delivery, tool_gate
from herdr.stage_skills import is_staged_dev_flow_workflow
from herdr.state_store import SQLiteStateStore
from herdr.workflow import load_template, validate_workflow_dag
from services.controller import advancement, stage_delivery


class TestTriadDevV1Engine(unittest.TestCase):
    def test_triad_template_loads_and_validates(self):
        tmpl_path = Path(__file__).resolve().parents[1] / "workflow_templates" / "triad-dev-v1.yaml"
        tmpl = load_template(str(tmpl_path))
        validate_workflow_dag(tmpl["nodes"])
        self.assertEqual([n["id"] for n in tmpl["nodes"]], ["executor", "gate", "review"])
        self.assertIs(tmpl.get("coordinator_required"), False)
        self.assertTrue(is_staged_dev_flow_workflow(tmpl))
        self.assertEqual(tmpl.get("candidate_node"), "executor")
        self.assertTrue(tmpl.get("direct_pr_delivery"))
        self.assertTrue(tmpl.get("require_heterogeneous"))
        self.assertEqual(advancement._resolve_candidate_node(tmpl), "executor")

    def test_resolve_candidate_node_backwards_compatible(self):
        self.assertEqual(advancement._resolve_candidate_node({"nodes": [{"id": "implementation"}]}), "implementation")
        self.assertEqual(
            advancement._resolve_candidate_node({"nodes": [{"id": "craft", "default_integration_mode": "git"}]}),
            "craft",
        )

    def test_heterogeneous_preflight_requires_two_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            pools_file = Path(tmp) / "pools.json"
            pools_file.write_text(json.dumps({
                "projects": {
                    "p1": {"allowed_agents": ["claude"], "disabled_agents": []},
                    "p2": {"allowed_agents": ["claude", "codex"], "disabled_agents": []},
                }
            }), encoding="utf-8")
            with patch.object(agent_router, "POOLS_FILE", pools_file):
                ok1, _ = agent_router.has_heterogeneous_candidates({"require_heterogeneous": True}, "p1")
                ok2, _ = agent_router.has_heterogeneous_candidates({"require_heterogeneous": True}, "p2")
                self.assertFalse(ok1)
                self.assertTrue(ok2)

    def test_tool_gate_runs_and_records_task_on_real_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)

            subprocess.run(["git", "checkout", "-b", "feat"], cwd=repo, check=True, stdout=subprocess.PIPE)
            (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "update"], cwd=repo, check=True, stdout=subprocess.PIPE)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

            store = SQLiteStateStore(root / "state.db")
            store.save_workflow({"workflow_id": "wf-tool-1", "project_id": "p1", "status": "running", "candidate_sha": sha})

            res = tool_gate.run_tool_gate(
                store,
                "wf-tool-1",
                {"id": "gate"},
                project_root=str(repo),
                candidate_sha=sha,
                base_branch="main",
                verify_commands=["python3 -c 'import app; assert app.x == 2'"],
            )
            self.assertEqual(res["verdict"], "pass")
            saved = store.get_task(res["task_id"])
            self.assertEqual(saved["stage_verdict"], "pass")
            self.assertEqual(saved["role"], "test")
            self.assertEqual(saved["candidate_sha"], sha)
            from herdr.business_gate import business_gates_blockers
            self.assertEqual(business_gates_blockers(store, store.get_workflow("wf-tool-1"), [saved], {"gate"}), [])

    def test_stage_delivery_dispatches_tool_node_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStateStore(Path(tmp) / "state.db")
            store.save_workflow({"workflow_id": "wf-tool-2", "project_id": "p1", "status": "running", "candidate_sha": "a" * 40})
            ports = SimpleNamespace(
                _get_store=lambda: store,
                workflow_config_for=lambda wid: {"nodes": [{"id": "gate", "node_type": "tool"}]},
                find_node=lambda cfg, nid: next((n for n in cfg["nodes"] if n["id"] == nid), None),
                project_for_workflow=lambda wid: {"project_root": tmp, "base_branch": "main"},
                clear_stage_advance=MagicMock(),
                check_workflow_stage_advance=MagicMock(),
            )
            with patch("herdr.tool_gate.run_tool_gate", return_value={"task_id": "t-g", "verdict": "pass"}) as mock_gate:
                stage_delivery._handle_stage_advance_item(ports, {"workflow_id": "wf-tool-2", "node_id": "gate"})
            mock_gate.assert_called_once()
            ports.clear_stage_advance.assert_called_once_with("wf-tool-2", "gate")
            ports.check_workflow_stage_advance.assert_called_once_with("wf-tool-2")

    def test_publish_direct_workflow_pr_requires_heterogeneous_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "a.txt").write_text("1\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
            branch = "herdr/integration-wf-pr-executor"
            subprocess.run(["git", "branch", branch, sha], cwd=repo, check=True)
            subprocess.run(["git", "update-ref", "refs/herdr/tasks/wf-pr-executor", sha], cwd=repo, check=True)
            subprocess.run(["git", "remote", "add", "origin", "https://github.com/org/repo.git"], cwd=repo, check=True)

            store = SQLiteStateStore(root / "state.db")
            store.save_workflow({"workflow_id": "wf-pr", "project_id": "p1", "status": "running", "config": {"candidate_node": "executor"}})
            store.save_task({
                "task_id": "wf-pr-executor",
                "workflow_id": "wf-pr",
                "node": "executor",
                "agent": "claude",
                "status": "integrated",
                "integrated_commit": sha,
                "integration_branch": branch,
                "integration_ref": "refs/herdr/tasks/wf-pr-executor",
                "source_repo": str(repo),
                "base_branch": "main",
            })
            store.save_task({
                "task_id": "wf-pr-gate",
                "workflow_id": "wf-pr",
                "node": "gate",
                "role": "test",
                "agent": "tool",
                "status": "completed",
                "stage_verdict": "pass",
                "candidate_sha": sha,
            })
            store.save_task({
                "task_id": "wf-pr-review",
                "workflow_id": "wf-pr",
                "node": "review",
                "role": "review",
                "agent": "codex",
                "status": "completed",
                "stage_verdict": "pass",
                "candidate_sha": sha,
            })

            fake_client = MagicMock()
            fake_client.list_open.return_value = []
            fake_client.create.return_value = {
                "number": 99,
                "html_url": "https://github.com/org/repo/pull/99",
                "state": "open",
                "head": {"ref": branch, "sha": sha},
                "base": {"ref": "main"},
            }
            with patch.object(pr_delivery, "_git", side_effect=lambda r, *a: "" if a and a[0] in ("ls-remote", "push") else subprocess.run(["git", *a], cwd=r, check=True, capture_output=True, text=True).stdout.strip()):
                receipt = pr_delivery.publish_direct_workflow_pr(store, "wf-pr", title="triad test", client=fake_client)
            self.assertEqual(receipt["number"], 99)
            self.assertEqual(receipt["candidate_sha"], sha)


if __name__ == "__main__":
    unittest.main()
