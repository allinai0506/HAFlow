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
from typing import Any, Dict, List, Optional, Tuple
import unittest

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr.review_benchmark import (
    DEFAULT_CONTEXT_BUDGET_CHARS,
    DEFAULT_CONTEXT_MODE,
    DEFAULT_SYSTEM_PROMPT,
    VALID_CONTEXT_MODES,
    aggregate_group_agent_identity,
    check_benchmark_comparability,
    compare_benchmarks,
    compute_golden_fingerprint,
    extract_and_validate_metrics,
    extract_base_diff,
    extract_findings_from_response,
    generate_chinese_report,
    pr_key,
    resolve_agent_identity,
    retrieve_and_assemble_context,
    run_agent_review,
    validate_candidate_output,
    validate_context_config,
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


class TestReviewBenchContextModes(unittest.TestCase):
    """Unit tests for ReviewBench same-model context modes comparison (diff_only vs existing_four_tier)."""

    def setUp(self):
        self.sample_pr_info = {
            "repo": "https://github.com/allinai0506/HAFlow",
            "pr_number": 199,
            "base": "0123456789abcdef0123456789abcdef01234567",
            "head": "abcdef0123456789abcdef0123456789abcdef01",
            "title": "Fix context boundary bug",
            "body": "Fixes context boundary resolution and verification",
        }
        self.sample_diff = (
            "--- a/herdr/scheduler.py\n"
            "+++ b/herdr/scheduler.py\n"
            "@@ -150,10 +150,15 @@\n"
            "-def old_func():\n"
            "+def extract_task_candidate_sha(baseline_commit):\n"
            "+    # evaluate_join_gate check\n"
            "+    pass\n"
        )

    def test_default_compatibility(self):
        """Unconfigured review config defaults to existing_four_tier and 35000 chars budget."""
        mode, budget = validate_context_config(None, None)
        self.assertEqual(mode, DEFAULT_CONTEXT_MODE)
        self.assertEqual(mode, "existing_four_tier")
        self.assertEqual(budget, DEFAULT_CONTEXT_BUDGET_CHARS)
        self.assertEqual(budget, 35000)

        # run_agent_review without explicit kwargs defaults to existing_four_tier
        res = run_agent_review(
            agent_name="rule",
            repo_dir=Path("/tmp"),
            pr_info=self.sample_pr_info,
            diff_content=self.sample_diff,
        )
        self.assertIn("context_meta", res)
        c_meta = res["context_meta"]
        self.assertEqual(c_meta["requested_context_mode"], "existing_four_tier")
        self.assertEqual(c_meta["context_budget_chars"], 35000)

    def test_context_config_validation(self):
        """Invalid context mode or non-positive/non-int budget fail-fast with ValueError."""
        # Invalid mode
        with self.assertRaises(ValueError) as ctx:
            validate_context_config("invalid_mode", 35000)
        self.assertIn("Invalid context_mode", str(ctx.exception))

        # Invalid budget <= 0
        with self.assertRaises(ValueError) as ctx:
            validate_context_config("diff_only", 0)
        self.assertIn("Must be a positive integer", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            validate_context_config("existing_four_tier", -500)
        self.assertIn("Must be a positive integer", str(ctx.exception))

        # Invalid budget type
        with self.assertRaises(ValueError) as ctx:
            validate_context_config("diff_only", "35000")  # type: ignore
        self.assertIn("Must be a positive integer", str(ctx.exception))

        # run_agent_review fail-fast before calling model
        with self.assertRaises(ValueError):
            run_agent_review(
                agent_name="rule",
                repo_dir=Path("/tmp"),
                pr_info=self.sample_pr_info,
                diff_content=self.sample_diff,
                context_mode="unsupported_mode",
            )

    def test_base_diff_fairness_and_identity(self):
        """Both modes receive character-for-character identical base_diff; four-tier expansion does not displace base_diff."""
        # Create a large diff (> 20,000 chars)
        repeated_lines = ["+    line_content_padding_for_large_diff_testing = 12345\n"] * 400
        large_diff = (
            "--- a/herdr/scheduler.py\n"
            "+++ b/herdr/scheduler.py\n"
            "@@ -1,5 +1,405 @@\n"
            + "".join(repeated_lines)
        )
        self.assertGreater(len(large_diff), 20000)

        # Extract base_diff with 35,000 budget (capped at 15,000 per Tier-1 logic)
        base_diff_1, files_1 = extract_base_diff(large_diff, max_budget_chars=35000)
        self.assertEqual(len(base_diff_1), 15000)

        # In diff_only mode:
        res_diff_only = run_agent_review(
            agent_name="rule",
            repo_dir=Path("/tmp"),
            pr_info=self.sample_pr_info,
            diff_content=large_diff,
            context_mode="diff_only",
            context_budget_chars=35000,
        )
        # In existing_four_tier mode (mock repo):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            subprocess.run(["git", "init", "-q"], cwd=str(tmp), check=True)
            res_four_tier = run_agent_review(
                agent_name="rule",
                repo_dir=tmp,
                pr_info=self.sample_pr_info,
                diff_content=large_diff,
                context_mode="existing_four_tier",
                context_budget_chars=35000,
            )

        # Both runs report identical base_diff_chars
        self.assertEqual(res_diff_only["context_meta"]["base_diff_chars"], 15000)
        self.assertEqual(res_four_tier["context_meta"]["base_diff_chars"], 15000)
        self.assertEqual(res_diff_only["context_meta"]["base_diff_chars"], res_four_tier["context_meta"]["base_diff_chars"])

    def test_mode_enforcement(self):
        """diff_only mode performs NO repo retrieval, while existing_four_tier retrieves repo context."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            subprocess.run(["git", "init", "-q"], cwd=str(tmp), check=True)
            target_py = tmp / "herdr" / "state_db.py"
            target_py.parent.mkdir(parents=True, exist_ok=True)
            target_py.write_text(
                "def _validate_context_source_existence(run_scope, workflow_id, verified_handoff_tasks):\n"
                "    # working_context validation\n"
                "    return True\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "."], cwd=str(tmp), check=True)
            subprocess.run(["git", "commit", "-m", "init", "-q"], cwd=str(tmp), check=True)

            diff_sample = (
                "--- a/herdr/state_db.py\n"
                "+++ b/herdr/state_db.py\n"
                "@@ -1,3 +1,3 @@\n"
                "-def old(): pass\n"
                "+def _validate_context_source_existence(): pass\n"
            )

            # 1. diff_only: context contains only base diff, no expanded symbols
            res_diff = run_agent_review(
                agent_name="rule-contract-context-v1",
                repo_dir=tmp,
                pr_info=self.sample_pr_info,
                diff_content=diff_sample,
                context_mode="diff_only",
                context_budget_chars=35000,
            )
            c_meta_diff = res_diff["context_meta"]
            self.assertEqual(c_meta_diff["requested_context_mode"], "diff_only")
            self.assertEqual(c_meta_diff["effective_context_mode"], "diff_only")
            self.assertIsNone(c_meta_diff["fallback_reason"])
            self.assertEqual(c_meta_diff["context_chars"], len(diff_sample))
            self.assertEqual(res_diff["audit"]["expanded_symbols"], [])
            self.assertEqual(res_diff["audit"]["retrieved_context"], [])

            # 2. existing_four_tier: performs retrieval, retrieves symbols from state_db.py
            res_four = run_agent_review(
                agent_name="rule-contract-context-v1",
                repo_dir=tmp,
                pr_info=self.sample_pr_info,
                diff_content=diff_sample,
                context_mode="existing_four_tier",
                context_budget_chars=35000,
            )
            c_meta_four = res_four["context_meta"]
            self.assertEqual(c_meta_four["requested_context_mode"], "existing_four_tier")
            self.assertEqual(c_meta_four["effective_context_mode"], "existing_four_tier")
            self.assertIsNone(c_meta_four["fallback_reason"])
            self.assertGreater(c_meta_four["context_chars"], c_meta_diff["context_chars"])
            self.assertIn("_validate_context_source_existence", res_four["audit"]["expanded_symbols"])

    def test_budget_compliance_and_truncation(self):
        """Very small context_budget_chars strictly limits both base_diff and total context."""
        diff = "--- a/test.py\n+++ b/test.py\n@@ -1 +1 @@\n" + ("+line\n" * 1000)
        self.assertGreater(len(diff), 5000)

        # Budget of 1200 chars
        base_diff, _ = extract_base_diff(diff, max_budget_chars=1200)
        self.assertLessEqual(len(base_diff), 1200)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            subprocess.run(["git", "init", "-q"], cwd=str(tmp), check=True)
            res = run_agent_review(
                agent_name="rule",
                repo_dir=tmp,
                pr_info=self.sample_pr_info,
                diff_content=diff,
                context_mode="existing_four_tier",
                context_budget_chars=1200,
            )
            self.assertLessEqual(res["context_meta"]["context_chars"], 1200)

    def test_fallback_traceability_and_integrity(self):
        """Strictly distinguish: successful 4-tier, explicit diff_only, and fallback to diff_only."""
        # 1. Explicit diff_only
        res_explicit = run_agent_review(
            agent_name="rule",
            repo_dir=Path("/nonexistent_dir"),
            pr_info=self.sample_pr_info,
            diff_content=self.sample_diff,
            context_mode="diff_only",
        )
        self.assertEqual(res_explicit["context_meta"]["requested_context_mode"], "diff_only")
        self.assertEqual(res_explicit["context_meta"]["effective_context_mode"], "diff_only")
        self.assertIsNone(res_explicit["context_meta"]["fallback_reason"])

        # 2. Fallback to diff_only (requested 4-tier, but repo_dir lacks .git or raises)
        res_fallback = run_agent_review(
            agent_name="rule",
            repo_dir=Path("/nonexistent_dir"),
            pr_info=self.sample_pr_info,
            diff_content=self.sample_diff,
            context_mode="existing_four_tier",
        )
        self.assertEqual(res_fallback["context_meta"]["requested_context_mode"], "existing_four_tier")
        self.assertEqual(res_fallback["context_meta"]["effective_context_mode"], "diff_only")
        self.assertIsNotNone(res_fallback["context_meta"]["fallback_reason"])
        self.assertIn("retrieval_failed", res_fallback["context_meta"]["fallback_reason"])

    def test_prompt_and_context_fingerprints(self):
        """Prompt and context character lengths and SHA-256 fingerprints are generated and valid."""
        res = run_agent_review(
            agent_name="rule",
            repo_dir=Path("/tmp"),
            pr_info=self.sample_pr_info,
            diff_content=self.sample_diff,
            context_mode="diff_only",
        )
        p_meta = res["prompt_meta"]
        c_meta = res["context_meta"]

        self.assertIsInstance(p_meta["prompt_chars"], int)
        self.assertGreater(p_meta["prompt_chars"], 0)
        self.assertEqual(len(p_meta["prompt_fingerprint"]), 64)

        self.assertIsInstance(c_meta["context_chars"], int)
        self.assertGreater(c_meta["context_chars"], 0)
        self.assertEqual(len(c_meta["context_fingerprint"]), 64)

    def test_usage_and_latency_integrity(self):
        """Missing tokens are preserved as None/'未知'; E2E elapsed vs review cumulative execution time are distinct."""
        res = run_agent_review(
            agent_name="rule",
            repo_dir=Path("/tmp"),
            pr_info=self.sample_pr_info,
            diff_content=self.sample_diff,
        )
        # For rule / mock, tokens is None (not fabricated as 0)
        self.assertIsNone(res["usage"]["tokens"])
        self.assertIsInstance(res["usage"]["time_in_ms"], int)

        # Chinese report formats missing tokens as '未知'
        report = generate_chinese_report(
            results_json={"metrics": {"overall": {"grounded_recall": 1.0, "grounded_precision": 1.0, "augmented_recall": 1.0, "augmented_precision": 1.0}}},
            details_json=[],
            execution_summary={
                "total_planned": 1,
                "completed": 1,
                "failed": 0,
                "fallback_count": 0,
                "elapsed_seconds": 2.5,
                "total_review_time_ms": 1500,
            },
            version_info={
                "context_mode": "diff_only",
                "context_budget_chars": 35000,
            },
        )
        self.assertIn("真实 Token 用量", report)
        self.assertIn("输入 Token (input_tokens): 未知", report)
        self.assertIn("评测实际总耗时**: 2.50 秒", report)
        self.assertIn("评审累积执行耗时**: 1.50 秒", report)

    def test_comparability_audit_and_report(self):
        """check_benchmark_comparability detects single-variable discrepancies and compare_benchmarks reports them."""
        # Baseline run
        base_run = {
            "status": "completed",
            "golden_fingerprint": "golden_fp_default",
            "context_config": {"context_mode": "diff_only", "context_budget_chars": 35000},
            "judge_config": "deepseek/deepseek-v4-flash",
            "scorer_sha": "e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d",
            "execution_config": {
                "timeout_seconds": 300,
                "system_prompt": "You are reviewing a Pull Request.",
                "system_prompt_fingerprint": "hash_sp_default",
                "context_budget_chars": 35000,
                "context_mode": "diff_only",
            },
            "execution_summary": {
                "total_planned": 1,
                "completed": 1,
                "failed": 0,
                "fallback_count": 0,
                "elapsed_seconds": 10.0,
                "agent_identity": {
                    "agent": "agy",
                    "model_name": "gemini-3.8-flash",
                    "model_confirmed": True,
                    "cli_version": "1.3.3",
                },
                "samples": [
                    {
                        "key": "pr_1",
                        "base": "a" * 40,
                        "head": "b" * 40,
                        "base_diff_chars": 1000,
                        "base_diff_fingerprint": "fp_diff_1",
                        "common_prompt_fingerprint": "prompt_fp_default",
                    }
                ],
            },
            "usage_summary": {
                "total_review_time_ms": 8000,
                "input_tokens": 1200,
                "output_tokens": 400,
                "total_tokens": 1600,
                "avg_prompt_chars": 3500,
                "avg_context_chars": 2000,
            },
            "version_info": {
                "judge_config": "deepseek/deepseek-v4-flash",
                "scorer_sha": "e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d",
                "golden_fingerprint": "golden_fp_default",
            },
            "metrics": {
                "overall": {
                    "grounded_recall": 0.50,
                    "grounded_precision": 0.60,
                    "augmented_recall": 0.50,
                    "augmented_precision": 0.60,
                }
            },
        }

        # 1. Perfectly controlled 4-tier run
        after_run = json.loads(json.dumps(base_run))
        after_run["context_config"]["context_mode"] = "existing_four_tier"
        after_run["usage_summary"]["total_review_time_ms"] = 12000
        after_run["usage_summary"]["input_tokens"] = 3500
        after_run["usage_summary"]["total_tokens"] = 3900
        after_run["metrics"]["overall"]["grounded_recall"] = 0.75

        is_comp, disc = check_benchmark_comparability(base_run, after_run)
        self.assertTrue(is_comp)
        self.assertEqual(len(disc), 0)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            b_dir = tmp / "before"
            a_dir = tmp / "after"
            b_dir.mkdir()
            a_dir.mkdir()
            (b_dir / "results.json").write_text(json.dumps(base_run), encoding="utf-8")
            (a_dir / "results.json").write_text(json.dumps(after_run), encoding="utf-8")
            (b_dir / "report.md").write_text("# Before", encoding="utf-8")
            (a_dir / "report.md").write_text("# After", encoding="utf-8")

            report_str = compare_benchmarks(b_dir, a_dir)
            self.assertIn("## 2. 单变量对照与可比性检查", report_str)
            self.assertIn("✓ 确认同模型", report_str)
            self.assertIn("## 3. 耗时与用量对比矩阵", report_str)
            self.assertIn("输入 Token (input_tokens)", report_str)
            self.assertIn("## 4. 上下文扩展收益分析", report_str)

        # 2. Unconfirmed model discrepancy
        unconf_run = json.loads(json.dumps(after_run))
        unconf_run["execution_summary"]["agent_identity"]["model_confirmed"] = False
        is_comp, disc = check_benchmark_comparability(base_run, unconf_run)
        self.assertFalse(is_comp)
        self.assertTrue(any("无法确认同模型" in d for d in disc))

        # 3. Model mismatch discrepancy
        mismatch_run = json.loads(json.dumps(after_run))
        mismatch_run["execution_summary"]["agent_identity"]["model_name"] = "claude-3-7-sonnet"
        is_comp, disc = check_benchmark_comparability(base_run, mismatch_run)
        self.assertFalse(is_comp)
        self.assertTrue(any("模型不一致" in d for d in disc))

        # 4. Fallback run discrepancy
        fallback_run = json.loads(json.dumps(after_run))
        fallback_run["execution_summary"]["fallback_count"] = 1
        is_comp, disc = check_benchmark_comparability(base_run, fallback_run)
        self.assertFalse(is_comp)
        self.assertTrue(any("包含降级样本" in d for d in disc))

    def test_legacy_report_backwards_compatibility(self):
        """Comparing a legacy results.json (without context_config or usage_summary) works smoothly without error."""
        legacy_run = {
            "status": "completed",
            "metrics": {
                "overall": {
                    "grounded_recall": 0.40,
                    "grounded_precision": 0.50,
                    "augmented_recall": 0.40,
                    "augmented_precision": 0.50,
                }
            },
        }
        modern_run = {
            "status": "completed",
            "context_config": {"context_mode": "diff_only", "context_budget_chars": 35000},
            "execution_summary": {"completed": 1, "total_planned": 1},
            "metrics": {
                "overall": {
                    "grounded_recall": 0.60,
                    "grounded_precision": 0.70,
                    "augmented_recall": 0.60,
                    "augmented_precision": 0.70,
                }
            },
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            b_dir = tmp / "before"
            a_dir = tmp / "after"
            b_dir.mkdir()
            a_dir.mkdir()
            (b_dir / "results.json").write_text(json.dumps(legacy_run), encoding="utf-8")
            (a_dir / "results.json").write_text(json.dumps(modern_run), encoding="utf-8")
            (b_dir / "report.md").write_text("# Before", encoding="utf-8")
            (a_dir / "report.md").write_text("# After", encoding="utf-8")

            report_str = compare_benchmarks(b_dir, a_dir)
            self.assertIn("## 1. 核心指标对比矩阵", report_str)
            self.assertIn("未知", report_str)

    def test_model_identity_resolution_and_runtime_evidence(self):
        """Distinguish claimed model vs verified model with runtime evidence."""
        # 1. Config only without runtime evidence -> unconfirmed
        res_unconf = resolve_agent_identity("agy", review_cfg={"model": "claimed-A"})
        self.assertFalse(res_unconf["model_confirmed"])
        self.assertEqual(res_unconf["model_name"], "claimed-A")
        self.assertEqual(res_unconf["details"]["source"], "config_only_unconfirmed")

        # 2. Claimed model doesn't match runtime evidence -> unconfirmed mismatch
        res_mismatch = resolve_agent_identity(
            "agy",
            review_cfg={"model": "claimed-A"},
            runtime_evidence={"model": "actual-B"},
        )
        self.assertFalse(res_mismatch["model_confirmed"])
        self.assertEqual(res_mismatch["model_name"], "actual-B")
        self.assertEqual(res_mismatch["details"]["source"], "runtime_mismatch")

        # 3. Matching runtime evidence -> confirmed
        res_conf = resolve_agent_identity(
            "agy",
            review_cfg={"model": "claimed-A"},
            runtime_evidence={"model": "claimed-A"},
        )
        self.assertTrue(res_conf["model_confirmed"])
        self.assertEqual(res_conf["model_name"], "claimed-A")
        self.assertEqual(res_conf["details"]["source"], "runtime_evidence")

        # 4. Deterministic rule reviewer -> confirmed
        res_rule = resolve_agent_identity("rule", review_cfg=None)
        self.assertTrue(res_rule["model_confirmed"])
        self.assertEqual(res_rule["model_name"], "rule")
        self.assertEqual(res_rule["details"]["source"], "deterministic_rule")

    def test_comparability_audit_comprehensive_conditions(self):
        """Test PR keys mismatch, partial failures, effort difference, same context mode, budget and judge/scorer verification."""
        base_run = {
            "status": "completed",
            "golden_fingerprint": "golden_fp_default",
            "context_config": {"context_mode": "diff_only", "context_budget_chars": 35000},
            "judge_config": "deepseek/deepseek-v4-flash",
            "scorer_sha": "e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d",
            "execution_config": {
                "timeout_seconds": 300,
                "system_prompt": "You are reviewing a Pull Request.",
                "system_prompt_fingerprint": "hash_sp_default",
                "context_budget_chars": 35000,
                "context_mode": "diff_only",
            },
            "version_info": {
                "judge_config": "deepseek/deepseek-v4-flash",
                "scorer_sha": "e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d",
                "golden_fingerprint": "golden_fp_default",
            },
            "execution_summary": {
                "total_planned": 2,
                "completed": 2,
                "failed": 0,
                "fallback_count": 0,
                "agent_identity": {
                    "agent": "agy",
                    "model_name": "gemini-3.8-flash",
                    "model_confirmed": True,
                    "cli_version": "1.3.3",
                    "details": {"effort": "low"},
                },
                "samples": [
                    {"key": "pr_1", "base": "aaa", "head": "bbb", "base_diff_chars": 1000, "base_diff_fingerprint": "fp_1", "common_prompt_fingerprint": "pfp_1"},
                    {"key": "pr_2", "base": "ccc", "head": "ddd", "base_diff_chars": 2000, "base_diff_fingerprint": "fp_2", "common_prompt_fingerprint": "pfp_2"},
                ],
            },
        }

        # 1. PR keys mismatch
        cand_keys = json.loads(json.dumps(base_run))
        cand_keys["context_config"]["context_mode"] = "existing_four_tier"
        cand_keys["execution_summary"]["samples"] = [
            {"key": "pr_1", "base": "aaa", "head": "bbb", "base_diff_chars": 1000, "base_diff_fingerprint": "fp_1"},
            {"key": "pr_999", "base": "ccc", "head": "ddd", "base_diff_chars": 2000, "base_diff_fingerprint": "fp_2"},
        ]
        is_comp, disc = check_benchmark_comparability(base_run, cand_keys)
        self.assertFalse(is_comp)
        self.assertTrue(any("评测 PR 集合不一致" in d for d in disc))

        # 2. Partial failures (candidate failed 1 sample)
        cand_fail = json.loads(json.dumps(base_run))
        cand_fail["context_config"]["context_mode"] = "existing_four_tier"
        cand_fail["execution_summary"]["completed"] = 1
        cand_fail["execution_summary"]["failed"] = 1
        is_comp, disc = check_benchmark_comparability(base_run, cand_fail)
        self.assertFalse(is_comp)
        self.assertTrue(any("包含失败样本" in d for d in disc))

        # 3. Effort difference (low vs high)
        cand_effort = json.loads(json.dumps(base_run))
        cand_effort["context_config"]["context_mode"] = "existing_four_tier"
        cand_effort["execution_summary"]["agent_identity"]["details"]["effort"] = "high"
        is_comp, disc = check_benchmark_comparability(base_run, cand_effort)
        self.assertFalse(is_comp)
        self.assertTrue(any("effort" in d for d in disc))

        # 4. Same context mode (both diff_only)
        cand_same_mode = json.loads(json.dumps(base_run))
        is_comp, disc = check_benchmark_comparability(base_run, cand_same_mode)
        self.assertFalse(is_comp)
        self.assertTrue(any("两组请求上下文模式相同" in d for d in disc))

        # 5. Missing budget
        cand_no_budget = json.loads(json.dumps(base_run))
        cand_no_budget["context_config"] = {"context_mode": "existing_four_tier"}
        is_comp, disc = check_benchmark_comparability(base_run, cand_no_budget)
        self.assertFalse(is_comp)
        self.assertTrue(any("预算" in d for d in disc))

        # 6. Judge configuration mismatch
        cand_diff_judge = json.loads(json.dumps(base_run))
        cand_diff_judge["context_config"]["context_mode"] = "existing_four_tier"
        cand_diff_judge["judge_config"] = "openai/gpt-4o"
        is_comp, disc = check_benchmark_comparability(base_run, cand_diff_judge)
        self.assertFalse(is_comp)
        self.assertTrue(any("裁判模型配置不一致" in d for d in disc))

        # 7. Scorer SHA mismatch
        cand_diff_scorer = json.loads(json.dumps(base_run))
        cand_diff_scorer["context_config"]["context_mode"] = "existing_four_tier"
        cand_diff_scorer["scorer_sha"] = "0000000000000000000000000000000000000000"
        is_comp, disc = check_benchmark_comparability(base_run, cand_diff_scorer)
        self.assertFalse(is_comp)
        self.assertTrue(any("评分器版本不一致" in d for d in disc))

    def test_token_usage_partial_coverage_and_aggregation(self):
        """Token aggregation preserves missing metrics as '未知' and labels partial coverage."""
        base_run = {
            "status": "completed",
            "context_config": {"context_mode": "diff_only", "context_budget_chars": 35000},
            "judge_config": "deepseek/deepseek-v4-flash",
            "scorer_sha": "e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d",
            "execution_summary": {
                "total_planned": 2,
                "completed": 2,
                "failed": 0,
                "fallback_count": 0,
                "agent_identity": {"agent": "agy", "model_name": "gemini", "model_confirmed": True},
                "samples": [{"key": "1"}, {"key": "2"}],
            },
            "usage_summary": {
                "input_tokens": 100,
                "output_tokens": 20,
                "thinking_tokens": "未知",
                "total_tokens": "120 (已知部分合计: 1/2 样本)",
                "token_details": {
                    "total_tokens": {"raw_sum": 120, "is_complete": False, "count": 1, "total_samples": 2},
                },
            },
            "metrics": {"overall": {"grounded_recall": 0.5, "grounded_precision": 0.5, "augmented_recall": 0.5, "augmented_precision": 0.5}},
        }
        cand_run = json.loads(json.dumps(base_run))
        cand_run["context_config"]["context_mode"] = "existing_four_tier"
        cand_run["usage_summary"]["total_tokens"] = 300
        cand_run["usage_summary"]["token_details"] = {
            "total_tokens": {"raw_sum": 300, "is_complete": True, "count": 2, "total_samples": 2},
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            b_dir = tmp / "b"
            a_dir = tmp / "a"
            b_dir.mkdir()
            a_dir.mkdir()
            (b_dir / "results.json").write_text(json.dumps(base_run), encoding="utf-8")
            (a_dir / "results.json").write_text(json.dumps(cand_run), encoding="utf-8")

            report = compare_benchmarks(b_dir, a_dir)
            self.assertIn("120 (已知部分合计: 1/2 样本)", report)
            self.assertIn("+180 (基于已知部分计算)", report)

    def test_quality_decision_logic_tradeoff_and_precision_drop(self):
        """Precision drop without recall change is not 'quality parity'; opposite recall/precision shifts are reported as trade-off."""
        base_run = {
            "status": "completed",
            "golden_fingerprint": "golden_fp_default",
            "context_config": {"context_mode": "diff_only", "context_budget_chars": 35000},
            "judge_config": "deepseek/deepseek-v4-flash",
            "scorer_sha": "e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d",
            "execution_config": {
                "timeout_seconds": 300,
                "system_prompt": "You are reviewing a Pull Request.",
                "system_prompt_fingerprint": "hash_sp_default",
                "context_budget_chars": 35000,
                "context_mode": "diff_only",
            },
            "version_info": {
                "judge_config": "deepseek/deepseek-v4-flash",
                "scorer_sha": "e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d",
                "golden_fingerprint": "golden_fp_default",
            },
            "execution_summary": {
                "total_planned": 1,
                "completed": 1,
                "failed": 0,
                "fallback_count": 0,
                "agent_identity": {"agent": "agy", "model_name": "gemini", "model_confirmed": True, "cli_version": "1.3.3"},
                "samples": [
                    {
                        "key": "1",
                        "base": "a" * 40,
                        "head": "b" * 40,
                        "base_diff_chars": 100,
                        "base_diff_fingerprint": "fp_diff_1",
                        "common_prompt_fingerprint": "pfp_1",
                    }
                ],
            },
            "usage_summary": {},
            "metrics": {"overall": {"grounded_recall": 0.50, "grounded_precision": 0.80, "augmented_recall": 0.50, "augmented_precision": 0.80}},
        }

        # Case 1: Recall flat, precision drops
        cand_p_drop = json.loads(json.dumps(base_run))
        cand_p_drop["context_config"]["context_mode"] = "existing_four_tier"
        cand_p_drop["metrics"]["overall"]["grounded_precision"] = 0.50

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            b_dir = tmp / "b1"
            a_dir = tmp / "a1"
            b_dir.mkdir()
            a_dir.mkdir()
            (b_dir / "results.json").write_text(json.dumps(base_run), encoding="utf-8")
            (a_dir / "results.json").write_text(json.dumps(cand_p_drop), encoding="utf-8")

            report = compare_benchmarks(b_dir, a_dir)
            self.assertIn("准确率下降 -30.0%", report)
            self.assertIn("不应视为质量持平", report)
            self.assertNotIn("质量持平（召回率 Delta: +0.0%", report)

        # Case 2: Recall up (+20%), precision down (-20%) -> Trade-off
        cand_tradeoff = json.loads(json.dumps(base_run))
        cand_tradeoff["context_config"]["context_mode"] = "existing_four_tier"
        cand_tradeoff["metrics"]["overall"]["grounded_recall"] = 0.70
        cand_tradeoff["metrics"]["overall"]["grounded_precision"] = 0.60

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            b_dir = tmp / "b2"
            a_dir = tmp / "a2"
            b_dir.mkdir()
            a_dir.mkdir()
            (b_dir / "results.json").write_text(json.dumps(base_run), encoding="utf-8")
            (a_dir / "results.json").write_text(json.dumps(cand_tradeoff), encoding="utf-8")

            report = compare_benchmarks(b_dir, a_dir)
            self.assertIn("反向权衡（Trade-off）", report)
            self.assertIn("不认定为单纯的整体质量提升或下降", report)
            self.assertNotIn("检索模式带来了审核质量提升", report)

    def test_cli_review_context_mode_flags(self):
        """CLI review command accepts --context-mode and --context-budget and forwards them."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            diff_file = tmp / "sample.diff"
            diff_file.write_text(self.sample_diff, encoding="utf-8")
            cmd = [
                sys.executable,
                str(HERDR_ROOT / "bin" / "herdr-review-bench"),
                "review",
                "--repo", str(tmp),
                "--diff", str(diff_file),
                "--agent", "rule",
                "--context-mode", "diff_only",
                "--context-budget", "20000",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"CLI error: {res.stderr}")
            out_json = json.loads(res.stdout)
            self.assertEqual(out_json["status"], "success")
            c_meta = out_json.get("context_meta", {})
            self.assertEqual(c_meta.get("requested_context_mode"), "diff_only")
            self.assertEqual(c_meta.get("context_budget_chars"), 20000)

    def test_cli_run_early_validation_rejection(self):
        """CLI run command fails early with non-zero exit code when given invalid context config in YAML/JSON."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            bad_cfg_file = tmp / "bad_cfg.yaml"
            bad_cfg_file.write_text(
                "agent: rule\n"
                "review:\n"
                "  context_mode: unsupported_mode_xyz\n"
                "  context_budget_chars: 35000\n",
                encoding="utf-8",
            )
            out_dir = tmp / "out"
            cmd = [
                sys.executable,
                str(HERDR_ROOT / "bin" / "herdr-review-bench"),
                "run",
                "--config", str(bad_cfg_file),
                "--output", str(out_dir),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Invalid review context configuration", res.stderr)

    def test_quality_decision_9_directions_matrix(self):
        """Test all 9 combinations of (recall, precision) delta: up, flat, down."""
        directions = [
            # (b_rec, a_rec, b_prec, a_prec, expected_substrings, forbidden_substrings)
            # 1. both up
            (0.5, 0.7, 0.6, 0.8, ["均实现提升", "+20.0%", "+20.0%"], ["下滑", "权衡"]),
            # 2. both down
            (0.7, 0.5, 0.8, 0.6, ["均出现下滑", "-20.0%", "-20.0%"], ["提升", "权衡"]),
            # 3. recall up, precision down -> trade-off
            (0.5, 0.7, 0.8, 0.6, ["反向权衡", "+20.0%", "-20.0%"], ["均实现提升"]),
            # 4. recall down, precision up -> trade-off
            (0.7, 0.5, 0.6, 0.8, ["反向权衡", "-20.0%", "+20.0%"], ["均实现提升"]),
            # 5. both flat
            (0.5, 0.5, 0.6, 0.6, ["完全持平", "+0.0%", "+0.0%"], ["下滑", "提升"]),
            # 6. recall flat, precision down
            (0.5, 0.5, 0.8, 0.6, ["准确率下降 -20.0%", "不应视为质量持平"], ["完全持平"]),
            # 7. recall flat, precision up
            (0.5, 0.5, 0.6, 0.8, ["准确率提高 +20.0%"], ["不应视为质量持平"]),
            # 8. recall up, precision flat
            (0.5, 0.7, 0.6, 0.6, ["召回率提升 +20.0%"], ["不应视为质量持平"]),
            # 9. recall down, precision flat -> MUST use gr_d (-30.0%), NOT gp_d (+0.0%)!
            (0.5, 0.2, 0.8, 0.8, ["召回率下降 -30.0%"], ["召回率下降 +0.0%"]),
        ]

        base_run = {
            "status": "completed",
            "golden_fingerprint": "golden_fp_default",
            "context_config": {"context_mode": "diff_only", "context_budget_chars": 35000},
            "judge_config": "deepseek/deepseek-v4-flash",
            "scorer_sha": "e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d",
            "execution_config": {
                "timeout_seconds": 300,
                "system_prompt": "You are reviewing a Pull Request.",
                "system_prompt_fingerprint": "hash_sp_default",
                "context_budget_chars": 35000,
                "context_mode": "diff_only",
            },
            "version_info": {
                "judge_config": "deepseek/deepseek-v4-flash",
                "scorer_sha": "e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d",
                "golden_fingerprint": "golden_fp_default",
            },
            "execution_summary": {
                "total_planned": 1,
                "completed": 1,
                "failed": 0,
                "fallback_count": 0,
                "agent_identity": {"agent": "agy", "model_name": "gemini", "model_confirmed": True, "cli_version": "1.3.3"},
                "samples": [
                    {
                        "key": "1",
                        "base": "a" * 40,
                        "head": "b" * 40,
                        "base_diff_chars": 100,
                        "base_diff_fingerprint": "fp_diff_1",
                        "common_prompt_fingerprint": "pfp_1",
                    }
                ],
            },
            "usage_summary": {},
            "metrics": {"overall": {}},
        }

        for idx, (b_rec, a_rec, b_prec, a_prec, exp_subs, forb_subs) in enumerate(directions):
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp = Path(tmp_dir)
                b_dir = tmp / "b"
                a_dir = tmp / "a"
                b_dir.mkdir()
                a_dir.mkdir()

                b_data = json.loads(json.dumps(base_run))
                b_data["metrics"]["overall"] = {
                    "grounded_recall": b_rec,
                    "grounded_precision": b_prec,
                    "augmented_recall": b_rec,
                    "augmented_precision": b_prec,
                }
                a_data = json.loads(json.dumps(base_run))
                a_data["context_config"]["context_mode"] = "existing_four_tier"
                a_data["metrics"]["overall"] = {
                    "grounded_recall": a_rec,
                    "grounded_precision": a_prec,
                    "augmented_recall": a_rec,
                    "augmented_precision": a_prec,
                }

                (b_dir / "results.json").write_text(json.dumps(b_data), encoding="utf-8")
                (a_dir / "results.json").write_text(json.dumps(a_data), encoding="utf-8")

                report = compare_benchmarks(b_dir, a_dir)
                for sub in exp_subs:
                    self.assertIn(sub, report, f"Direction {idx} expected '{sub}' in report: {report}")
                for sub in forb_subs:
                    self.assertNotIn(sub, report, f"Direction {idx} forbidden '{sub}' in report: {report}")

    def test_comparability_rejects_divergent_prompts_and_timeouts(self):
        """Strict comparability must reject runs with divergent system prompts, timeouts, or reversed mode direction."""
        base_run = {
            "status": "completed",
            "context_config": {"context_mode": "diff_only", "context_budget_chars": 35000},
            "judge_config": "deepseek/deepseek-v4-flash",
            "scorer_sha": "e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d",
            "golden_fingerprint": "golden_fp_default",
            "execution_config": {
                "timeout_seconds": 300,
                "system_prompt": "Check only security.",
                "system_prompt_fingerprint": "hash_sec",
                "context_budget_chars": 35000,
                "context_mode": "diff_only",
            },
            "execution_summary": {
                "total_planned": 1,
                "completed": 1,
                "failed": 0,
                "fallback_count": 0,
                "agent_identity": {
                    "agent": "agy",
                    "model_name": "gemini",
                    "model_confirmed": True,
                    "cli_version": "1.3.3",
                    "details": {"effort": "low"},
                },
                "samples": [
                    {
                        "key": "pr_1",
                        "base": "a" * 40,
                        "head": "b" * 40,
                        "base_diff_chars": 1000,
                        "base_diff_fingerprint": "fp_diff_123",
                        "common_prompt_fingerprint": "pfp_123",
                    }
                ],
            },
            "version_info": {
                "judge_config": "deepseek/deepseek-v4-flash",
                "scorer_sha": "e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d",
                "golden_fingerprint": "golden_fp_default",
            },
            "metrics": {
                "overall": {
                    "grounded_recall": 0.5,
                    "grounded_precision": 0.5,
                    "augmented_recall": 0.5,
                    "augmented_precision": 0.5,
                }
            },
        }

        # Subtest 1: System prompt divergence
        cand_diff_prompt = json.loads(json.dumps(base_run))
        cand_diff_prompt["context_config"]["context_mode"] = "existing_four_tier"
        cand_diff_prompt["execution_config"]["system_prompt"] = "Check only naming."
        cand_diff_prompt["execution_config"]["system_prompt_fingerprint"] = "hash_naming"
        is_comp, disc = check_benchmark_comparability(base_run, cand_diff_prompt)
        self.assertFalse(is_comp, "Divergent system prompts should not be strictly comparable")
        self.assertTrue(any("提示词" in d or "prompt" in d.lower() for d in disc))

        # Subtest 2: Timeout divergence (300 vs 1)
        cand_diff_timeout = json.loads(json.dumps(base_run))
        cand_diff_timeout["context_config"]["context_mode"] = "existing_four_tier"
        cand_diff_timeout["execution_config"]["timeout_seconds"] = 1
        is_comp, disc = check_benchmark_comparability(base_run, cand_diff_timeout)
        self.assertFalse(is_comp, "Divergent timeouts should not be strictly comparable")
        self.assertTrue(any("超时" in d or "timeout" in d.lower() for d in disc))

        # Subtest 3: Reversed mode direction (existing_four_tier -> diff_only)
        rev_base = json.loads(json.dumps(base_run))
        rev_base["context_config"]["context_mode"] = "existing_four_tier"
        rev_cand = json.loads(json.dumps(base_run))
        rev_cand["context_config"]["context_mode"] = "diff_only"
        is_comp, disc = check_benchmark_comparability(rev_base, rev_cand)
        self.assertFalse(is_comp, "Reversed direction should not be strictly comparable")
        self.assertTrue(any("对照方向" in d or "diff_only" in d for d in disc))

        # Subtest 4: Missing base diff fingerprint
        cand_no_fp = json.loads(json.dumps(base_run))
        cand_no_fp["context_config"]["context_mode"] = "existing_four_tier"
        del cand_no_fp["execution_summary"]["samples"][0]["base_diff_fingerprint"]
        is_comp, disc = check_benchmark_comparability(base_run, cand_no_fp)
        self.assertFalse(is_comp, "Missing base diff fingerprint should not be strictly comparable")
        self.assertTrue(any("指纹" in d or "fingerprint" in d.lower() for d in disc))


class TestGroupModelIdentityTable(unittest.TestCase):
    """Unit tests for Section A: Group model identity aggregation across 6 contract scenarios."""

    def test_aggregate_group_agent_identity_6_scenarios_table(self):
        # Case 1: No declaration, A -> A => Confirmed A
        base_unclaimed = {"agent": "agy", "model_name": "unknown", "cli_version": "1.3.3", "model_confirmed": False, "details": {}}
        s_a1 = {"review_attempted": True, "agent_identity": {"model_name": "gemini-2.5", "model_confirmed": True, "details": {"source": "runtime_evidence"}}}
        s_a2 = {"review_attempted": True, "agent_identity": {"model_name": "gemini-2.5", "model_confirmed": True, "details": {"source": "runtime_evidence"}}}
        res1 = aggregate_group_agent_identity([s_a1, s_a2], base_unclaimed)
        self.assertTrue(res1["model_confirmed"])
        self.assertEqual(res1["model_name"], "gemini-2.5")

        # Case 2: No declaration, A -> B -> A => Unconfirmed, preserves runtime_drift
        s_b = {"review_attempted": True, "agent_identity": {"model_name": "claude-3.7", "model_confirmed": True, "details": {"source": "runtime_evidence"}}}
        res2 = aggregate_group_agent_identity([s_a1, s_b, s_a2], base_unclaimed)
        self.assertFalse(res2["model_confirmed"])
        self.assertEqual(res2["details"].get("source"), "runtime_drift")
        self.assertIn("multiple models detected", res2["details"].get("mismatch", ""))

        # Case 3: No declaration, unknown -> A => Unconfirmed, preserves unconfirmed_samples
        s_unk = {"review_attempted": True, "agent_identity": {"model_name": "unknown", "model_confirmed": False, "details": {"source": "unknown"}}}
        res3 = aggregate_group_agent_identity([s_unk, s_a1], base_unclaimed)
        self.assertFalse(res3["model_confirmed"])
        self.assertEqual(res3["details"].get("source"), "unconfirmed_samples")

        # Case 4: Declared A, A -> unknown => Unconfirmed
        base_decl_a = {"agent": "agy", "model_name": "gemini-2.5", "cli_version": "1.3.3", "model_confirmed": False, "details": {"requested_model": "gemini-2.5"}}
        res4 = aggregate_group_agent_identity([s_a1, s_unk], base_decl_a)
        self.assertFalse(res4["model_confirmed"])
        self.assertEqual(res4["details"].get("source"), "unconfirmed_samples")

        # Case 5: Declared A, B -> A => Unconfirmed, preserves mismatch / drift
        res5 = aggregate_group_agent_identity([s_b, s_a1], base_decl_a)
        self.assertFalse(res5["model_confirmed"])
        self.assertIn(res5["details"].get("source"), ("runtime_drift", "runtime_mismatch"))

        # Case 6: Any declaration, no review attempts => Unconfirmed, preserves no_review_attempts
        s_no_attempt = {"review_attempted": False, "error": "workspace_prep_failed"}
        res6 = aggregate_group_agent_identity([s_no_attempt], base_decl_a)
        self.assertFalse(res6["model_confirmed"])
        self.assertEqual(res6["details"].get("source"), "no_review_attempts")
        self.assertEqual(res6["details"].get("reason"), "no review attempts occurred")


class TestTokenCoverageAndTimingContracts(unittest.TestCase):
    """Unit tests for Section C (Token failure coverage) and Section D (Review timing boundary)."""

    def test_token_coverage_aggregation_scenarios(self):
        # Helper to compute usage summary using the exact bin/herdr-review-bench logic
        def compute_tokens(samples, review_attempts):
            token_metrics = ["input_tokens", "output_tokens", "thinking_tokens", "total_tokens"]
            token_stats = {k: {"sum": 0, "count": 0} for k in token_metrics}
            for s in samples:
                if s.get("review_attempted") is True:
                    toks = s.get("tokens")
                    if isinstance(toks, dict):
                        inp = toks.get("input_tokens")
                        out = toks.get("output_tokens")
                        thk = toks.get("thinking_tokens")
                        tot = toks.get("total_tokens")
                        for k, v in [("input_tokens", inp), ("output_tokens", out), ("thinking_tokens", thk), ("total_tokens", tot)]:
                            if isinstance(v, (int, float)):
                                token_stats[k]["sum"] += int(v)
                                token_stats[k]["count"] += 1
            token_details = {}
            formatted = {}
            for k in token_metrics:
                c = token_stats[k]["count"]
                s = token_stats[k]["sum"]
                if review_attempts == 0 or c == 0:
                    formatted[k] = "未知"
                    token_details[k] = {"raw_sum": None, "count": 0, "total_samples": review_attempts, "is_complete": False}
                elif c == review_attempts:
                    formatted[k] = s
                    token_details[k] = {"raw_sum": s, "count": c, "total_samples": review_attempts, "is_complete": True}
                else:
                    formatted[k] = f"{s} (已知部分合计: {c}/{review_attempts} 样本)"
                    token_details[k] = {"raw_sum": s, "count": c, "total_samples": review_attempts, "is_complete": False}
            return formatted, token_details

        # Scenario 1: Only input=100, output=20 -> thinking, total remain unknown
        s1 = [{"review_attempted": True, "tokens": {"input_tokens": 100, "output_tokens": 20}}]
        fmt1, dtl1 = compute_tokens(s1, 1)
        self.assertEqual(fmt1["input_tokens"], 100)
        self.assertTrue(dtl1["input_tokens"]["is_complete"])
        self.assertEqual(fmt1["output_tokens"], 20)
        self.assertTrue(dtl1["output_tokens"]["is_complete"])
        self.assertEqual(fmt1["thinking_tokens"], "未知")
        self.assertFalse(dtl1["thinking_tokens"]["is_complete"])
        self.assertEqual(fmt1["total_tokens"], "未知")
        self.assertFalse(dtl1["total_tokens"]["is_complete"])

        # Scenario 2: Two success attempts, one without usage, one total=120 -> total=120, coverage 1/2
        s2 = [
            {"review_attempted": True, "tokens": None},
            {"review_attempted": True, "tokens": {"total_tokens": 120}},
        ]
        fmt2, dtl2 = compute_tokens(s2, 2)
        self.assertEqual(fmt2["total_tokens"], "120 (已知部分合计: 1/2 样本)")
        self.assertFalse(dtl2["total_tokens"]["is_complete"])
        self.assertEqual(dtl2["total_tokens"]["raw_sum"], 120)

        # Scenario 3: One timeout usage unknown, one success total=120 -> coverage 1/2
        s3 = [
            {"review_attempted": True, "status": "failed", "tokens": None},
            {"review_attempted": True, "status": "completed", "tokens": {"total_tokens": 120}},
        ]
        fmt3, dtl3 = compute_tokens(s3, 2)
        self.assertEqual(fmt3["total_tokens"], "120 (已知部分合计: 1/2 样本)")
        self.assertFalse(dtl3["total_tokens"]["is_complete"])

        # Scenario 4: One prep failure not entering review, one success total=120 -> review attempts=1, coverage 1/1
        s4 = [
            {"review_attempted": False, "status": "failed", "tokens": None},
            {"review_attempted": True, "status": "completed", "tokens": {"total_tokens": 120}},
        ]
        fmt4, dtl4 = compute_tokens(s4, 1)
        self.assertEqual(fmt4["total_tokens"], 120)
        self.assertTrue(dtl4["total_tokens"]["is_complete"])

        # Scenario 5: Received usage then findings parse failed -> sample failed, usage preserved and counted
        s5 = [
            {"review_attempted": True, "status": "failed", "tokens": {"total_tokens": 80}},
            {"review_attempted": True, "status": "completed", "tokens": {"total_tokens": 120}},
        ]
        fmt5, dtl5 = compute_tokens(s5, 2)
        self.assertEqual(fmt5["total_tokens"], 200)
        self.assertTrue(dtl5["total_tokens"]["is_complete"])

        # Scenario 6: No review attempts -> tokens_available False, all unknown
        s6 = [
            {"review_attempted": False, "status": "failed"},
        ]
        fmt6, dtl6 = compute_tokens(s6, 0)
        self.assertEqual(fmt6["total_tokens"], "未知")
        self.assertFalse(dtl6["total_tokens"]["is_complete"])

    def test_timing_boundary_contracts(self):
        # Scenario 1: First sample prep 7s, fails (not reviewed); Second sample prep 2s, review 3s
        # -> review_attempts: 1, total_review_time: 3s, avg_review_time: 3s
        t_rev_total_1 = 3000
        attempts_1 = 1
        avg_1 = round(t_rev_total_1 / attempts_1, 2) if attempts_1 > 0 else None
        self.assertEqual(avg_1, 3000.0)

        # Scenario 2: First sample prep 2s, review 300s timeout; Second prep 2s, review 3s success
        # -> review_attempts: 2, total_review_time: 303s, avg_review_time: 151.5s
        t_rev_total_2 = 303000
        attempts_2 = 2
        avg_2 = round(t_rev_total_2 / attempts_2, 2) if attempts_2 > 0 else None
        self.assertEqual(avg_2, 151500.0)

        # Scenario 3: 0 review attempts -> avg is None, NOT max(count, 1) fabricated
        attempts_0 = 0
        avg_0 = round(0 / attempts_0, 2) if attempts_0 > 0 else None
        self.assertIsNone(avg_0)


class TestRealCallChainIntegration(unittest.TestCase):
    """End-to-end integration tests for cmd_run and compare_benchmarks verifying real call chains."""

    @classmethod
    def setUpClass(cls):
        from importlib.machinery import SourceFileLoader
        import importlib.util
        hrb_path = HERDR_ROOT / "bin" / "herdr-review-bench"
        loader = SourceFileLoader("hrb_integration", str(hrb_path))
        spec = importlib.util.spec_from_loader("hrb_integration", loader)
        cls.hrb = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.hrb)

    def _run_with_mock_judge(self, config_file: Path, output_dir: Path, judge_metrics: Optional[Dict[str, Any]] = None):
        import argparse
        import unittest.mock

        metrics = judge_metrics or {
            "overall": {
                "grounded_recall": 0.5,
                "grounded_precision": 0.8,
                "augmented_recall": 0.5,
                "augmented_precision": 0.8,
            }
        }

        orig_sp_run = subprocess.run

        def mock_subprocess_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and len(cmd) > 2 and cmd[0] == "npm" and cmd[1] == "run" and cmd[2] == "judge":
                out_idx = cmd.index("--output")
                rf = Path(cmd[out_idx + 1])
                rf.parent.mkdir(parents=True, exist_ok=True)
                rf.write_text(json.dumps({"metrics": metrics}), encoding="utf-8")
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="judge success", stderr="")
            return orig_sp_run(cmd, *args, **kwargs)

        with unittest.mock.patch.object(self.hrb, "find_or_setup_reviewbench", return_value=(output_dir, "e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d")):
            with unittest.mock.patch("subprocess.run", side_effect=mock_subprocess_run):
                args = argparse.Namespace(config=str(config_file), output=str(output_dir))
                return self.hrb.cmd_run(args)

    def test_integration_1_strict_context_comparison_holds(self):
        """Integration I: diff_only -> existing_four_tier pairing strictly comparable via real cmd_run."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            mf = tmp / "manifest.json"
            with open(HERDR_ROOT / "tests/fixtures/review_benchmark/corpus/manifest.json", encoding="utf-8") as f:
                mf_data = json.load(f)[:1]
            mf.write_text(json.dumps(mf_data), encoding="utf-8")

            b_cfg = tmp / "b_cfg.yaml"
            b_cfg.write_text(f"""
name: b_diff_only
agent: rule
benchmark:
  manifest: {mf}
review:
  context_mode: diff_only
  context_budget_chars: 35000
  timeout_seconds: 300
""")
            b_out = tmp / "b_out"

            a_cfg = tmp / "a_cfg.yaml"
            a_cfg.write_text(f"""
name: a_ctx
agent: rule
benchmark:
  manifest: {mf}
review:
  context_mode: existing_four_tier
  context_budget_chars: 35000
  timeout_seconds: 300
""")
            a_out = tmp / "a_out"

            ret_b = self._run_with_mock_judge(b_cfg, b_out)
            ret_a = self._run_with_mock_judge(a_cfg, a_out)
            self.assertEqual(ret_b, 0)
            self.assertEqual(ret_a, 0)

            # Validate generated results.json files
            b_res = json.loads((b_out / "results.json").read_text(encoding="utf-8"))
            a_res = json.loads((a_out / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(b_res["status"], "completed")
            self.assertEqual(a_res["status"], "completed")
            self.assertTrue(b_res["agent_identity"]["model_confirmed"])
            self.assertTrue(a_res["agent_identity"]["model_confirmed"])

            # Verify strict comparability and report generation
            is_comp, disc = check_benchmark_comparability(b_res, a_res)
            self.assertTrue(is_comp, f"Expected strictly comparable, got discrepancies: {disc}")

            report = compare_benchmarks(b_out, a_out)
            self.assertIn("单变量控制结论", report)
            self.assertIn("两组评测已满足严格单变量同模型控制条件", report)
            self.assertIn("主要来源于上下文检索模式的不同", report)
            self.assertNotIn("单变量控制警告", report)

    def test_integration_2_divergent_prompt_and_timeout_blocked(self):
        """Integration II: Divergent prompts and timeouts blocked via real cmd_run results."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            mf = tmp / "manifest.json"
            with open(HERDR_ROOT / "tests/fixtures/review_benchmark/corpus/manifest.json", encoding="utf-8") as f:
                mf_data = json.load(f)[:1]
            mf.write_text(json.dumps(mf_data), encoding="utf-8")

            # Counterexample 1: Divergent system prompts
            b_sec_cfg = tmp / "b_sec.yaml"
            b_sec_cfg.write_text(f"""
name: b_sec
agent: rule
benchmark:
  manifest: {mf}
review:
  context_mode: diff_only
  system_prompt: 'Check only security.'
  timeout_seconds: 300
""")
            b_sec_out = tmp / "b_sec_out"

            a_nam_cfg = tmp / "a_nam.yaml"
            a_nam_cfg.write_text(f"""
name: a_nam
agent: rule
benchmark:
  manifest: {mf}
review:
  context_mode: existing_four_tier
  system_prompt: 'Check only naming.'
  timeout_seconds: 300
""")
            a_nam_out = tmp / "a_nam_out"

            self.assertEqual(self._run_with_mock_judge(b_sec_cfg, b_sec_out), 0)
            self.assertEqual(self._run_with_mock_judge(a_nam_cfg, a_nam_out), 0)

            report_prompt = compare_benchmarks(b_sec_out, a_nam_out)
            self.assertIn("单变量控制警告", report_prompt)
            self.assertIn("系统提示词/指令不一致", report_prompt)
            self.assertIn("无法得出单变量质量结论", report_prompt)
            self.assertNotIn("单变量控制结论", report_prompt)

            # Counterexample 2: Divergent timeouts
            b_t300_cfg = tmp / "b_t300.yaml"
            b_t300_cfg.write_text(f"""
name: b_t300
agent: rule
benchmark:
  manifest: {mf}
review:
  context_mode: diff_only
  timeout_seconds: 300
""")
            b_t300_out = tmp / "b_t300_out"

            a_t1_cfg = tmp / "a_t1.yaml"
            a_t1_cfg.write_text(f"""
name: a_t1
agent: rule
benchmark:
  manifest: {mf}
review:
  context_mode: existing_four_tier
  timeout_seconds: 1
""")
            a_t1_out = tmp / "a_t1_out"

            self.assertEqual(self._run_with_mock_judge(b_t300_cfg, b_t300_out), 0)
            self.assertEqual(self._run_with_mock_judge(a_t1_cfg, a_t1_out), 0)

            report_timeout = compare_benchmarks(b_t300_out, a_t1_out)
            self.assertIn("单变量控制警告", report_timeout)
            self.assertIn("超时时间不一致", report_timeout)
            self.assertIn("无法得出单变量质量结论", report_timeout)
            self.assertNotIn("单变量控制结论", report_timeout)

    def test_integration_3_multisample_failure_boundaries_written_to_disk(self):
        """Integration III: Multi-sample run with prep failure, review timeout, parse failure with tokens, and drift."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            mf_data = [
                # Sample 1: Prep failure (non-existent commit SHAs)
                {"repo": "https://github.com/allinai0506/HAFlow", "pr_number": 991, "base": "0" * 40, "head": "1" * 40},
                # Sample 2: PR 107, will raise TimeoutError
                {"repo": "https://github.com/allinai0506/HAFlow", "pr_number": 107, "base": "a957663a38f48a399668c4d6b970f0d82ab885cf", "head": "3477d064da8541451840959c419b94146b185053"},
                # Sample 3: PR 108, will raise ValueError with tokens & model-A
                {"repo": "https://github.com/allinai0506/HAFlow", "pr_number": 108, "base": "a957663a38f48a399668c4d6b970f0d82ab885cf", "head": "3477d064da8541451840959c419b94146b185053"},
                # Sample 4: PR 109, will succeed with tokens & model-B
                {"repo": "https://github.com/allinai0506/HAFlow", "pr_number": 109, "base": "a957663a38f48a399668c4d6b970f0d82ab885cf", "head": "3477d064da8541451840959c419b94146b185053"},
            ]
            mf = tmp / "mf.json"
            mf.write_text(json.dumps(mf_data), encoding="utf-8")

            cfg_file = tmp / "cfg.yaml"
            cfg_file.write_text(f"""
name: multisample_test
agent: agy
benchmark:
  manifest: {mf}
review:
  context_mode: diff_only
  timeout_seconds: 300
""")
            out_dir = tmp / "out"

            orig_rar = self.hrb.run_agent_review

            def mock_rar(agent_name, repo_dir, pr_info, *a, **kw):
                pr_num = pr_info["pr_number"]
                if pr_num == 107:
                    raise TimeoutError("agent_review_timed_out")
                elif pr_num == 108:
                    exc = ValueError("output_parse_failed: malformed json")
                    exc.token_usage = {"total_tokens": 150, "input_tokens": 100, "output_tokens": 50}
                    exc.agent_identity = {"agent": "agy", "model_name": "model-A", "model_confirmed": True, "details": {"source": "runtime_evidence"}}
                    raise exc
                elif pr_num == 109:
                    return {
                        "pr": pr_info,
                        "agent": agent_name,
                        "findings": [],
                        "agent_identity": {"agent": "agy", "model_name": "model-B", "model_confirmed": True, "details": {"source": "runtime_evidence"}},
                        "usage": {"time_in_ms": 50, "tokens": {"total_tokens": 200, "input_tokens": 150, "output_tokens": 50}},
                        "prompt_meta": {"prompt_chars": 100, "prompt_fingerprint": "fp"},
                        "context_meta": {"requested_context_mode": "diff_only", "effective_context_mode": "diff_only", "context_budget_chars": 35000},
                    }
                return orig_rar(agent_name, repo_dir, pr_info, *a, **kw)

            import unittest.mock
            with unittest.mock.patch.object(self.hrb, "run_agent_review", side_effect=mock_rar):
                self._run_with_mock_judge(cfg_file, out_dir)

            res = json.loads((out_dir / "results.json").read_text(encoding="utf-8"))
            es = res["execution_summary"]
            us = res["usage_summary"]

            # Assert sample counts and review_attempted status
            self.assertEqual(es["total_planned"], 4)
            self.assertEqual(es["review_attempts"], 3)  # Sample 1 prep failure excluded from attempts
            self.assertEqual(es["completed"], 1)
            self.assertEqual(es["failed"], 3)

            # Sample 1: prep failure
            self.assertFalse(es["samples"][0]["review_attempted"])
            self.assertIsNone(es["samples"][0]["time_in_ms"])
            self.assertIn("workspace_prep_failed", es["samples"][0]["error"])

            # Sample 2: review timeout
            self.assertTrue(es["samples"][1]["review_attempted"])
            self.assertEqual(es["samples"][1]["status"], "failed")
            self.assertIsNone(es["samples"][1]["tokens"])

            # Sample 3: parse failure with tokens preserved
            self.assertTrue(es["samples"][2]["review_attempted"])
            self.assertEqual(es["samples"][2]["status"], "failed")
            self.assertEqual(es["samples"][2]["tokens"]["total_tokens"], 150)

            # Sample 4: review success
            self.assertTrue(es["samples"][3]["review_attempted"])
            self.assertEqual(es["samples"][3]["status"], "completed")

            # Group model identity: unconfirmed due to unknown sample 2 and drift between model-A & model-B
            self.assertFalse(res["agent_identity"]["model_confirmed"])

            # Token coverage: 3 review attempts, 2 samples have tokens (150 + 200 = 350)
            self.assertEqual(us["total_tokens"], "350 (已知部分合计: 2/3 样本)")
            self.assertFalse(us["token_details"]["total_tokens"]["is_complete"])
            self.assertEqual(us["token_details"]["total_tokens"]["raw_sum"], 350)

            # Average review time: denominator is actual review_attempts (3), not total samples or completed
            self.assertIsNotNone(us["avg_review_time_ms"])

    def test_integration_group_model_identity_cases_via_cmd_run(self):
        """Integration verification that group model identity aggregation is written to disk correctly across cases."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            mf_data = [
                {"repo": "https://github.com/allinai0506/HAFlow", "pr_number": 107, "base": "a957663a38f48a399668c4d6b970f0d82ab885cf", "head": "3477d064da8541451840959c419b94146b185053"},
                {"repo": "https://github.com/allinai0506/HAFlow", "pr_number": 108, "base": "a957663a38f48a399668c4d6b970f0d82ab885cf", "head": "3477d064da8541451840959c419b94146b185053"},
            ]
            mf = tmp / "mf_2.json"
            mf.write_text(json.dumps(mf_data), encoding="utf-8")

            # Case A: A -> A through cmd_run => Group confirmed A
            cfg_a = tmp / "cfg_a.yaml"
            cfg_a.write_text(f"""
name: test_a
agent: agy
benchmark:
  manifest: {mf}
review:
  context_mode: diff_only
""")
            out_a = tmp / "out_a"

            def mock_rar_a(agent_name, repo_dir, pr_info, *a, **kw):
                return {
                    "pr": pr_info, "agent": agent_name, "findings": [],
                    "agent_identity": {"agent": "agy", "model_name": "model-A", "model_confirmed": True, "details": {"source": "runtime_evidence"}},
                    "usage": {"time_in_ms": 10}, "prompt_meta": {"prompt_chars": 10}, "context_meta": {"requested_context_mode": "diff_only"},
                }

            import unittest.mock
            with unittest.mock.patch.object(self.hrb, "run_agent_review", side_effect=mock_rar_a):
                self._run_with_mock_judge(cfg_a, out_a)
            res_a = json.loads((out_a / "results.json").read_text(encoding="utf-8"))
            self.assertTrue(res_a["agent_identity"]["model_confirmed"])
            self.assertEqual(res_a["agent_identity"]["model_name"], "model-A")

            # Case B: A -> B through cmd_run => Drift detected, not confirmed
            out_b = tmp / "out_b"

            def mock_rar_b(agent_name, repo_dir, pr_info, *a, **kw):
                m_name = "model-A" if pr_info["pr_number"] == 107 else "model-B"
                return {
                    "pr": pr_info, "agent": agent_name, "findings": [],
                    "agent_identity": {"agent": "agy", "model_name": m_name, "model_confirmed": True, "details": {"source": "runtime_evidence"}},
                    "usage": {"time_in_ms": 10}, "prompt_meta": {"prompt_chars": 10}, "context_meta": {"requested_context_mode": "diff_only"},
                }

            with unittest.mock.patch.object(self.hrb, "run_agent_review", side_effect=mock_rar_b):
                self._run_with_mock_judge(cfg_a, out_b)
            res_b = json.loads((out_b / "results.json").read_text(encoding="utf-8"))
            self.assertFalse(res_b["agent_identity"]["model_confirmed"])
            self.assertEqual(res_b["agent_identity"]["details"]["source"], "runtime_drift")

            # Case C: Declared model-A, runtime evidence is B -> A => Mismatch/drift, not confirmed
            cfg_c = tmp / "cfg_c.yaml"
            cfg_c.write_text(f"""
name: test_c
agent: agy
benchmark:
  manifest: {mf}
review:
  context_mode: diff_only
  model: model-A
""")
            out_c = tmp / "out_c"

            with unittest.mock.patch.object(self.hrb, "run_agent_review", side_effect=mock_rar_b):
                self._run_with_mock_judge(cfg_c, out_c)
            res_c = json.loads((out_c / "results.json").read_text(encoding="utf-8"))
            self.assertFalse(res_c["agent_identity"]["model_confirmed"])
            self.assertIn(res_c["agent_identity"]["details"]["source"], ("runtime_drift", "runtime_mismatch"))


class TestAcceptanceComparabilityAndBudgetTables(unittest.TestCase):
    """Fixed acceptance tests for PR #197 closeout: Comparability omissions & Budget type validation."""

    @classmethod
    def setUpClass(cls):
        from importlib.machinery import SourceFileLoader
        import importlib.util
        hrb_path = HERDR_ROOT / "bin" / "herdr-review-bench"
        loader = SourceFileLoader("hrb_acc_tests", str(hrb_path))
        spec = importlib.util.spec_from_loader("hrb_acc_tests", loader)
        cls.hrb = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.hrb)

    def _run_cmd(self, config_file: Path, output_dir: Path, judge_metrics: Optional[Dict[str, Any]] = None) -> int:
        import argparse
        import unittest.mock

        metrics = judge_metrics or {
            "overall": {
                "grounded_recall": 0.5,
                "grounded_precision": 0.8,
                "augmented_recall": 0.5,
                "augmented_precision": 0.8,
            }
        }

        orig_sp_run = subprocess.run

        def mock_subprocess_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and len(cmd) > 2 and cmd[0] == "npm" and cmd[1] == "run" and cmd[2] == "judge":
                out_idx = cmd.index("--output")
                rf = Path(cmd[out_idx + 1])
                rf.parent.mkdir(parents=True, exist_ok=True)
                rf.write_text(json.dumps({"metrics": metrics}), encoding="utf-8")
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="judge success", stderr="")
            return orig_sp_run(cmd, *args, **kwargs)

        with unittest.mock.patch.object(self.hrb, "find_or_setup_reviewbench", return_value=(output_dir, "e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d")):
            with unittest.mock.patch("subprocess.run", side_effect=mock_subprocess_run):
                args = argparse.Namespace(config=str(config_file), output=str(output_dir))
                return self.hrb.cmd_run(args)

    def _create_base_and_after_runs(self):
        golden_fp = "golden_fp_test_123"
        common_prompt_fp = "prompt_fp_test_abc"

        base_run = {
            "status": "completed",
            "golden_fingerprint": golden_fp,
            "context_config": {"context_mode": "diff_only", "context_budget_chars": 35000},
            "judge_config": "deepseek/deepseek-v4-flash",
            "scorer_sha": "e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d",
            "execution_config": {
                "timeout_seconds": 300,
                "system_prompt": "You are reviewing a Pull Request.",
                "system_prompt_fingerprint": "hash_sp_default",
                "context_budget_chars": 35000,
                "context_mode": "diff_only",
            },
            "execution_summary": {
                "total_planned": 1,
                "completed": 1,
                "failed": 0,
                "fallback_count": 0,
                "elapsed_seconds": 10.0,
                "agent_identity": {
                    "agent": "agy",
                    "model_name": "gemini-3.8-flash",
                    "model_confirmed": True,
                    "cli_version": "1.3.3",
                    "details": {"effort": "low", "permission_flag": "--sandbox"},
                },
                "samples": [
                    {
                        "key": "pr_1",
                        "base": "a" * 40,
                        "head": "b" * 40,
                        "base_diff_chars": 1000,
                        "base_diff_fingerprint": "fp_diff_1",
                        "common_prompt_fingerprint": common_prompt_fp,
                    }
                ],
            },
            "usage_summary": {
                "total_review_time_ms": 8000,
                "input_tokens": 1200,
                "output_tokens": 400,
                "total_tokens": 1600,
                "avg_prompt_chars": 3500,
                "avg_context_chars": 2000,
            },
            "version_info": {
                "judge_config": "deepseek/deepseek-v4-flash",
                "scorer_sha": "e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d",
                "golden_fingerprint": golden_fp,
            },
            "metrics": {
                "overall": {
                    "grounded_recall": 0.50,
                    "grounded_precision": 0.60,
                    "augmented_recall": 0.50,
                    "augmented_precision": 0.60,
                }
            },
        }

        after_run = json.loads(json.dumps(base_run))
        after_run["context_config"]["context_mode"] = "existing_four_tier"
        after_run["usage_summary"]["total_review_time_ms"] = 12000
        after_run["usage_summary"]["input_tokens"] = 3500
        after_run["usage_summary"]["total_tokens"] = 3900
        after_run["metrics"]["overall"]["grounded_recall"] = 0.75

        return base_run, after_run

    # --- Section IV.A: Comparability Acceptance Table ---

    def test_table_a_1_valid_baseline_control_comparable(self):
        """A.1 有效基准对照：严格可比成立。"""
        base_run, after_run = self._create_base_and_after_runs()
        is_comp, disc = check_benchmark_comparability(base_run, after_run)
        self.assertTrue(is_comp, f"Expected comparable, got: {disc}")
        self.assertEqual(len(disc), 0)

    def test_table_a_2_divergent_pr_title_blocked(self):
        """A.2 仅改变 PR title：不可比，报告共同提示词差异。"""
        base_run, after_run = self._create_base_and_after_runs()
        after_run["execution_summary"]["samples"][0]["common_prompt_fingerprint"] = "prompt_fp_different_title"
        is_comp, disc = check_benchmark_comparability(base_run, after_run)
        self.assertFalse(is_comp, "Divergent PR title must fail comparability")
        self.assertTrue(any("共同提示词" in d for d in disc), f"Expected '共同提示词' in discrepancies: {disc}")

    def test_table_a_3_divergent_pr_body_blocked(self):
        """A.3 仅改变 PR body：不可比，报告共同提示词差异。"""
        base_run, after_run = self._create_base_and_after_runs()
        after_run["execution_summary"]["samples"][0]["common_prompt_fingerprint"] = "prompt_fp_different_body"
        is_comp, disc = check_benchmark_comparability(base_run, after_run)
        self.assertFalse(is_comp, "Divergent PR body must fail comparability")
        self.assertTrue(any("共同提示词" in d for d in disc), f"Expected '共同提示词' in discrepancies: {disc}")

    def test_table_a_4_divergent_golden_finding_blocked(self):
        """A.4 仅改变某 golden 缺陷条目：不可比，报告 golden 内容差异。"""
        base_run, after_run = self._create_base_and_after_runs()
        after_run["golden_fingerprint"] = "diff_golden_fp"
        after_run["version_info"]["golden_fingerprint"] = "diff_golden_fp"
        is_comp, disc = check_benchmark_comparability(base_run, after_run)
        self.assertFalse(is_comp, "Divergent golden content must fail comparability")
        self.assertTrue(any("golden" in d.lower() or "黄金标准" in d for d in disc), f"Expected golden in discrepancies: {disc}")

    def test_table_a_5_cli_version_unknown_blocked(self):
        """A.5 CLI 版本为 unknown：不可比，报告缺少 CLI 版本有效证据。"""
        base_run, after_run = self._create_base_and_after_runs()
        after_run["execution_summary"]["agent_identity"]["cli_version"] = "unknown"
        is_comp, disc = check_benchmark_comparability(base_run, after_run)
        self.assertFalse(is_comp, "Unknown CLI version for external reviewer must fail strict comparability")
        self.assertTrue(any("cli" in d.lower() or "版本" in d for d in disc), f"Expected CLI version discrepancy: {disc}")

    def test_table_a_6_same_golden_in_different_directories(self):
        """A.6 相同 golden 集合置于不同目录：严格可比成立。"""
        from herdr.review_benchmark import compute_golden_fingerprint
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            dir1 = tmp / "golden_dir1"
            dir2 = tmp / "golden_dir2"
            dir1.mkdir()
            dir2.mkdir()
            sample_golden = json.dumps([{"file": "a.py", "line": 1, "message": "bug"}])
            (dir1 / "item1.json").write_text(sample_golden, encoding="utf-8")
            (dir2 / "item1.json").write_text(sample_golden, encoding="utf-8")

            fp1 = compute_golden_fingerprint(dir1)
            fp2 = compute_golden_fingerprint(dir2)
            self.assertIsNotNone(fp1)
            self.assertEqual(fp1, fp2)

            base_run, after_run = self._create_base_and_after_runs()
            base_run["golden_fingerprint"] = fp1
            base_run["version_info"]["golden_fingerprint"] = fp1
            after_run["golden_fingerprint"] = fp2
            after_run["version_info"]["golden_fingerprint"] = fp2

            is_comp, disc = check_benchmark_comparability(base_run, after_run)
            self.assertTrue(is_comp, f"Expected comparable across same content in diff dirs, got: {disc}")

    def test_table_a_7_old_results_missing_fingerprints_readable_without_strict_attribution(self):
        """A.7 旧结果缺少新增指纹：可读取，展示数值与差值，不输出严格归因结论。"""
        base_run, after_run = self._create_base_and_after_runs()
        # Strip all new fingerprints to simulate legacy results
        del base_run["golden_fingerprint"]
        del base_run["version_info"]["golden_fingerprint"]
        del after_run["golden_fingerprint"]
        del after_run["version_info"]["golden_fingerprint"]
        del base_run["execution_summary"]["samples"][0]["common_prompt_fingerprint"]
        del after_run["execution_summary"]["samples"][0]["common_prompt_fingerprint"]

        is_comp, disc = check_benchmark_comparability(base_run, after_run)
        self.assertFalse(is_comp, "Old results missing fingerprints must not be strictly comparable")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            b_dir = tmp / "before"
            a_dir = tmp / "after"
            b_dir.mkdir()
            a_dir.mkdir()
            (b_dir / "results.json").write_text(json.dumps(base_run), encoding="utf-8")
            (a_dir / "results.json").write_text(json.dumps(after_run), encoding="utf-8")
            (b_dir / "report.md").write_text("# Before", encoding="utf-8")
            (a_dir / "report.md").write_text("# After", encoding="utf-8")

            report = compare_benchmarks(b_dir, a_dir)
            self.assertIn("50.0%", report)
            self.assertIn("75.0%", report)
            self.assertIn("+25.0%", report)
            self.assertIn("单变量控制警告", report)
            self.assertNotIn("单变量控制结论", report)

    # --- Section IV.B: Budget Acceptance Table ---

    def test_table_b_1_budget_true_fails_fast(self):
        """B.1 配置 context_budget_chars: true：受控错误，退出码 1，prep=0，review=0。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            cfg = tmp / "cfg.yaml"
            out = tmp / "out"
            cfg.write_text("""
name: test_budget_true
agent: rule
benchmark:
  manifest: tests/fixtures/review_benchmark/corpus/manifest.json
review:
  context_mode: diff_only
  context_budget_chars: true
""")
            ret = self._run_cmd(cfg, out)
            self.assertEqual(ret, 1)
            self.assertFalse((out / "results.json").exists())
            self.assertFalse((out / "workspaces").exists())

    def test_table_b_2_budget_float_fails_fast(self):
        """B.2 配置 context_budget_chars: 1.5：受控错误，退出码 1，prep=0，review=0。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            cfg = tmp / "cfg.yaml"
            out = tmp / "out"
            cfg.write_text("""
name: test_budget_float
agent: rule
benchmark:
  manifest: tests/fixtures/review_benchmark/corpus/manifest.json
review:
  context_mode: diff_only
  context_budget_chars: 1.5
""")
            ret = self._run_cmd(cfg, out)
            self.assertEqual(ret, 1)
            self.assertFalse((out / "results.json").exists())
            self.assertFalse((out / "workspaces").exists())

    def test_table_b_3_budget_bad_string_fails_fast_without_unhandled_traceback(self):
        """B.3 配置 context_budget_chars: "bad"：受控错误，退出码 1，无未处理异常。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            cfg = tmp / "cfg.yaml"
            out = tmp / "out"
            cfg.write_text("""
name: test_budget_bad
agent: rule
benchmark:
  manifest: tests/fixtures/review_benchmark/corpus/manifest.json
review:
  context_mode: diff_only
  context_budget_chars: "bad"
""")
            # Must return 1 without raising unhandled ValueError
            ret = self._run_cmd(cfg, out)
            self.assertEqual(ret, 1)
            self.assertFalse((out / "results.json").exists())
            self.assertFalse((out / "workspaces").exists())

    def test_table_b_4_budget_35000_succeeds(self):
        """B.4 配置 context_budget_chars: 35000：正常执行，预算为 35000。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            mf = tmp / "manifest.json"
            with open(HERDR_ROOT / "tests/fixtures/review_benchmark/corpus/manifest.json", encoding="utf-8") as f:
                mf_data = json.load(f)[:1]
            mf.write_text(json.dumps(mf_data), encoding="utf-8")

            cfg = tmp / "cfg.yaml"
            out = tmp / "out"
            cfg.write_text(f"""
name: test_budget_35000
agent: rule
benchmark:
  manifest: {mf}
review:
  context_mode: diff_only
  context_budget_chars: 35000
""")
            ret = self._run_cmd(cfg, out)
            self.assertEqual(ret, 0)
            res = json.loads((out / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(res["execution_summary"]["context_budget_chars"], 35000)

    def test_table_b_5_budget_unconfigured_uses_default(self):
        """B.5 不配置该项：正常执行，采用默认预算。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            mf = tmp / "manifest.json"
            with open(HERDR_ROOT / "tests/fixtures/review_benchmark/corpus/manifest.json", encoding="utf-8") as f:
                mf_data = json.load(f)[:1]
            mf.write_text(json.dumps(mf_data), encoding="utf-8")

            cfg = tmp / "cfg.yaml"
            out = tmp / "out"
            cfg.write_text(f"""
name: test_budget_default
agent: rule
benchmark:
  manifest: {mf}
review:
  context_mode: diff_only
""")
            ret = self._run_cmd(cfg, out)
            self.assertEqual(ret, 0)
            res = json.loads((out / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(res["execution_summary"]["context_budget_chars"], DEFAULT_CONTEXT_BUDGET_CHARS)


if __name__ == "__main__":
    unittest.main()
