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
    DEFAULT_CONTEXT_BUDGET_CHARS,
    DEFAULT_CONTEXT_MODE,
    VALID_CONTEXT_MODES,
    check_benchmark_comparability,
    compare_benchmarks,
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
            "context_config": {"context_mode": "diff_only", "context_budget_chars": 35000},
            "execution_summary": {
                "total_planned": 1,
                "completed": 1,
                "fallback_count": 0,
                "elapsed_seconds": 10.0,
                "agent_identity": {
                    "agent": "agy",
                    "model_name": "gemini-3.8-flash",
                    "model_confirmed": True,
                    "cli_version": "1.3.3",
                },
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
            "context_config": {"context_mode": "diff_only", "context_budget_chars": 35000},
            "judge_config": "deepseek/deepseek-v4-flash",
            "scorer_sha": "e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d",
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
                    {"key": "pr_1", "base": "aaa", "head": "bbb", "base_diff_chars": 1000},
                    {"key": "pr_2", "base": "ccc", "head": "ddd", "base_diff_chars": 2000},
                ],
            },
        }

        # 1. PR keys mismatch
        cand_keys = json.loads(json.dumps(base_run))
        cand_keys["context_config"]["context_mode"] = "existing_four_tier"
        cand_keys["execution_summary"]["samples"] = [
            {"key": "pr_1", "base": "aaa", "head": "bbb", "base_diff_chars": 1000},
            {"key": "pr_999", "base": "ccc", "head": "ddd", "base_diff_chars": 2000},
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
            "context_config": {"context_mode": "diff_only", "context_budget_chars": 35000},
            "judge_config": "deepseek/deepseek-v4-flash",
            "scorer_sha": "e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d",
            "execution_summary": {
                "total_planned": 1,
                "completed": 1,
                "failed": 0,
                "fallback_count": 0,
                "agent_identity": {"agent": "agy", "model_name": "gemini", "model_confirmed": True},
                "samples": [{"key": "1"}],
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


if __name__ == "__main__":
    unittest.main()
