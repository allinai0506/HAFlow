"""tests/test_auto_pr_review.py

Automated test suite for HAFlow Reviewer Auto PR Review V1.
Covers all 12 mandatory criteria from Section IX:
1. PR opened auto-trigger workflow contract.
2. synchronize trigger and re-review on updated commit.
3. Rule success and AI shadow success.
4. Rule success but AI shadow timeout (does not alter Rule gate).
5. Rule success but AI unavailable (shadow_skipped).
6. Reviewer failure state propagation.
7. Finding Verifier returns uncertain (retained safely).
8. Idempotent PR comment update (single bot comment, no spam).
9. Stale SHA guard (older SHA cannot overwrite newer SHA).
10. Fork PR / untrusted code permission safety (no token write leaks, graceful 403 handling).
11. Security boundary: unisolated runner forbids dangerous CLI / flags.
12. Sensitive credential redaction in comments and audit artifacts.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

HERDR_ROOT = Path(__file__).resolve().parent.parent

from herdr.finding_verifier import verify_findings
from herdr.pr_review_bot import (
    MARKER_PREFIX,
    build_audit_artifacts,
    execute_auto_pr_review,
    format_chinese_review_report,
    post_or_update_pr_comment,
    redact_sensitive_content,
)
from herdr.review_benchmark import review_diff, run_agent_review


class TestAutoPRReview(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp_dir.name)
        # Create minimal git repository
        (self.repo / "test.py").write_text("def sample():\n    return 42\n", encoding="utf-8")

    def tearDown(self):
        self.tmp_dir.cleanup()

    # 1. PR opened 自动触发
    def test_01_pr_opened_workflow_contract(self):
        workflow_file = HERDR_ROOT / ".github" / "workflows" / "ha-review.yml"
        self.assertTrue(workflow_file.exists(), "ha-review.yml workflow file must exist")
        content = workflow_file.read_text(encoding="utf-8")

        self.assertIn("pull_request:", content)
        self.assertIn("- opened", content)
        self.assertIn("- synchronize", content)
        self.assertIn("- reopened", content)
        self.assertNotIn("pull_request_target", content, "Must not use pull_request_target")
        self.assertIn("concurrency:", content)
        self.assertIn("cancel-in-progress: true", content)
        # Security boundary: PR title and body must be passed via env variables to prevent script injection
        self.assertIn("PR_TITLE: ${{ github.event.pull_request.title }}", content)
        self.assertIn("PR_BODY: ${{ github.event.pull_request.body }}", content)
        self.assertIn('--pr-title "$PR_TITLE"', content)
        self.assertIn('--pr-body "$PR_BODY"', content)
        self.assertNotIn('--pr-title "${{ github.event.pull_request.title }}"', content)
        self.assertNotIn('--pr-body "${{ github.event.pull_request.body }}"', content)

    # 2. synchronize 更新后重新审核
    def test_02_synchronize_trigger_and_commit_alignment(self):
        workflow_file = HERDR_ROOT / ".github" / "workflows" / "ha-review.yml"
        content = workflow_file.read_text(encoding="utf-8")

        # Must fetch and checkout the actual PR head commit (not temporary merge commit)
        self.assertIn("${{ github.event.pull_request.head.sha }}", content)
        self.assertIn("${{ github.event.pull_request.base.sha }}", content)
        self.assertIn("--base", content)
        self.assertIn("--head", content)

    # 3. Rule 正常且 AI 正常
    def test_03_rule_success_and_ai_success(self):
        review_result = {
            "status": "success",
            "shadow_status": "shadow_success",
            "agent": "rule",
            "findings": [],
            "shadow": {
                "status": "shadow_success",
                "agent": "agy",
                "findings": [],
                "usage": {"time_in_ms": 1500},
                "audit": {"assembled_chars": 12000},
            },
        }
        verif_summary = {"counts": {"total": 0, "verified": 0, "rejected": 0, "uncertain": 0}}
        report = format_chinese_review_report(
            pr_number=190,
            head_sha="abcdef1234567890abcdef1234567890abcdef12",
            review_result=review_result,
            verification_summary=verif_summary,
            retained_findings=[],
        )

        self.assertIn("状态：`success`", report)
        self.assertIn("状态：`shadow_success`", report)
        self.assertIn("模型：`agy`", report)
        self.assertIn("1500 ms", report)
        self.assertIn("12000 字符", report)
        self.assertIn("本轮未报告缺陷，不代表代码不存在缺陷。", report)

    # 4. Rule 正常但 AI 超时
    def test_04_rule_success_and_ai_timeout(self):
        diff = "--- a/test.py\n+++ b/test.py\n@@ -1 +1 @@\n-def sample():\n+def sample_v2():\n"
        with patch("herdr.review_benchmark.run_agent_review") as mock_run:
            def side_effect(agent_name, **kwargs):
                if agent_name == "rule":
                    return {"agent": "rule", "findings": [], "usage": {"time_in_ms": 10}}
                elif agent_name == "agy":
                    raise TimeoutError("review_timeout: Agent execution timed out after 180s")
                return {"agent": agent_name, "findings": []}
            mock_run.side_effect = side_effect

            # Allow agy shadow execution for this mock
            with patch.dict(os.environ, {"HERDR_ALLOW_AGY_SHADOW": "1", "HERDR_SECURE_LLM_RUNNER": "1"}):
                res = review_diff(self.repo, diff, reviewer_agent="rule", shadow_mode=True, shadow_agent="agy")

        # Crucial invariant: Rule success must NOT be compromised by shadow timeout
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["shadow_status"], "shadow_timeout")
        self.assertEqual(res["shadow"]["status"], "shadow_timeout")

        report = format_chinese_review_report(
            pr_number=191,
            head_sha="abcdef1234567890abcdef1234567890abcdef12",
            review_result=res,
            verification_summary={"counts": {"total": 0, "verified": 0, "rejected": 0, "uncertain": 0}},
            retained_findings=[],
        )
        self.assertIn("状态：`success`", report)
        self.assertIn("状态：`shadow_timeout`", report)
        self.assertIn("⚠️ **AI 影子审核超时**", report)

    # 5. Rule 正常但 AI 不可用
    def test_05_rule_success_and_ai_unavailable_skipped(self):
        diff = "--- a/test.py\n+++ b/test.py\n@@ -1 +1 @@\n-def sample():\n+def sample_v2():\n"
        # In default unisolated or agy-missing environment, shadow review is skipped safely
        with patch.dict(os.environ, {}, clear=True):
            res = review_diff(self.repo, diff, reviewer_agent="rule", shadow_mode=True, shadow_agent="agy")

        self.assertEqual(res["status"], "success")
        self.assertEqual(res["shadow_status"], "shadow_skipped")
        self.assertEqual(res["shadow"]["status"], "shadow_skipped")
        self.assertIn("shadow_skip_reason", res)

        report = format_chinese_review_report(
            pr_number=192,
            head_sha="abcdef1234567890abcdef1234567890abcdef12",
            review_result=res,
            verification_summary={"counts": {"total": 0, "verified": 0, "rejected": 0, "uncertain": 0}},
            retained_findings=[],
        )
        self.assertIn("状态：`success`", report)
        self.assertIn("状态：`shadow_skipped`", report)
        self.assertIn("ℹ️ **AI 影子审核说明**", report)

    # 6. Reviewer 执行失败
    def test_06_reviewer_failed_state(self):
        diff = "--- a/test.py\n+++ b/test.py\n@@ -1 +1 @@\n-def sample():\n+def sample_v2():\n"
        with patch("herdr.review_benchmark.run_agent_review") as mock_run:
            mock_run.side_effect = RuntimeError("Fatal syntax parse failure in base commit")
            res = review_diff(self.repo, diff, reviewer_agent="rule", shadow_mode=False)

        self.assertEqual(res["status"], "failed")
        self.assertIn("Fatal syntax parse failure", res.get("error", ""))

        report = format_chinese_review_report(
            pr_number=193,
            head_sha="abcdef1234567890abcdef1234567890abcdef12",
            review_result=res,
            verification_summary={"counts": {"total": 0, "verified": 0, "rejected": 0, "uncertain": 0}},
            retained_findings=[],
        )
        self.assertIn("状态：`failed`", report)

    # 7. Finding Verifier 返回 uncertain
    def test_07_finding_verifier_uncertain_retention(self):
        (self.repo / "herdr" / "custom.py").parent.mkdir(parents=True, exist_ok=True)
        (self.repo / "herdr" / "custom.py").write_text(
            "def handle_work(ctx):\n    # potential concurrency issue under high load\n    return ctx.get('val')\n",
            encoding="utf-8",
        )
        uncertain_finding = {
            "producer": "agy",
            "file": "herdr/custom.py",
            "start_line": 2,
            "end_line": 3,
            "message": "Potential race condition under high contention between workers",
        }
        retained, rejected, summary = verify_findings([uncertain_finding], repo_dir=self.repo)

        self.assertEqual(len(retained), 1)
        self.assertEqual(len(rejected), 0)
        self.assertEqual(summary["counts"]["uncertain"], 1)
        self.assertEqual(retained[0]["verification_status"], "uncertain")

        report = format_chinese_review_report(
            pr_number=194,
            head_sha="abcdef1234567890abcdef1234567890abcdef12",
            review_result={"status": "success", "findings": retained, "shadow_status": "shadow_success"},
            verification_summary=summary,
            retained_findings=retained,
        )
        self.assertIn("- Uncertain：1", report)
        self.assertIn("herdr/custom.py:2-3", report)
        self.assertIn("(`uncertain`)", report)

    # 8. 同一 PR 重跑更新评论，不新增重复评论
    def test_08_idempotent_comment_update_single_bot_comment(self):
        api_responses = {
            # GET PR info (SHA aligns)
            "/repos/allinai0506/HAFlow/pulls/10": {
                "head": {"sha": "sha_111"}
            },
            # GET comments (initially empty)
            "/repos/allinai0506/HAFlow/issues/10/comments": [
                {"id": 555, "body": "regular human comment"},
            ],
        }

        posted_comments = []
        updated_comments = []

        def mock_urlopen(req, timeout=15):
            url = req.full_url
            method = req.get_method()
            if "/pulls/10" in url:
                return io.BytesIO(json.dumps(api_responses["/repos/allinai0506/HAFlow/pulls/10"]).encode("utf-8"))
            elif "/issues/10/comments" in url and method == "GET":
                return io.BytesIO(json.dumps(api_responses["/repos/allinai0506/HAFlow/issues/10/comments"]).encode("utf-8"))
            elif "/issues/10/comments" in url and method == "POST":
                payload = json.loads(req.data.decode("utf-8"))
                posted_comments.append(payload)
                new_id = 999
                # update simulated server state so second call sees it
                api_responses["/repos/allinai0506/HAFlow/issues/10/comments"].append(
                    {"id": new_id, "body": payload["body"]}
                )
                return io.BytesIO(json.dumps({"id": new_id, "html_url": "https://github.com/allinai0506/HAFlow/issues/10#comment-999"}).encode("utf-8"))
            elif "/issues/comments/999" in url and method == "PATCH":
                payload = json.loads(req.data.decode("utf-8"))
                updated_comments.append(payload)
                return io.BytesIO(json.dumps({"id": 999, "html_url": "https://github.com/allinai0506/HAFlow/issues/10#comment-999"}).encode("utf-8"))
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            # Run 1: First comment created
            res1 = post_or_update_pr_comment(
                repo_slug="allinai0506/HAFlow",
                pr_number=10,
                head_sha="sha_111",
                comment_body=f"Review Run 1\n{MARKER_PREFIX}: head_sha=sha_111 pr=10 -->",
                github_token="dummy_token",
            )
            self.assertEqual(res1["status"], "created")
            self.assertEqual(len(posted_comments), 1)

            # Run 2: Re-run should update existing comment, NOT create a second one
            res2 = post_or_update_pr_comment(
                repo_slug="allinai0506/HAFlow",
                pr_number=10,
                head_sha="sha_111",
                comment_body=f"Review Run 2 (Updated)\n{MARKER_PREFIX}: head_sha=sha_111 pr=10 -->",
                github_token="dummy_token",
            )
            self.assertEqual(res2["status"], "updated")
            self.assertEqual(len(updated_comments), 1)
            self.assertEqual(len(posted_comments), 1, "Must never post duplicate comments")

    # 9. 旧 SHA 不覆盖新 SHA
    def test_09_stale_sha_does_not_overwrite_new_sha(self):
        def mock_urlopen(req, timeout=15):
            url = req.full_url
            if "/pulls/10" in url:
                # PR head on GitHub has already moved to 'sha_new'
                return io.BytesIO(json.dumps({"head": {"sha": "sha_new"}}).encode("utf-8"))
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res = post_or_update_pr_comment(
                repo_slug="allinai0506/HAFlow",
                pr_number=10,
                head_sha="sha_old",
                comment_body="Stale Review",
                github_token="dummy_token",
            )
            self.assertEqual(res["status"], "stale_sha_skipped")
            self.assertIn("aborted stale overwrite", res["reason"])

    # 10. Fork PR 或不可信代码不会获得写凭据
    def test_10_fork_pr_or_untrusted_code_permissions(self):
        def mock_urlopen(req, timeout=15):
            # GitHub returns HTTP 403 when a fork PR has read-only token
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res = post_or_update_pr_comment(
                repo_slug="allinai0506/HAFlow",
                pr_number=10,
                head_sha="sha_fork",
                comment_body="Fork PR Review",
                github_token="read_only_fork_token",
            )
            self.assertEqual(res["status"], "permission_denied")
            self.assertEqual(res["reason"], "fork_pr_or_read_only_token")

    # 11. 安全执行环境不满足要求时禁止启动危险 CLI
    def test_11_dangerous_cli_forbidden_in_unisolated_env(self):
        # 1. Verify default flags in run_agent_review use --sandbox and forbid --dangerously-skip-permissions
        with patch("shutil.which", return_value="/mock/agy"), patch("subprocess.run") as mock_proc:
            mock_proc.return_value = MagicMock(returncode=0, stdout='{"response": "[]"}')
            run_agent_review("agy", self.repo, {"pr_number": 1, "repo": "test", "base": "a", "head": "b"}, "diff", 60)
            executed_cmd = mock_proc.call_args[0][0]
            self.assertIn("--sandbox", executed_cmd)
            self.assertNotIn("--dangerously-skip-permissions", executed_cmd)

        # 2. Verify review_diff skips agy when secure runner isolation is absent
        with patch("shutil.which", return_value="/mock/agy"), patch.dict(os.environ, {}, clear=True):
            res = review_diff(self.repo, "diff", reviewer_agent="rule", shadow_mode=True, shadow_agent="agy")
            self.assertEqual(res["shadow_status"], "shadow_skipped")
            self.assertIn("Safe isolated LLM runner environment not configured", res["shadow"]["reason"])

    # 12. 评论内容不会泄漏密钥
    def test_12_sensitive_token_redaction(self):
        raw_text = (
            "Review generated with token ghp_1234567890abcdef1234567890abcdef and "
            "Authorization: Bearer my_secret_bearer_token_12345678 and api_key='sk-1234567890abcdef1234567890'"
        )
        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_1234567890abcdef1234567890abcdef"}):
            redacted = redact_sensitive_content(raw_text)

        self.assertNotIn("ghp_1234567890abcdef1234567890abcdef", redacted)
        self.assertNotIn("my_secret_bearer_token_12345678", redacted)
        self.assertNotIn("sk-1234567890abcdef1234567890", redacted)
        self.assertIn("***REDACTED***", redacted)

    # Verification of 4 audit artifacts generation
    def test_build_audit_artifacts_completeness(self):
        with tempfile.TemporaryDirectory() as art_dir:
            out_p = Path(art_dir)
            review_res = {
                "status": "success",
                "shadow_status": "shadow_skipped",
                "findings": [],
                "shadow": {"status": "shadow_skipped", "agent": "agy", "reason": "test skip"},
            }
            summary = {"counts": {"total": 0, "verified": 0, "rejected": 0, "uncertain": 0}}
            paths = build_audit_artifacts(
                output_dir=out_p,
                review_result=review_res,
                verification_summary=summary,
                retained_findings=[],
                rejected_findings=[],
                comment_md="## Sample Markdown",
            )
            self.assertTrue(Path(paths["review_result"]).exists())
            self.assertTrue(Path(paths["shadow_review"]).exists())
            self.assertTrue(Path(paths["context_audit"]).exists())
            self.assertTrue(Path(paths["finding_verification"]).exists())
            self.assertTrue(Path(paths["comment_md"]).exists())

            # Check JSON parseability
            for key in ("review_result", "shadow_review", "context_audit", "finding_verification"):
                with open(paths[key], "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.assertIsInstance(data, dict)


if __name__ == "__main__":
    unittest.main()
