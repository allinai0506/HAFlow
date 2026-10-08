"""HAFlow Reviewer Auto PR Review Bot (V1).

Integrates Rule Review, AI Shadow Review, Finding Verifier, and GitHub Actions PR commenting.
Enforces:
1. Single comment per PR (update existing, do not spam).
2. Anti-stale SHA guard (old commit results cannot overwrite new commit results).
3. Secret redaction on all comments and audit artifacts.
4. Tri-state primary status and 4-state shadow status.
5. Strict artifact generation (review-result.json, shadow-review.json, context-audit.json, finding-verification.json).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.request

from herdr.finding_verifier import verify_findings
from herdr.review_benchmark import review_pr


MARKER_PREFIX = "<!-- haflow-auto-pr-review"

REDACT_PATTERNS = [
    re.compile(r"gh[posr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{20,}", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"(api[_-]?key|token|secret)[\s:=]+['\"]?([A-Za-z0-9_\-]{16,})['\"]?", re.IGNORECASE),
]


def redact_sensitive_content(text: str) -> str:
    """Redact tokens, API keys, and sensitive environment variables from text."""
    if not text:
        return text

    redacted = str(text)

    # Redact pattern matches
    for pattern in REDACT_PATTERNS:
        redacted = pattern.sub("***REDACTED***", redacted)

    # Redact known environment secrets if present and long enough
    for env_var in ("GITHUB_TOKEN", "GH_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"):
        val = os.environ.get(env_var, "").strip()
        if len(val) >= 8 and val in redacted:
            redacted = redacted.replace(val, "***REDACTED***")

    return redacted


def format_chinese_review_report(
    pr_number: int | str,
    head_sha: str,
    review_result: Dict[str, Any],
    verification_summary: Dict[str, Any],
    retained_findings: List[Dict[str, Any]],
) -> str:
    """Format structured Chinese PR review comment adhering to Section VI contract."""
    rule_status = review_result.get("status", "unknown")
    primary = review_result.get("primary", {})
    primary_findings = review_result.get("findings", [])
    rule_findings_count = len(primary_findings)

    shadow = review_result.get("shadow", {}) or {}
    shadow_status = review_result.get("shadow_status") or shadow.get("status", "shadow_skipped")
    shadow_agent = shadow.get("agent") or "agy"
    shadow_usage = shadow.get("usage", {})
    shadow_time_ms = shadow_usage.get("time_in_ms")
    shadow_time_str = f"{shadow_time_ms} ms" if shadow_time_ms is not None else "N/A"

    # Context size calculation
    shadow_audit = shadow.get("audit") or review_result.get("audit")
    if shadow_audit and isinstance(shadow_audit, dict):
        ctx_chars = shadow_audit.get("assembled_chars") or shadow_audit.get("chars")
        ctx_str = f"{ctx_chars} 字符" if ctx_chars is not None else "N/A"
    else:
        ctx_str = "N/A"

    counts = verification_summary.get("counts", {})
    verified_count = counts.get("verified", 0)
    rejected_count = counts.get("rejected", 0)
    uncertain_count = counts.get("uncertain", 0)

    lines = [
        "### HAFlow Automated Code Review",
        "",
        f"**PR：** #{pr_number}",
        "",
        f"**Commit：** `{head_sha}`",
        "",
        "**Rule Review：**",
        f"- 状态：`{rule_status}`",
        f"- Findings 数量：{rule_findings_count}",
        "",
        "**AI Shadow Review：**",
        f"- 状态：`{shadow_status}`",
        f"- 模型：`{shadow_agent}`",
        f"- 执行耗时：{shadow_time_str}",
        f"- Context 大小：{ctx_str}",
        "",
        "**Finding Verification：**",
        f"- Verified：{verified_count}",
        f"- Rejected：{rejected_count}",
        f"- Uncertain：{uncertain_count}",
        "",
    ]

    # Handle shadow status callouts
    shadow_err = shadow.get("error") or shadow.get("reason") or review_result.get("shadow_skip_reason")
    if shadow_status == "shadow_skipped":
        reason = shadow_err or "未配置安全的 LLM 运行环境，依安全策略跳过影子审核"
        lines.append(f"> ℹ️ **AI 影子审核说明**：{reason}")
        lines.append("")
    elif shadow_status == "shadow_timeout":
        lines.append("> ⚠️ **AI 影子审核超时**：影子评审在设定时限内未完成，已记录为 `shadow_timeout`（不影响 Rule 主门禁结果）。")
        lines.append("")
    elif shadow_status == "shadow_failed":
        err_msg = shadow_err or "执行异常"
        lines.append(f"> ⚠️ **AI 影子审核失败**：{err_msg}（影子审核失败，禁止显示为零缺陷；不影响 Rule 主门禁结果）。")
        lines.append("")

    lines.append("### Findings")
    lines.append("")

    if not retained_findings:
        lines.append("本轮未报告缺陷，不代表代码不存在缺陷。")
    else:
        for idx, f in enumerate(retained_findings, 1):
            file_loc = f"{f.get('file', 'unknown')}:{f.get('start_line', '?')}-{f.get('end_line', '?')}"
            ver_status = f.get("verification_status", "uncertain")
            msg = f.get("message", "")
            reason = f.get("verification_reason", "")
            lines.append(f"{idx}. **[{file_loc}]** (`{ver_status}`)")
            lines.append(f"   - **问题描述**：{msg}")
            lines.append(f"   - **验证状态**：`{ver_status}`")
            if reason:
                lines.append(f"   - **证据**：{reason}")
            lines.append("")

    lines.append(f"{MARKER_PREFIX}: head_sha={head_sha} pr={pr_number} -->")
    raw_md = "\n".join(lines)
    return redact_sensitive_content(raw_md)


def build_audit_artifacts(
    output_dir: Path,
    review_result: Dict[str, Any],
    verification_summary: Dict[str, Any],
    retained_findings: List[Dict[str, Any]],
    rejected_findings: List[Dict[str, Any]],
    comment_md: str,
) -> Dict[str, Path]:
    """Save the 4 mandatory audit artifacts plus comment.md into output_dir."""
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. review-result.json
    review_file = output_dir / "review-result.json"
    review_file.write_text(
        redact_sensitive_content(json.dumps(review_result, indent=2, ensure_ascii=False)),
        encoding="utf-8",
    )

    # 2. shadow-review.json
    shadow_file = output_dir / "shadow-review.json"
    shadow_data = review_result.get("shadow")
    if not shadow_data:
        shadow_data = {
            "status": review_result.get("shadow_status", "shadow_skipped"),
            "agent": "agy",
            "reason": review_result.get("shadow_skip_reason", "Shadow review not executed"),
            "findings": [],
        }
    shadow_file.write_text(
        redact_sensitive_content(json.dumps(shadow_data, indent=2, ensure_ascii=False)),
        encoding="utf-8",
    )

    # 3. context-audit.json
    context_file = output_dir / "context-audit.json"
    audit_data = review_result.get("audit") or review_result.get("primary", {}).get("audit") or shadow_data.get("audit")
    if not audit_data:
        audit_data = {
            "status": "not_applicable",
            "reason": "Reviewer executed directly against git diff or context retrieval was not triggered",
        }
    context_file.write_text(
        redact_sensitive_content(json.dumps(audit_data, indent=2, ensure_ascii=False)),
        encoding="utf-8",
    )

    # 4. finding-verification.json
    verification_file = output_dir / "finding-verification.json"
    verification_payload = {
        "summary": verification_summary,
        "retained_findings": retained_findings,
        "rejected_findings": rejected_findings,
    }
    verification_file.write_text(
        redact_sensitive_content(json.dumps(verification_payload, indent=2, ensure_ascii=False)),
        encoding="utf-8",
    )

    # 5. comment.md
    comment_file = output_dir / "comment.md"
    comment_file.write_text(comment_md, encoding="utf-8")

    return {
        "review_result": review_file,
        "shadow_review": shadow_file,
        "context_audit": context_file,
        "finding_verification": verification_file,
        "comment_md": comment_file,
    }


def post_or_update_pr_comment(
    repo_slug: str,
    pr_number: int | str,
    head_sha: str,
    comment_body: str,
    github_token: Optional[str] = None,
    api_base: str = "https://api.github.com",
) -> Dict[str, Any]:
    """Create or update a single bot review comment on GitHub PR with stale SHA guard."""
    token = github_token or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return {"status": "skipped", "reason": "no_github_token_provided"}

    clean_body = redact_sensitive_content(comment_body)
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "HAFlow-PR-Review-Bot/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Step 1: Check PR current head SHA to prevent stale writes (§5, §7)
    pr_url = f"{api_base}/repos/{repo_slug}/pulls/{pr_number}"
    req_pr = urllib.request.Request(pr_url, headers=headers)
    try:
        with urllib.request.urlopen(req_pr, timeout=15) as resp:
            pr_data = json.loads(resp.read().decode("utf-8"))
            current_head = pr_data.get("head", {}).get("sha", "")
            if current_head and current_head != head_sha:
                return {
                    "status": "stale_sha_skipped",
                    "reason": f"current PR head ({current_head}) != review head ({head_sha}); aborted stale overwrite",
                    "current_head": current_head,
                    "target_head": head_sha,
                }
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            return {"status": "permission_denied", "reason": "fork_pr_or_read_only_token", "status_code": 403}
        return {"status": "api_error", "reason": f"failed to query PR metadata: HTTP {exc.code} {exc.reason}"}
    except Exception as exc:
        return {"status": "api_error", "reason": f"failed to query PR metadata: {exc}"}

    # Step 2: Query existing comments to find bot marker
    comments_url = f"{api_base}/repos/{repo_slug}/issues/{pr_number}/comments"
    req_comments = urllib.request.Request(comments_url, headers=headers)
    existing_comment_id = None
    try:
        with urllib.request.urlopen(req_comments, timeout=15) as resp:
            comments = json.loads(resp.read().decode("utf-8"))
            for c in comments:
                body = c.get("body", "")
                if MARKER_PREFIX in body:
                    existing_comment_id = c.get("id")
                    break
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            return {"status": "permission_denied", "reason": "fork_pr_or_read_only_token", "status_code": 403}
        return {"status": "api_error", "reason": f"failed to list comments: HTTP {exc.code} {exc.reason}"}
    except Exception as exc:
        return {"status": "api_error", "reason": f"failed to list comments: {exc}"}

    # Step 3: Update existing comment or create new comment
    if existing_comment_id:
        update_url = f"{api_base}/repos/{repo_slug}/issues/comments/{existing_comment_id}"
        req_update = urllib.request.Request(
            update_url,
            data=json.dumps({"body": clean_body}).encode("utf-8"),
            headers=headers,
            method="PATCH",
        )
        try:
            with urllib.request.urlopen(req_update, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return {
                    "status": "updated",
                    "comment_id": existing_comment_id,
                    "comment_url": result.get("html_url"),
                }
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                return {"status": "permission_denied", "reason": "fork_pr_or_read_only_token", "status_code": 403}
            return {"status": "api_error", "reason": f"failed to update comment: HTTP {exc.code} {exc.reason}"}
        except Exception as exc:
            return {"status": "api_error", "reason": f"failed to update comment: {exc}"}
    else:
        req_create = urllib.request.Request(
            comments_url,
            data=json.dumps({"body": clean_body}).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req_create, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return {
                    "status": "created",
                    "comment_id": result.get("id"),
                    "comment_url": result.get("html_url"),
                }
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                return {"status": "permission_denied", "reason": "fork_pr_or_read_only_token", "status_code": 403}
            return {"status": "api_error", "reason": f"failed to create comment: HTTP {exc.code} {exc.reason}"}
        except Exception as exc:
            return {"status": "api_error", "reason": f"failed to create comment: {exc}"}


def execute_auto_pr_review(
    repo_dir: Path,
    base_sha: str,
    head_sha: str,
    pr_number: int | str,
    pr_title: str = "",
    pr_body: str = "",
    output_dir: Path = Path(".omc/review_artifacts"),
    github_token: Optional[str] = None,
    repo_slug: Optional[str] = None,
    reviewer_agent: str = "rule",
    shadow_agent: str = "agy",
    timeout_seconds: int = 180,
    api_base: str = "https://api.github.com",
) -> Dict[str, Any]:
    """Execute end-to-end PR auto review workflow."""
    repo_path = Path(repo_dir).resolve()
    pr_info = {
        "repo": repo_slug or "allinai0506/HAFlow",
        "pr_number": int(pr_number) if str(pr_number).isdigit() else 0,
        "base": base_sha,
        "head": head_sha,
        "title": pr_title or f"PR #{pr_number}",
        "body": pr_body or "",
    }

    # 1. Execute core review
    review_result = review_pr(
        repo_dir=repo_path,
        base_ref=base_sha,
        head_ref=head_sha,
        pr_info=pr_info,
        reviewer_agent=reviewer_agent,
        shadow_agent=shadow_agent,
        shadow_mode=True,
        timeout_seconds=timeout_seconds,
    )

    # 2. Fact-check candidate findings with Finding Verifier
    all_findings = []
    # Primary findings
    for f in review_result.get("findings", []):
        all_findings.append(f)
    # Shadow findings (if any)
    shadow_obj = review_result.get("shadow", {})
    if shadow_obj and isinstance(shadow_obj, dict):
        for f in shadow_obj.get("findings", []):
            if f not in all_findings:
                all_findings.append(f)

    retained_findings = []
    rejected_findings = []
    if all_findings:
        retained_findings, rejected_findings, verification_summary = verify_findings(
            findings=all_findings,
            repo_dir=repo_path,
            head_commit=head_sha,
        )
    else:
        verification_summary = {
            "counts": {"total": 0, "verified": 0, "rejected": 0, "uncertain": 0},
            "elapsed_ms": 0,
        }

    # 3. Format Chinese report
    comment_md = format_chinese_review_report(
        pr_number=pr_number,
        head_sha=head_sha,
        review_result=review_result,
        verification_summary=verification_summary,
        retained_findings=retained_findings,
    )

    # 4. Save audit artifacts
    artifact_paths = build_audit_artifacts(
        output_dir=output_dir,
        review_result=review_result,
        verification_summary=verification_summary,
        retained_findings=retained_findings,
        rejected_findings=rejected_findings,
        comment_md=comment_md,
    )

    # 5. Post or update PR comment if repo_slug is known
    comment_result = {"status": "skipped", "reason": "no_repo_slug_provided"}
    target_slug = repo_slug or os.environ.get("GITHUB_REPOSITORY")
    if target_slug:
        comment_result = post_or_update_pr_comment(
            repo_slug=target_slug,
            pr_number=pr_number,
            head_sha=head_sha,
            comment_body=comment_md,
            github_token=github_token,
            api_base=api_base,
        )

    return {
        "status": review_result.get("status"),
        "shadow_status": review_result.get("shadow_status"),
        "review_result": review_result,
        "verification_summary": verification_summary,
        "retained_findings": retained_findings,
        "rejected_findings": rejected_findings,
        "comment_result": comment_result,
        "artifact_paths": {k: str(v) for k, v in artifact_paths.items()},
    }
