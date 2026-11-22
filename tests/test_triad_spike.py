import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.experiments import triad_spike


class TestTriadSpikePureLogic(unittest.TestCase):
    def test_scan_test_tampering_detects_removed_assert_and_skip(self):
        diff = (
            "+++ b/tests/test_sample.py\n"
            "-    assert result == 42\n"
            "+@unittest.skip('flaky')\n"
            "+    pass\n"
        )
        warnings = triad_spike.scan_test_tampering(diff)
        self.assertTrue(any("断言删除" in w for w in warnings))
        self.assertTrue(any("跳过测试" in w for w in warnings))

    def test_evaluate_scope_blocks_out_of_bounds_and_forbidden(self):
        contract = {"allowed_paths": ["herdr/foo.py", "tests/"], "max_changed_lines": 50}
        errors = triad_spike.evaluate_scope(
            ["herdr/foo.py", "herdr/bar.py", "state.db"],
            "10\t5\therdr/foo.py\n60\t0\therdr/bar.py\n",
            contract,
        )
        self.assertTrue(any("herdr/bar.py" in e for e in errors))
        self.assertTrue(any("state.db" in e for e in errors))
        self.assertTrue(any("超过上限" in e for e in errors))

    def test_normalize_review_downgrades_p0_without_evidence_to_p1(self):
        raw = {
            "verdict": "block",
            "findings": [
                {
                    "severity": "P0",
                    "file": "",
                    "line": 0,
                    "claim": "looks wrong",
                    "evidence": "",
                    "contract_clause": "goal",
                }
            ],
        }
        norm = triad_spike.normalize_review_output(raw)
        self.assertEqual(norm["verdict"], "pass")
        self.assertEqual(norm["findings"][0]["severity"], "P1")

    def test_normalize_review_keeps_grounded_p0_and_blocks(self):
        raw = {
            "verdict": "pass",
            "findings": [
                {
                    "severity": "P0",
                    "file": "herdr/foo.py",
                    "line": 12,
                    "claim": "null deref",
                    "evidence": "x.y when x is None",
                    "contract_clause": "acceptance",
                }
            ],
        }
        norm = triad_spike.normalize_review_output(raw)
        self.assertEqual(norm["verdict"], "block")
        self.assertEqual(norm["findings"][0]["severity"], "P0")

    def test_decide_next_action_convergence(self):
        self.assertEqual(triad_spike.decide_next_action(False, None, 1), "retry_executor_gate")
        self.assertEqual(triad_spike.decide_next_action(True, {"verdict": "pass", "findings": []}, 1), "approve_pr")
        self.assertEqual(triad_spike.decide_next_action(True, {"verdict": "block", "findings": []}, 1, 2), "retry_executor_review")
        self.assertEqual(triad_spike.decide_next_action(True, {"verdict": "block", "findings": []}, 2, 2), "escalate_human")

    def test_rejects_same_model_self_review(self):
        rc = triad_spike.main(["--repo", ".", "--task", "x", "--executor", "claude", "--reviewer", "claude"])
        self.assertEqual(rc, 2)


class TestTriadSpikeEndToEndMocked(unittest.TestCase):
    def test_end_to_end_gate_then_review_loop_on_real_git_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
            (repo / "test_app.py").write_text("from app import add\nassert add(2, 3) == 5\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)

            contract_file = root / "contract.json"
            contract_file.write_text(json.dumps({
                "goal": "fix add",
                "non_goals": [],
                "allowed_paths": ["app.py"],
                "max_changed_lines": 20,
                "acceptance_cmd": "python3 test_app.py",
            }), encoding="utf-8")

            exec_calls = []

            def fake_executor(agent, prompt, cwd, timeout):
                exec_calls.append(len(exec_calls) + 1)
                (cwd / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
                return triad_spike.CmdResult(0, "done", "")

            review_calls = []

            def fake_structured(agent, prompt, schema, cwd, read_only, timeout):
                review_calls.append(len(review_calls) + 1)
                if len(review_calls) == 1:
                    return {
                        "verdict": "block",
                        "findings": [{
                            "severity": "P0",
                            "file": "app.py",
                            "line": 2,
                            "claim": "needs comment",
                            "evidence": "return a + b",
                            "contract_clause": "goal",
                        }],
                    }
                return {"verdict": "pass", "findings": []}

            runs_dir = root / "runs"
            with patch.object(triad_spike, "call_executor_agent", side_effect=fake_executor), \
                 patch.object(triad_spike, "call_structured_agent", side_effect=fake_structured):
                rc = triad_spike.main([
                    "--repo", str(repo),
                    "--task", "fix add",
                    "--contract", str(contract_file),
                    "--runs-dir", str(runs_dir),
                    "--skip-confirm",
                ])

            self.assertEqual(rc, 0)
            self.assertEqual(len(exec_calls), 2)
            self.assertEqual(len(review_calls), 2)
            run_dirs = list(runs_dir.iterdir())
            self.assertEqual(len(run_dirs), 1)
            summary = (run_dirs[0] / "summary.md").read_text(encoding="utf-8")
            self.assertIn("`approved`", summary)
            self.assertTrue((run_dirs[0] / "single_agent_snapshot.diff").exists())
            self.assertTrue((run_dirs[0] / "final.diff").exists())


if __name__ == "__main__":
    unittest.main()
