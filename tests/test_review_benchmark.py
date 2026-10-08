"""tests/test_review_benchmark.py

Automated unit tests for herdr/review_benchmark.py and bin/herdr-review-bench.
Does NOT invoke real LLM models or paid APIs.
Covers:
- Input schema validation (findings, candidate PR output, manifest)
- Candidate identity checks & SHA mismatches
- Missing cases in manifest or golden set
- Agent execution failure states (startup failure, timeout, malformed output vs 0 findings)
- Chinese report generation completeness
- Benchmark comparison delta calculations
- End-to-end dry run CLI commands
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr.review_benchmark import (
    compare_benchmarks,
    extract_and_validate_metrics,
    extract_findings_from_response,
    generate_chinese_report,
    pr_key,
    run_agent_review,
    validate_candidate_output,
    validate_finding,
    validate_manifest,
)


class TestReviewBenchmarkValidation(unittest.TestCase):
    def test_pr_key_format(self):
        key = pr_key("https://github.com/allinai0506/HAFlow", 147, "8aa3ca4347f6f4e2f08cc161466dc20cc746212c")
        self.assertEqual(key, "allinai0506_HAFlow_147-8aa3ca43")

    def test_validate_finding_success(self):
        f = {
            "producer": "test-agent",
            "file": "herdr/agent_router.py",
            "start_line": 10,
            "end_line": 20,
            "message": "Potential resource deadlock",
        }
        validate_finding(f)

    def test_validate_finding_failures(self):
        # Missing field
        with self.assertRaises(ValueError):
            validate_finding({"producer": "a", "file": "b", "start_line": 1})

        # Non-positive line number
        with self.assertRaises(ValueError):
            validate_finding({"producer": "a", "file": "b", "start_line": 0, "end_line": 5, "message": "msg"})

        # end_line < start_line
        with self.assertRaises(ValueError):
            validate_finding({"producer": "a", "file": "b", "start_line": 10, "end_line": 5, "message": "msg"})

        # Empty message
        with self.assertRaises(ValueError):
            validate_finding({"producer": "a", "file": "b", "start_line": 10, "end_line": 15, "message": ""})

    def test_validate_candidate_output_success(self):
        candidate = {
            "pr": {
                "repo": "https://github.com/allinai0506/HAFlow",
                "pr_number": 147,
                "base": "79f5bfc00c95c90ebe69260a2b9b5d12645a102a",
                "head": "8aa3ca4347f6f4e2f08cc161466dc20cc746212c",
            },
            "findings": [
                {
                    "producer": "agy",
                    "file": "bin/herdr-task",
                    "start_line": 3140,
                    "end_line": 3150,
                    "message": "stale baseline not validated",
                }
            ],
        }
        validate_candidate_output(candidate)

    def test_validate_candidate_output_invalid_sha(self):
        candidate = {
            "pr": {
                "repo": "https://github.com/allinai0506/HAFlow",
                "pr_number": 147,
                "base": "short",
                "head": "8aa3ca4347f6f4e2f08cc161466dc20cc746212c",
            },
            "findings": [],
        }
        with self.assertRaises(ValueError):
            validate_candidate_output(candidate)

    def test_validate_manifest(self):
        valid = [
            {
                "repo": "https://github.com/allinai0506/HAFlow",
                "pr_number": 147,
                "base": "79f5bfc00c95c90ebe69260a2b9b5d12645a102a",
                "head": "8aa3ca4347f6f4e2f08cc161466dc20cc746212c",
            }
        ]
        validate_manifest(valid)

        with self.assertRaises(ValueError):
            validate_manifest([])


class TestExtractFindings(unittest.TestCase):
    def test_extract_from_json_block(self):
        raw = """Here is my review:
```json
[
  {
    "file": "test.py",
    "start_line": 5,
    "end_line": 10,
    "message": "Bug here"
  }
]
```
Hope this helps!"""
        findings = extract_findings_from_response(raw, "agy")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["file"], "test.py")
        self.assertEqual(findings[0]["producer"], "agy")

    def test_extract_empty_list_as_clean_review(self):
        raw = "No issues found: `[]`"
        findings = extract_findings_from_response(raw, "agy")
        self.assertEqual(findings, [])

    def test_extract_invalid_json_raises(self):
        raw = "I found a bug on line 5 but forgot to output JSON."
        with self.assertRaises(ValueError):
            extract_findings_from_response(raw, "agy")


class TestAgentExecutionFailureModes(unittest.TestCase):
    def test_distinguish_clean_review_from_failure(self):
        # Clean review produces 0 findings successfully
        pr_info = {
            "repo": "https://github.com/allinai0506/HAFlow",
            "pr_number": 100,
            "base": "a" * 40,
            "head": "b" * 40,
            "mock_findings": [],
        }
        out = run_agent_review("mock", Path("/tmp"), pr_info, "diff", timeout_seconds=10)
        self.assertEqual(out["findings"], [])
        self.assertEqual(out["pr"]["pr_number"], 100)

    def test_unsupported_agent_startup_fails(self):
        pr_info = {
            "repo": "https://github.com/allinai0506/HAFlow",
            "pr_number": 100,
            "base": "a" * 40,
            "head": "b" * 40,
        }
        with self.assertRaises(RuntimeError) as ctx:
            run_agent_review("non_existent_agent_binary_xyz", Path("/tmp"), pr_info, "diff")
        self.assertIn("agent_startup_failed", str(ctx.exception))


class TestReportAndComparison(unittest.TestCase):
    def test_chinese_report_generation(self):
        results = {
            "metrics": {
                "overall": {
                    "grounded_precision": 1.0,
                    "grounded_recall": 1.0,
                    "augmented_precision": 1.0,
                    "augmented_recall": 1.0,
                }
            }
        }
        details = [
            {
                "pr_key": "allinai0506_HAFlow_147-8aa3ca43",
                "candidate_findings": [
                    {
                        "file": "bin/herdr-task",
                        "start_line": 3140,
                        "end_line": 3150,
                        "message": "stale baseline not validated",
                        "status": "matched_tp",
                    }
                ],
            }
        ]
        summary = {
            "total_planned": 1,
            "completed": 1,
            "failed": 0,
            "failure_details": {},
            "elapsed_seconds": 12.34,
        }
        version = {
            "code_sha": "abc12345",
            "agent_config": "agy",
            "judge_config": "deepseek/deepseek-v4-flash",
            "scorer_sha": "e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d",
            "cost_usd": "0.01",
        }
        report = generate_chinese_report(results, details, summary, version)
        self.assertIn("HAFlow 代码评审回归评测报告", report)
        self.assertIn("计划案例数**: 1", report)
        self.assertIn("基础召回率 (Grounded Recall)**: 100.0%", report)
        self.assertIn("bin/herdr-task:3140-3150", report)
        self.assertIn("12.34 秒", report)

    def test_compare_benchmarks(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            before_dir = tmp / "before"
            after_dir = tmp / "after"
            before_dir.mkdir()
            after_dir.mkdir()

            before_res = {
                "status": "completed",
                "metrics": {
                    "overall": {
                        "grounded_recall": 0.33,
                        "grounded_precision": 0.50,
                        "augmented_recall": 0.33,
                        "augmented_precision": 0.50,
                    }
                }
            }
            after_res = {
                "status": "completed",
                "metrics": {
                    "overall": {
                        "grounded_recall": 0.67,
                        "grounded_precision": 0.80,
                        "augmented_recall": 0.67,
                        "augmented_precision": 0.80,
                    }
                }
            }

            (before_dir / "results.json").write_text(json.dumps(before_res))
            (after_dir / "results.json").write_text(json.dumps(after_res))
            (before_dir / "report.md").write_text("# Before Report")
            (after_dir / "report.md").write_text("# After Report")

            cmp_file = tmp / "comparison.md"
            comp = compare_benchmarks(before_dir, after_dir, cmp_file)

    def test_compare_benchmarks_refuses_failed_judge(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            before_dir = tmp / "before"
            after_dir = tmp / "after"
            before_dir.mkdir()
            after_dir.mkdir()

            failed_res = {
                "metrics": None,
                "status": "judge_failed",
            }
            ok_res = {
                "metrics": {"overall": {"grounded_recall": 1.0}},
                "status": "completed",
            }
            (before_dir / "results.json").write_text(json.dumps(failed_res))
            (after_dir / "results.json").write_text(json.dumps(ok_res))

            with self.assertRaises(ValueError) as ctx:
                compare_benchmarks(before_dir, after_dir)
            self.assertIn("Refusing to fabricate benchmark regressions", str(ctx.exception))

    def test_extract_and_validate_metrics_official_and_wrapped(self):
        # Official ReviewBench output structure (macro / micro)
        official_data = {
            "macro": {
                "overall": {
                    "grounded_precision": 0.8,
                    "grounded_recall": 0.9,
                    "augmented_precision": 0.85,
                    "augmented_recall": 0.95,
                }
            },
            "micro": {"overall": {}},
            "per_pr": [],
        }
        extracted = extract_and_validate_metrics(official_data)
        self.assertIsNotNone(extracted)
        self.assertEqual(extracted["overall"]["grounded_recall"], 0.9)

        # Wrapped output structure (metrics.overall)
        wrapped_data = {
            "metrics": {
                "overall": {
                    "grounded_precision": 0.8,
                    "grounded_recall": 0.9,
                    "augmented_precision": 0.85,
                    "augmented_recall": 0.95,
                }
            }
        }
        extracted_w = extract_and_validate_metrics(wrapped_data)
        self.assertIsNotNone(extracted_w)
        self.assertEqual(extracted_w["overall"]["grounded_recall"], 0.9)

        # Invalid structure missing required keys
        invalid_data = {"macro": {"overall": {"grounded_recall": 0.9}}}
        self.assertIsNone(extract_and_validate_metrics(invalid_data))

    def test_manifest_is_neutral_and_blind(self):
        manifest_path = HERDR_ROOT / "tests/fixtures/review_benchmark/corpus/manifest.json"
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        # Verify no defect answers, specific bug identifiers or consequence leaks in body
        forbidden_leak_terms = [
            "extract_task_candidate_sha",
            "verdict_fingerprint",
            "decision_identity",
            "plan_identity",
            "然而",
            "漏洞",
            "透支循环预算",
            "破坏了重复判定",
        ]
        for entry in manifest:
            body = entry.get("body", "")
            for term in forbidden_leak_terms:
                self.assertNotIn(
                    term,
                    body,
                    f"Manifest PR #{entry.get('pr_number')} leaks defect answer '{term}' in body",
                )


class TestWorkspaceIsolation(unittest.TestCase):
    def test_isolated_workspace_has_no_future_refs(self):
        from herdr.review_benchmark import prepare_isolated_workspace
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws_path = Path(tmp_dir) / "isolated_ws"
            base_sha = "a957663a38f48a399668c4d6b970f0d82ab885cf"
            head_sha = "3477d064da8541451840959c419b94146b185053"
            _, diff = prepare_isolated_workspace(str(HERDR_ROOT), base_sha, head_sha, ws_path)
            self.assertTrue(len(diff) > 0)

            # Ensure no remote was added
            remotes = subprocess.run(["git", "remote"], cwd=str(ws_path), capture_output=True, text=True).stdout.strip()
            self.assertEqual(remotes, "")

            # Ensure no golden fixture exists in historical checkout
            self.assertFalse((ws_path / "tests" / "fixtures" / "review_benchmark").exists())

            # Ensure cannot query current head commit object
            current_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(HERDR_ROOT), capture_output=True, text=True).stdout.strip()
            check_obj = subprocess.run(["git", "cat-file", "-e", current_head], cwd=str(ws_path))
            self.assertNotEqual(check_obj.returncode, 0)


class TestCLIIntegration(unittest.TestCase):
    def test_cli_help(self):
        res = subprocess.run(
            [sys.executable, str(HERDR_ROOT / "bin" / "herdr-review-bench"), "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("HAFlow Code Review Benchmark Tool", res.stdout)


class TestDatasetV1Integrity(unittest.TestCase):
    def test_dataset_v1_manifest_and_golden_alignment(self):
        manifest_path = HERDR_ROOT / "tests/fixtures/review_benchmark/corpus/manifest.json"
        golden_dir = HERDR_ROOT / "tests/fixtures/review_benchmark/golden"
        meta_dir = HERDR_ROOT / "tests/fixtures/review_benchmark/metadata"

        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        self.assertGreaterEqual(len(manifest), 10, "Dataset V1 must contain at least 10 PR cases")
        self.assertEqual(len(manifest), 10, "Dataset V1 has exactly 10 strictly verified PR cases")

        golden_files = list(golden_dir.glob("*.json"))
        self.assertEqual(len(golden_files), len(manifest), "Golden files count must match manifest count")

        for entry in manifest:
            pr_num = entry["pr_number"]
            base_sha = entry["base"]
            head_sha = entry["head"]
            key = pr_key(entry["repo"], pr_num, head_sha)

            golden_file = golden_dir / f"{key}.json"
            self.assertTrue(golden_file.exists(), f"Missing golden fixture for {key}")

            meta_file = meta_dir / f"{key}.json"
            self.assertTrue(meta_file.exists(), f"Missing metadata file for {key}")

            # Verify base and head commits exist in git
            base_check = subprocess.run(["git", "cat-file", "-e", f"{base_sha}^{{commit}}"], cwd=str(HERDR_ROOT))
            self.assertEqual(base_check.returncode, 0, f"Base commit {base_sha} does not exist in git")

            head_check = subprocess.run(["git", "cat-file", "-e", f"{head_sha}^{{commit}}"], cwd=str(HERDR_ROOT))
            self.assertEqual(head_check.returncode, 0, f"Head commit {head_sha} does not exist in git")

            # Verify PR git diff contains the modified files (B1/B2 blocker check)
            diff_files = subprocess.run(
                ["git", "diff", "--name-only", f"{base_sha}...{head_sha}"],
                cwd=str(HERDR_ROOT),
                capture_output=True,
                text=True,
            ).stdout.splitlines()

            # Verify golden findings point to valid files and lines in head commit
            with open(golden_file, "r", encoding="utf-8") as gf:
                gdata = json.load(gf)

            self.assertEqual(gdata["pr_key"], key)
            self.assertGreaterEqual(len(gdata["findings"]), 1, f"Golden case {key} must have >= 1 defect finding")

            for finding in gdata["findings"]:
                f_path = finding["file"]
                s_line = finding["start_line"]
                e_line = finding["end_line"]

                # Ensure file is actively touched by the PR git diff
                self.assertIn(
                    f_path,
                    diff_files,
                    f"Golden finding file {f_path} is NOT touched in PR diff {base_sha}...{head_sha}!",
                )

                # Ensure file exists in the defect head commit
                file_check = subprocess.run(["git", "cat-file", "-e", f"{head_sha}:{f_path}"], cwd=str(HERDR_ROOT))
                self.assertEqual(file_check.returncode, 0, f"{f_path} does not exist in {head_sha}")

                # Ensure line numbers are within bounds
                content = subprocess.run(["git", "show", f"{head_sha}:{f_path}"], cwd=str(HERDR_ROOT), capture_output=True, text=True).stdout
                total_lines = len(content.splitlines())
                self.assertTrue(1 <= s_line <= e_line <= total_lines, f"Invalid lines {s_line}-{e_line} in {f_path} (total {total_lines})")

                # Verify verified_commit exists
                v_commit = finding.get("source", {}).get("verified_commit")
                if v_commit:
                    vc_check = subprocess.run(["git", "cat-file", "-e", f"{v_commit}^{{commit}}"], cwd=str(HERDR_ROOT))
                    self.assertEqual(vc_check.returncode, 0, f"Verified commit {v_commit} does not exist in git")

    def test_context_budget_hard_limit_and_allocator(self):
        """Verify Budget Allocator guarantees len(assembled) <= max_budget_chars with headers and metadata."""
        from herdr.review_benchmark import retrieve_and_assemble_context
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            # Create several python files with functions
            for i in range(10):
                lines = [f"def function_{i}_{j}():\n    return '{'x'*200}'\n" for j in range(10)]
                (tmp / f"module_{i}.py").write_text("\n".join(lines), encoding="utf-8")

            dummy_pr = {
                "title": "feat: module validation and saving for function",
                "body": "Ensure function validation works",
            }
            # Large diff
            large_diff = "diff --git a/test.py b/test.py\n" + ("+ line of diff\n" * 3000)

            # Test multiple budget thresholds
            for budget in (20000, 25000, 35000):
                assembled, audit = retrieve_and_assemble_context(
                    repo_dir=tmp,
                    pr_info=dummy_pr,
                    diff_content=large_diff,
                    max_budget_chars=budget,
                )
                self.assertLessEqual(len(assembled), budget, f"Assembled context {len(assembled)} exceeded budget {budget}!")
                self.assertEqual(len(assembled), audit["final_context_size"])
                # Ensure each retrieved snippet is accounted for and has valid provenance
                for item in audit["retrieved_context"]:
                    self.assertIn("file", item)
                    self.assertIn("symbol", item)
                    self.assertIn("start_line", item)
                    self.assertIn("end_line", item)
                    self.assertFalse(item.get("partial", False))

    def test_ast_function_spans_and_partial_marking(self):
        """Verify AST extraction captures complete functions and marks partial=True on syntax error."""
        from herdr.review_benchmark import extract_function_spans
        valid_code = (
            "def outer_fn(x):\n"
            "    def inner_fn(y):\n"
            "        return y * 2\n"
            "    return inner_fn(x)\n\n"
            "def second_fn():\n"
            "    pass\n"
        )
        spans = extract_function_spans(valid_code)
        span_dict = {s[0]: s for s in spans}
        self.assertIn("outer_fn", span_dict)
        self.assertIn("inner_fn", span_dict)
        self.assertIn("second_fn", span_dict)
        self.assertEqual(span_dict["outer_fn"][1], 1)
        self.assertEqual(span_dict["outer_fn"][2], 4)
        self.assertFalse(span_dict["outer_fn"][3])  # partial is False

        # Invalid syntax fallback
        invalid_code = "def broken_syntax(:\n    return 42\n"
        spans_inv = extract_function_spans(invalid_code)
        self.assertEqual(len(spans_inv), 1)
        self.assertEqual(spans_inv[0][0], "broken_syntax")
        self.assertTrue(spans_inv[0][3])  # partial is True
        self.assertIn("syntax_fallback", spans_inv[0][4])

    def test_review_diff_official_integration_and_fallback(self):
        """Verify official review integration defaults to rule-contract-context-v1 and supports fallback."""
        from herdr.review_benchmark import review_diff, DEFAULT_REVIEWER, FALLBACK_REVIEWER

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            (tmp / "test.py").write_text("def dummy():\n    pass\n", encoding="utf-8")
            diff = "--- a/test.py\n+++ b/test.py\n@@ -1 +1 @@\n-def dummy():\n+def dummy_updated():\n"

            # 1. Default reviewer is rule-contract-context-v1
            res_default = review_diff(tmp, diff)
            self.assertEqual(res_default["status"], "success")
            self.assertEqual(res_default["agent"], DEFAULT_REVIEWER)
            self.assertFalse(res_default["fallback_used"])
            self.assertIn("audit", res_default)

            # 2. Fallback via explicit agent
            res_fallback_agent = review_diff(tmp, diff, reviewer_agent="rule")
            self.assertEqual(res_fallback_agent["status"], "success")
            self.assertEqual(res_fallback_agent["agent"], FALLBACK_REVIEWER)
            self.assertFalse(res_fallback_agent["fallback_used"])

            # 3. Fallback via environment variable
            os.environ["HERDR_REVIEW_FALLBACK"] = "1"
            try:
                res_env_fallback = review_diff(tmp, diff)
                self.assertEqual(res_env_fallback["status"], "success")
                self.assertEqual(res_env_fallback["agent"], FALLBACK_REVIEWER)
            finally:
                os.environ.pop("HERDR_REVIEW_FALLBACK", None)

            # 4. Shadow mode execution alongside fallback
            res_shadow = review_diff(tmp, diff, reviewer_agent="rule", shadow_mode=True)
            self.assertEqual(res_shadow["status"], "success")
            self.assertEqual(res_shadow["agent"], "rule")
            self.assertIn("shadow", res_shadow)
            self.assertEqual(res_shadow["shadow"]["agent"], DEFAULT_REVIEWER)
            self.assertIn("shadow_persisted_to", res_shadow)
            self.assertTrue(Path(res_shadow["shadow_persisted_to"]).exists())
            persisted_data = json.loads(Path(res_shadow["shadow_persisted_to"]).read_text(encoding="utf-8"))
            self.assertIn("shadow_rejected_findings", persisted_data)
            self.assertIn("shadow_rejected_count", persisted_data)

            # 5. CLI review subcommand sanity
            cmd = [
                sys.executable,
                str(HERDR_ROOT / "bin" / "herdr-review-bench"),
                "review",
                "--repo", str(tmp),
                "--diff", str(tmp / "diff.txt"),
                "--agent", "rule",
            ]
            (tmp / "diff.txt").write_text(diff, encoding="utf-8")
            cli_res = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(cli_res.returncode, 0, f"CLI error: {cli_res.stderr}")
            parsed_out = json.loads(cli_res.stdout)
            self.assertEqual(parsed_out["status"], "success")
            self.assertEqual(parsed_out["agent"], "rule")


if __name__ == "__main__":
    unittest.main()
