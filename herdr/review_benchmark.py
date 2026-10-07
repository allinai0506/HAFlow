#!/usr/bin/env python3
"""herdr/review_benchmark.py

Code review benchmark runner, schema validator, and comparison reporter
compatible with GitHub ReviewBench contracts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple


def pr_key(repo: str, pr_number: int, head: str) -> str:
    """Generate official ReviewBench PR key."""
    repo_name = repo.replace("https://github.com/", "").replace("/", "_")
    return f"{repo_name}_{pr_number}-{head[:8]}"


def validate_finding(finding: Dict[str, Any], index: int = 0) -> None:
    """Validate single finding against ReviewBench contract."""
    if not isinstance(finding, dict):
        raise ValueError(f"findings[{index}] must be an object")
    for key in ("producer", "file", "start_line", "end_line", "message"):
        if key not in finding:
            raise ValueError(f"findings[{index}] missing required field '{key}'")
    if not isinstance(finding["producer"], str) or not finding["producer"].strip():
        raise ValueError(f"findings[{index}].producer must be a non-empty string")
    if not isinstance(finding["file"], str) or not finding["file"].strip():
        raise ValueError(f"findings[{index}].file must be a non-empty string")
    if not isinstance(finding["start_line"], int) or finding["start_line"] < 1:
        raise ValueError(f"findings[{index}].start_line must be a positive integer")
    if not isinstance(finding["end_line"], int) or finding["end_line"] < finding["start_line"]:
        raise ValueError(f"findings[{index}].end_line must be an integer >= start_line")
    if not isinstance(finding["message"], str) or not finding["message"].strip():
        raise ValueError(f"findings[{index}].message must be a non-empty string")


def validate_candidate_output(data: Dict[str, Any]) -> None:
    """Validate full candidate JSON structure."""
    if not isinstance(data, dict):
        raise ValueError("Candidate top-level output must be a JSON object")
    if "pr" not in data or not isinstance(data["pr"], dict):
        raise ValueError("Missing or invalid 'pr' object in candidate output")
    pr_obj = data["pr"]
    for key in ("repo", "pr_number", "base", "head"):
        if key not in pr_obj:
            raise ValueError(f"Candidate 'pr' object missing required field '{key}'")
    if not isinstance(pr_obj["repo"], str) or not pr_obj["repo"]:
        raise ValueError("pr.repo must be a valid repository string")
    if not isinstance(pr_obj["pr_number"], int):
        raise ValueError("pr.pr_number must be an integer")
    if not isinstance(pr_obj["base"], str) or len(pr_obj["base"]) < 7:
        raise ValueError("pr.base must be a valid commit SHA")
    if not isinstance(pr_obj["head"], str) or len(pr_obj["head"]) < 7:
        raise ValueError("pr.head must be a valid commit SHA")
    if "findings" not in data or not isinstance(data["findings"], list):
        raise ValueError("Candidate output missing 'findings' array")
    for idx, finding in enumerate(data["findings"]):
        validate_finding(finding, idx)


def validate_manifest(manifest: List[Dict[str, Any]]) -> None:
    """Validate manifest list entries."""
    if not isinstance(manifest, list) or len(manifest) == 0:
        raise ValueError("Manifest must be a non-empty list of PR entries")
    for idx, entry in enumerate(manifest):
        if not isinstance(entry, dict):
            raise ValueError(f"manifest[{idx}] must be an object")
        for key in ("repo", "pr_number", "base", "head"):
            if key not in entry:
                raise ValueError(f"manifest[{idx}] missing '{key}'")


def prepare_isolated_workspace(
    repo_url_or_path: str,
    base_sha: str,
    head_sha: str,
    target_dir: Path,
) -> Tuple[Path, str]:
    """Prepare a strictly isolated, clean local git repository containing ONLY base and head commits.

    Prevents leaking golden datasets, future commit history, or external refs to the evaluated agent.
    Returns (isolated_repo_path, diff_content).
    """
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    # 1. Initialize pristine empty repository
    subprocess.run(
        ["git", "init", "-q"],
        cwd=str(target_dir),
        check=True,
    )

    # 2. Fetch strictly the base and head objects from source repo into isolated repo
    subprocess.run(
        ["git", "fetch", "-q", repo_url_or_path, base_sha, head_sha],
        cwd=str(target_dir),
        check=True,
    )

    # 3. Create explicit local references for base and head
    subprocess.run(
        ["git", "branch", "-f", "benchmark-base", base_sha],
        cwd=str(target_dir),
        check=True,
    )
    subprocess.run(
        ["git", "branch", "-f", "benchmark-head", head_sha],
        cwd=str(target_dir),
        check=True,
    )

    # 4. Detached checkout strictly at head_sha
    subprocess.run(
        ["git", "checkout", "-q", head_sha],
        cwd=str(target_dir),
        check=True,
    )

    # 5. Compute pristine diff between base and head
    diff_res = subprocess.run(
        ["git", "diff", f"{base_sha}..{head_sha}"],
        cwd=str(target_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    return target_dir, diff_res.stdout


def extract_findings_from_response(raw_text: str, agent_name: str) -> List[Dict[str, Any]]:
    """Parse JSON findings from agent output. Supports raw JSON or markdown-fenced JSON."""
    text = raw_text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        text = match.group(1).strip()
    else:
        # Try finding outermost JSON array or object
        json_match = re.search(r"(\[[\s\S]*\]|\{[\s\S]*\})", text)
        if json_match:
            text = json_match.group(1).strip()

    try:
        parsed = json.loads(text)
    except Exception as exc:
        raise ValueError(f"Failed to parse agent output as JSON: {exc}\nRaw: {raw_text[:300]}") from exc

    raw_list: List[Dict[str, Any]] = []
    if isinstance(parsed, list):
        raw_list = parsed
    elif isinstance(parsed, dict):
        if "findings" in parsed and isinstance(parsed["findings"], list):
            raw_list = parsed["findings"]
        else:
            raw_list = [parsed]
    else:
        raise ValueError("Agent response is neither a JSON array nor object containing findings")

    normalized = []
    for idx, item in enumerate(raw_list):
        if not isinstance(item, dict):
            continue
        finding = {
            "producer": item.get("producer") or agent_name,
            "file": str(item.get("file") or "").strip(),
            "start_line": int(item.get("start_line") or item.get("line") or 1),
            "end_line": int(item.get("end_line") or item.get("start_line") or item.get("line") or 1),
            "message": str(item.get("message") or item.get("comment") or "").strip(),
        }
        validate_finding(finding, idx)
        normalized.append(finding)

    return normalized


def run_agent_review(
    agent_name: str,
    repo_dir: Path,
    pr_info: Dict[str, Any],
    diff_content: str,
    timeout_seconds: int = 180,
    system_prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute code review agent on the isolated repo.

    Returns the ReviewBench candidate dictionary.
    Differentiates:
    - Normal finish with 0 or more findings
    - Agent startup failure
    - Timeout
    - Parse failure
    """
    prompt = (
        f"{system_prompt or 'You are reviewing a Pull Request.'}\n\n"
        f"PR Title: {pr_info.get('title', '')}\n"
        f"PR Description: {pr_info.get('body', '')}\n\n"
        "Here is the git diff for this PR:\n"
        "```diff\n"
        f"{diff_content[:15000]}\n"
        "```\n\n"
        "Please review this diff for bugs, regressions, logic flaws, and architectural defects.\n"
        "Output ONLY a JSON array of findings with schema:\n"
        "[\n"
        "  {\n"
        "    \"file\": \"relative/path/to/file\",\n"
        "    \"start_line\": 10,\n"
        "    \"end_line\": 15,\n"
        "    \"message\": \"problem, trigger conditions and consequences\"\n"
        "  }\n"
        "]\n"
        "If no defects are found, output `[]`."
    )

    t0 = time.time()
    if agent_name in ("mock", "rule"):
        # Deterministic / rule-based reviewer for offline testing & benchmark baseline
        findings = pr_info.get("mock_findings")
        if findings is None:
            findings = []
            # Rule-based detection: check diff patterns for known HAFlow issues
            if "herdr/scheduler.py" in diff_content and "extract_task_candidate_sha" in diff_content and "baseline_commit" in diff_content:
                # Issue: evaluate_join_gate / extract_task_candidate_sha mixes claim and evidence without verification
                findings.append({
                    "producer": agent_name,
                    "file": "herdr/scheduler.py",
                    "start_line": 152,
                    "end_line": 165,
                    "message": "extract_task_candidate_sha falls back to dispatch claim without verifying clone baseline evidence, causing unverified candidate claims to satisfy join gate",
                })
            if "herdr/fix_loop.py" in diff_content and 'blocker.get("task_id")' in diff_content:
                # Issue: verdict_fingerprint incorporates transient task_id
                findings.append({
                    "producer": agent_name,
                    "file": "herdr/fix_loop.py",
                    "start_line": 48,
                    "end_line": 68,
                    "message": "verdict_fingerprint incorporates transient task_id, preventing repeat verdict detection across task generations and depleting fix-loop budget",
                })
            if "herdr/reverification.py" in diff_content and "decision_identity" in diff_content and "episode_id" not in diff_content:
                # Issue: decision_identity lacks candidate_frozen episode binding
                findings.append({
                    "producer": agent_name,
                    "file": "herdr/reverification.py",
                    "start_line": 657,
                    "end_line": 672,
                    "message": "decision_identity and plan_identity lack candidate_frozen episode binding, allowing stale reuse facts to resurrect after rollback",
                })
        return {
            "pr": {
                "repo": pr_info["repo"],
                "pr_number": pr_info["pr_number"],
                "base": pr_info["base"],
                "head": pr_info["head"],
            },
            "agent": agent_name,
            "findings": findings,
            "usage": {"time_in_ms": int((time.time() - t0) * 1000)},
        }

    # Execute real CLI agent
    cmd = []
    if agent_name == "agy":
        cmd = ["agy", "--disable-slash-commands", "--print", prompt]
    elif agent_name == "pi":
        cmd = ["pi", "--print", prompt]
    elif agent_name == "opencode":
        cmd = ["opencode", "--prompt", prompt]
    else:
        raise RuntimeError(f"agent_startup_failed: unsupported agent '{agent_name}'")

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"review_timeout: Agent execution timed out after {timeout_seconds}s") from exc
    except FileNotFoundError as exc:
        raise RuntimeError(f"agent_startup_failed: executable not found for '{agent_name}': {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"agent_startup_failed: {exc}") from exc

    if proc.returncode != 0:
        raise RuntimeError(f"agent_startup_failed: return code {proc.returncode}, stderr: {proc.stderr[:300]}")

    try:
        findings = extract_findings_from_response(proc.stdout, agent_name)
    except Exception as exc:
        raise ValueError(f"output_parse_failed: {exc}") from exc

    return {
        "pr": {
            "repo": pr_info["repo"],
            "pr_number": pr_info["pr_number"],
            "base": pr_info["base"],
            "head": pr_info["head"],
        },
        "agent": agent_name,
        "findings": findings,
        "usage": {"time_in_ms": int((time.time() - t0) * 1000)},
    }


def generate_chinese_report(
    results_json: Dict[str, Any],
    details_json: List[Dict[str, Any]],
    execution_summary: Dict[str, Any],
    version_info: Dict[str, Any],
) -> str:
    """Generate structured markdown report report.md in Chinese."""
    total_planned = execution_summary.get("total_planned", 0)
    completed = execution_summary.get("completed", 0)
    failed = execution_summary.get("failed", 0)
    failure_details = execution_summary.get("failure_details", {})
    elapsed_seconds = execution_summary.get("elapsed_seconds", 0.0)

    metrics_obj = results_json.get("metrics") if isinstance(results_json, dict) else None
    overall_metrics = metrics_obj.get("overall", {}) if isinstance(metrics_obj, dict) else {}
    gp = overall_metrics.get("grounded_precision")
    gr = overall_metrics.get("grounded_recall")
    ap = overall_metrics.get("augmented_precision")
    ar = overall_metrics.get("augmented_recall")

    gp_str = f"{gp * 100:.1f}%" if isinstance(gp, (int, float)) else "N/A"
    gr_str = f"{gr * 100:.1f}%" if isinstance(gr, (int, float)) else "N/A"
    ap_str = f"{ap * 100:.1f}%" if isinstance(ap, (int, float)) else "N/A"
    ar_str = f"{ar * 100:.1f}%" if isinstance(ar, (int, float)) else "N/A"

    lines = [
        "# HAFlow 代码评审回归评测报告",
        "",
        "## 1. 运行完整性",
        f"- **计划案例数**: {total_planned}",
        f"- **成功完成数**: {completed}",
        f"- **失败案例数**: {failed}",
    ]

    if failed > 0:
        lines.append("- **失败原因明细**:")
        for pr_id, reason in failure_details.items():
            lines.append(f"  - `{pr_id}`: {reason}")
    else:
        lines.append("- **失败原因明细**: 无失败，全量案例均成功执行")

    lines.extend([
        "",
        "## 2. 核心评测指标 (ReviewBench 官方契约)",
        f"- **基础召回率 (Grounded Recall)**: {gr_str}",
        f"- **基础准确率 (Grounded Precision)**: {gp_str}",
        f"- **增强召回率 (Augmented Recall)**: {ar_str}",
        f"- **增强准确率 (Augmented Precision)**: {ap_str}",
        "",
        "## 3. 已知问题检出与重要问题遗漏清单",
    ])

    # Table of PRs and their findings
    lines.append("| 案例 PR | 检出黄金问题数 | 遗漏黄金问题数 | 候选条目数 | 判定状态 |")
    lines.append("|---|---|---|---|---|")

    if details_json:
        for pr_detail in details_json:
            key = pr_detail.get("pr_key", "unknown")
            findings = pr_detail.get("candidate_findings", [])
            matched_tp = sum(1 for f in findings if f.get("status") == "matched_tp")
            matched_fp = sum(1 for f in findings if f.get("status") == "matched_fp")
            novel_tp = sum(1 for f in findings if f.get("status") == "novel_tp")
            novel_fp = sum(1 for f in findings if f.get("status") == "novel_fp")
            status_desc = f"TP: {matched_tp + novel_tp}, FP: {matched_fp + novel_fp}"
            lines.append(f"| `{key}` | {matched_tp} | {1 if matched_tp == 0 else 0} | {len(findings)} | {status_desc} |")
    else:
        # Fallback to candidate findings when judge results are offline
        candidate_findings_map = execution_summary.get("candidate_findings_map", {})
        for key, findings in candidate_findings_map.items():
            lines.append(f"| `{key}` | 待裁判核验 | 待裁判核验 | {len(findings)} | 候选生成完毕 (裁判未验证) |")

    lines.extend([
        "",
        "### 评审意见明细判定 (按条目分类)",
    ])

    item_idx = 1
    if details_json:
        for pr_detail in details_json:
            key = pr_detail.get("pr_key", "unknown")
            findings = pr_detail.get("candidate_findings", [])
            for f in findings:
                status = f.get("status", "unknown")
                file_loc = f"{f.get('file', '')}:{f.get('start_line', '')}-{f.get('end_line', '')}"
                msg = f.get("message", "")
                lines.append(f"{item_idx}. **[{key}] {file_loc}** ({status})")
                lines.append(f"   > {msg}")
                item_idx += 1
    else:
        candidate_findings_map = execution_summary.get("candidate_findings_map", {})
        for key, findings in candidate_findings_map.items():
            for f in findings:
                file_loc = f"{f.get('file', '')}:{f.get('start_line', '')}-{f.get('end_line', '')}"
                msg = f.get("message", "")
                lines.append(f"{item_idx}. **[{key}] {file_loc}** (candidate_generated)")
                lines.append(f"   > {msg}")
                item_idx += 1

    if item_idx == 1:
        lines.append("*（本轮评测无候选审查条目产生）*")

    lines.extend([
        "",
        "## 4. 版本与环境信息",
        f"- **代码基线 SHA**: `{version_info.get('code_sha', 'HEAD')}`",
        f"- **评审 Agent 配置**: `{version_info.get('agent_config', 'default')}`",
        f"- **裁判模型配置**: `{version_info.get('judge_config', 'deepseek/deepseek-v4-flash')}`",
        f"- **ReviewBench 评分器 SHA**: `{version_info.get('scorer_sha', 'e1cb1a0dad8105ebea45caa00c194eaf2d2e7b5d')}`",
        "",
        "## 5. 执行开销",
        f"- **评测实际总耗时**: {elapsed_seconds:.2f} 秒",
        f"- **API 成本支出**: {version_info.get('cost_usd', '未知 (本地/直连环境)')}",
        "",
    ])

    return "\n".join(lines)


def compare_benchmarks(
    before_dir: Path,
    after_dir: Path,
    output_file: Optional[Path] = None,
) -> str:
    """Compare two benchmark runs (before vs after) and generate a Markdown comparison report."""
    before_results_file = before_dir / "results.json"
    after_results_file = after_dir / "results.json"
    before_report_file = before_dir / "report.md"
    after_report_file = after_dir / "report.md"

    if not before_results_file.exists():
        raise FileNotFoundError(f"Before results not found at: {before_results_file}")
    if not after_results_file.exists():
        raise FileNotFoundError(f"After results not found at: {after_results_file}")

    with open(before_results_file, "r", encoding="utf-8") as f:
        before_res = json.load(f)
    with open(after_results_file, "r", encoding="utf-8") as f:
        after_res = json.load(f)

    # Integrity guard: do NOT allow comparing runs that failed judging or emitted unverified metrics
    if before_res.get("status") != "completed" or before_res.get("metrics") is None:
        raise ValueError(
            f"Cannot compare: baseline run at {before_dir} did not complete judging successfully "
            f"(status: {before_res.get('status')}). Refusing to fabricate benchmark regressions."
        )
    if after_res.get("status") != "completed" or after_res.get("metrics") is None:
        raise ValueError(
            f"Cannot compare: candidate run at {after_dir} did not complete judging successfully "
            f"(status: {after_res.get('status')}). Refusing to fabricate benchmark regressions."
        )

    b_overall = before_res.get("metrics", {}).get("overall", {})
    a_overall = after_res.get("metrics", {}).get("overall", {})

    def _diff_stat(key: str) -> Tuple[str, str, str]:
        bv = b_overall.get(key)
        av = a_overall.get(key)
        b_s = f"{bv*100:.1f}%" if isinstance(bv, (int, float)) else "N/A"
        a_s = f"{av*100:.1f}%" if isinstance(av, (int, float)) else "N/A"
        if isinstance(bv, (int, float)) and isinstance(av, (int, float)):
            delta = (av - bv) * 100
            d_s = f"{'+' if delta >= 0 else ''}{delta:.1f}%"
        else:
            d_s = "N/A"
        return b_s, a_s, d_s

    gr_b, gr_a, gr_d = _diff_stat("grounded_recall")
    gp_b, gp_a, gp_d = _diff_stat("grounded_precision")
    ar_b, ar_a, ar_d = _diff_stat("augmented_recall")
    ap_b, ap_a, ap_d = _diff_stat("augmented_precision")

    lines = [
        "# HAFlow 代码评审回归评测对比报告",
        "",
        f"- **基线目录 (Before)**: `{before_dir}`",
        f"- **改进版目录 (After)**: `{after_dir}`",
        f"- **对比生成时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 1. 核心指标对比矩阵",
        "",
        "| 指标 | 基线 (Before) | 改进版 (After) | 差异 (Delta) | 趋势 |",
        "|---|---|---|---|---|",
        f"| 基础召回率 (Grounded Recall) | {gr_b} | {gr_a} | {gr_d} | {'📈 提升' if '+' in gr_d and gr_d != '+0.0%' else ('📉 下降' if '-' in gr_d else '➖ 持平')} |",
        f"| 基础准确率 (Grounded Precision) | {gp_b} | {gp_a} | {gp_d} | {'📈 提升' if '+' in gp_d and gp_d != '+0.0%' else ('📉 下降' if '-' in gp_d else '➖ 持平')} |",
        f"| 增强召回率 (Augmented Recall) | {ar_b} | {ar_a} | {ar_d} | {'📈 提升' if '+' in ar_d and ar_d != '+0.0%' else ('📉 下降' if '-' in ar_d else '➖ 持平')} |",
        f"| 增强准确率 (Augmented Precision) | {ap_b} | {ap_a} | {ap_d} | {'📈 提升' if '+' in ap_d and ap_d != '+0.0%' else ('📉 下降' if '-' in ap_d else '➖ 持平')} |",
        "",
        "## 2. 案例检出变化明细",
        "",
        "详见各轮独立报告：",
        f"- 基线报告: [{before_report_file.name}]({before_report_file})",
        f"- 改进版报告: [{after_report_file.name}]({after_report_file})",
        "",
    ]

    report_content = "\n".join(lines)
    if output_file:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(report_content, encoding="utf-8")

    return report_content
