#!/usr/bin/env python3
"""Minimal headless Triad Spike (Executor + Deterministic Gate + Heterogeneous Reviewer + Rule Judge).

Temporary experiment script (Stage 1) to measure whether heterogeneous adversarial review
catches real P0/drift/test-tampering issues beyond single-agent + deterministic gates.
Does NOT write to state.db or touch herdr-controller.
"""

from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_MAX_CHANGED_LINES = 200
DEFAULT_TIMEOUT_SECONDS = 1200
MAX_REVIEW_ROUNDS = 2
MAX_GATE_RETRIES = 2
MAX_DIFF_CHARS = 60_000
FORBIDDEN_PATH_PATTERNS = ("*.db", "*.sqlite", "*.sqlite3", ".env", ".env.*")

CONTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["goal", "non_goals", "allowed_paths", "max_changed_lines", "acceptance_cmd"],
    "properties": {
        "goal": {"type": "string", "minLength": 1},
        "non_goals": {"type": "array", "items": {"type": "string"}},
        "allowed_paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "max_changed_lines": {"type": "integer", "minimum": 1},
        "acceptance_cmd": {"type": "string", "minLength": 1},
    },
}

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "block"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "file", "line", "claim", "evidence", "contract_clause"],
                "properties": {
                    "severity": {"type": "string", "enum": ["P0", "P1", "P2"]},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "claim": {"type": "string"},
                    "evidence": {"type": "string"},
                    "contract_clause": {
                        "type": "string",
                        "enum": ["goal", "non_goals", "allowed_paths", "acceptance"],
                    },
                },
            },
        },
    },
}


@dataclasses.dataclass
class CmdResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    duration_sec: float = 0.0


def run_cmd(
    cmd: list[str] | str,
    cwd: Path,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    shell: bool = False,
) -> CmdResult:
    """Run subprocess in its own process group and kill the group on timeout."""
    start = time.monotonic()
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        shell=shell,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return CmdResult(
            returncode=proc.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
            timed_out=False,
            duration_sec=round(time.monotonic() - start, 2),
        )
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
        return CmdResult(
            returncode=124,
            stdout=stdout or "",
            stderr=(stderr or "") + f"\n[TIMEOUT after {timeout}s]",
            timed_out=True,
            duration_sec=round(time.monotonic() - start, 2),
        )


def _match_allowed_path(path: str, allowed_patterns: list[str]) -> bool:
    for pattern in allowed_patterns:
        pat = pattern.rstrip("/")
        if fnmatch.fnmatch(path, pat) or path == pat or path.startswith(pat + "/"):
            return True
    return False


def _is_forbidden_path(path: str) -> bool:
    name = Path(path).name
    return any(fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(path, pat) for pat in FORBIDDEN_PATH_PATTERNS)


def scan_test_tampering(diff_text: str) -> list[str]:
    """Heuristically detect deleted assertions or added skip/xfail markers in test files."""
    warnings: list[str] = []
    current_file = ""
    is_test_file = False
    removed_asserts = 0
    added_skips = 0

    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
            is_test_file = bool(re.search(r"(^|/)tests?/|test_.*\.py$|_test\.py$|\.test\.[jt]sx?$", current_file))
            continue
        if not is_test_file or line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("-") and re.search(r"\b(assert|assertEqual|assertTrue|assertFalse|assertRaises|expect\()\b", line):
            removed_asserts += 1
            warnings.append(f"[{current_file}] 删除断言: {line[1:].strip()[:120]}")
        elif line.startswith("+") and re.search(r"(@pytest\.mark\.(skip|xfail)|@unittest\.skip|\.skip\(|xit\(|xtest\()", line):
            added_skips += 1
            warnings.append(f"[{current_file}] 新增跳过测试标记: {line[1:].strip()[:120]}")

    if removed_asserts > 0 or added_skips > 0:
        warnings.insert(0, f"共检测到 {removed_asserts} 处断言删除、{added_skips} 处 skip/xfail 新增，请审核者重点核查。")
    return warnings


def evaluate_scope(changed_files: list[str], numstat_output: str, contract: dict[str, Any]) -> list[str]:
    """Deterministic scope guard against allowed_paths, max_changed_lines, and forbidden files."""
    errors: list[str] = []
    allowed = contract.get("allowed_paths", [])
    max_lines = int(contract.get("max_changed_lines", DEFAULT_MAX_CHANGED_LINES))

    for fpath in changed_files:
        if _is_forbidden_path(fpath):
            errors.append(f"触及禁改文件: {fpath}")
        elif allowed and not _match_allowed_path(fpath, allowed):
            errors.append(f"越界修改文件（不在 allowed_paths 中）: {fpath}")

    total_lines = 0
    for line in numstat_output.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            total_lines += int(parts[0]) + int(parts[1])
    if total_lines > max_lines:
        errors.append(f"改动行数 {total_lines} 超过上限 {max_lines}")
    return errors


def normalize_review_output(raw_review: dict[str, Any]) -> dict[str, Any]:
    """Enforce P0-only blocking and require concrete file+line+evidence for P0."""
    findings = []
    has_valid_p0 = False
    for item in raw_review.get("findings", []):
        f = dict(item)
        sev = f.get("severity", "P2")
        if sev == "P0":
            has_loc = bool(str(f.get("file", "")).strip()) and int(f.get("line") or 0) > 0
            has_ev = bool(str(f.get("evidence", "")).strip())
            if not (has_loc and has_ev):
                f["severity"] = "P1"
                f["claim"] = f"[自动降级为P1: 缺少file/line/evidence] {f.get('claim', '')}"
            else:
                has_valid_p0 = True
        findings.append(f)

    verdict = "block" if has_valid_p0 else "pass"
    return {"verdict": verdict, "findings": findings}


def decide_next_action(gate_passed: bool, normalized_review: dict[str, Any] | None, review_round: int, max_rounds: int = MAX_REVIEW_ROUNDS) -> str:
    """Pure deterministic rule judge."""
    if not gate_passed:
        return "retry_executor_gate"
    if normalized_review is None:
        raise ValueError("normalized_review required when gate_passed is True")
    if normalized_review["verdict"] == "pass":
        return "approve_pr"
    if review_round < max_rounds:
        return "retry_executor_review"
    return "escalate_human"


def _extract_claude_json(stdout: str) -> dict[str, Any]:
    data = json.loads(stdout)
    if isinstance(data, dict) and "structured_output" in data and isinstance(data["structured_output"], dict):
        return data["structured_output"]
    if isinstance(data, dict) and "result" in data and isinstance(data["result"], str):
        return json.loads(data["result"])
    if isinstance(data, dict):
        return data
    raise ValueError(f"Unexpected claude JSON output: {stdout[:300]}")


def call_structured_agent(
    agent: str,
    prompt: str,
    schema: dict[str, Any],
    cwd: Path,
    read_only: bool,
    timeout: int,
) -> dict[str, Any]:
    """Invoke claude or codex headlessly with strict JSON schema output."""
    if agent == "claude":
        cmd = [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, ensure_ascii=False),
            "--no-session-persistence",
        ]
        if read_only:
            cmd.extend(["--permission-mode", "plan", "--disallowedTools", "Edit,Write,Bash"])
        else:
            cmd.extend(["--permission-mode", "acceptEdits"])
        cmd.append(prompt)
        res = run_cmd(cmd, cwd=cwd, timeout=timeout)
        if res.returncode != 0:
            raise RuntimeError(f"claude exited {res.returncode}: {res.stderr[:500]}")
        return _extract_claude_json(res.stdout)

    if agent == "codex":
        with tempfile.TemporaryDirectory(prefix="triad-codex-") as tmp:
            schema_path = Path(tmp) / "schema.json"
            out_path = Path(tmp) / "out.json"
            schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
            sandbox = "read-only" if read_only else "workspace-write"
            cmd = [
                "codex",
                "exec",
                "--sandbox",
                sandbox,
                "--output-schema",
                str(schema_path),
                "-o",
                str(out_path),
                prompt,
            ]
            res = run_cmd(cmd, cwd=cwd, timeout=timeout)
            if res.returncode != 0:
                raise RuntimeError(f"codex exited {res.returncode}: {res.stderr[:500]}")
            return json.loads(out_path.read_text(encoding="utf-8"))

    raise ValueError(f"Unsupported agent: {agent}")


def call_executor_agent(agent: str, prompt: str, cwd: Path, timeout: int) -> CmdResult:
    """Invoke claude or codex headlessly to implement changes in the isolated worktree."""
    if agent == "claude":
        cmd = [
            "claude",
            "-p",
            "--permission-mode",
            "acceptEdits",
            "--no-session-persistence",
            prompt,
        ]
        return run_cmd(cmd, cwd=cwd, timeout=timeout)
    if agent == "codex":
        cmd = [
            "codex",
            "exec",
            "--sandbox",
            "workspace-write",
            prompt,
        ]
        return run_cmd(cmd, cwd=cwd, timeout=timeout)
    raise ValueError(f"Unsupported executor agent: {agent}")


def ensure_committed(worktree: Path, message: str) -> str:
    """Stage and commit any uncommitted changes inside the isolated worktree; return HEAD SHA."""
    run_cmd(["git", "add", "-u"], cwd=worktree, timeout=30)
    untracked = run_cmd(["git", "ls-files", "--others", "--exclude-standard"], cwd=worktree, timeout=30)
    for rel in untracked.stdout.splitlines():
        rel = rel.strip()
        if not rel or "__pycache__" in rel or ".pytest_cache" in rel or rel.endswith(".pyc"):
            continue
        run_cmd(["git", "add", "--", rel], cwd=worktree, timeout=30)
    staged = run_cmd(["git", "diff", "--cached", "--name-only"], cwd=worktree, timeout=30)
    if staged.stdout.strip():
        commit_res = run_cmd(["git", "commit", "-m", message], cwd=worktree, timeout=30)
        if commit_res.returncode != 0:
            raise RuntimeError(f"git commit failed: {commit_res.stderr}")
    head = run_cmd(["git", "rev-parse", "HEAD"], cwd=worktree, timeout=30)
    return head.stdout.strip()


def run_deterministic_gate(
    worktree: Path,
    base_sha: str,
    contract: dict[str, Any],
    extra_test_cmd: str | None = None,
) -> dict[str, Any]:
    """Run all deterministic checks (scope, acceptance_cmd, optional test_cmd, tampering scan)."""
    files_res = run_cmd(["git", "diff", "--name-only", f"{base_sha}..HEAD"], cwd=worktree, timeout=30)
    changed_files = [f.strip() for f in files_res.stdout.splitlines() if f.strip()]
    numstat_res = run_cmd(["git", "diff", "--numstat", f"{base_sha}..HEAD"], cwd=worktree, timeout=30)
    diff_res = run_cmd(["git", "diff", f"{base_sha}..HEAD"], cwd=worktree, timeout=30)

    errors: list[str] = []
    if not changed_files:
        errors.append("没有任何代码改动（diff 为空）")
    errors.extend(evaluate_scope(changed_files, numstat_res.stdout, contract))

    acc_res = run_cmd(contract["acceptance_cmd"], cwd=worktree, timeout=300, shell=True)
    if acc_res.returncode != 0:
        errors.append(f"acceptance_cmd 失败 (exit={acc_res.returncode})")

    extra_res = None
    if extra_test_cmd:
        extra_res = run_cmd(extra_test_cmd, cwd=worktree, timeout=300, shell=True)
        if extra_res.returncode != 0:
            errors.append(f"extra_test_cmd 失败 (exit={extra_res.returncode})")

    tampering_warnings = scan_test_tampering(diff_res.stdout)
    return {
        "passed": len(errors) == 0,
        "errors": errors,
        "changed_files": changed_files,
        "acceptance_exit": acc_res.returncode,
        "acceptance_output": (acc_res.stdout + "\n" + acc_res.stderr)[-4000:],
        "extra_test_exit": extra_res.returncode if extra_res else None,
        "tampering_warnings": tampering_warnings,
        "diff_text": diff_res.stdout,
    }


def check_baseline_freshness(repo: Path, base_ref: str, base_sha: str) -> tuple[bool, str]:
    """Check whether local base_sha is still at origin/<base_ref> if remote exists."""
    fetch = run_cmd(["git", "fetch", "origin", base_ref], cwd=repo, timeout=60)
    if fetch.returncode != 0:
        return True, "无远端或 fetch 跳过，保留本地基线检查"
    remote_sha = run_cmd(["git", "rev-parse", f"origin/{base_ref}"], cwd=repo, timeout=30).stdout.strip()
    if remote_sha and remote_sha != base_sha:
        return False, f"BASELINE STALE: 本地基线 {base_sha[:8]} 落后于 origin/{base_ref} ({remote_sha[:8]})"
    return True, "基线最新"


def write_summary(
    run_dir: Path,
    run_id: str,
    task: str,
    executor: str,
    reviewer: str,
    verdict: str,
    duration_sec: float,
    gate_catches: int,
    reviewer_p0_count: int,
    branch: str,
    contract: dict[str, Any],
    final_review: dict[str, Any] | None,
) -> None:
    lines = [
        f"# Triad Run Summary: {run_id}",
        "",
        f"- **任务**: {task}",
        f"- **分支**: `{branch}`",
        f"- **Executor / Reviewer**: `{executor}` / `{reviewer}`",
        f"- **最终裁决**: `{verdict}`",
        f"- **总耗时**: {duration_sec:.1f}s",
        f"- **门禁独立拦截轮次**: {gate_catches}",
        f"- **审核者 P0 数量**: {reviewer_p0_count}",
        "",
        "## 验收契约",
        f"- Goal: {contract['goal']}",
        f"- Non-goals: {', '.join(contract.get('non_goals', [])) or '无'}",
        f"- Allowed paths: {', '.join(contract['allowed_paths'])}",
        f"- Acceptance cmd: `{contract['acceptance_cmd']}`",
        "",
        "## 审核者结论",
    ]
    if final_review:
        lines.append(f"- Verdict: `{final_review['verdict']}`")
        for f in final_review.get("findings", []):
            lines.append(f"  - [{f['severity']}] `{f['file']}:{f['line']}` ({f['contract_clause']}): {f['claim']} — 证据: {f['evidence']}")
    else:
        lines.append("- 未进入审核阶段")

    lines.extend([
        "",
        "## 人工盲审记录（请测试完成后填写）",
        "- [ ] 1. 这个改动能否不经大改直接合并？（是 / 否，原因：______）",
        "- [ ] 2. 审核者拦下的问题（如有）是不是真实有效的问题？（是 / 误判 / 无拦截）",
        "- [ ] 3. 对比 `single_agent_snapshot.diff` 与 `final.diff`，多 Agent 审核是否带来了额外价值？",
    ])
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HAFlow Triad Spike Runner")
    parser.add_argument("--repo", required=True, help="目标 Git 仓库路径")
    parser.add_argument("--task", required=True, help="任务需求描述")
    parser.add_argument("--base", default="main", help="基准分支（默认 main）")
    parser.add_argument("--executor", default="claude", choices=["claude", "codex"])
    parser.add_argument("--reviewer", default="codex", choices=["claude", "codex"])
    parser.add_argument("--contract", help="可选：已有契约 JSON 文件路径")
    parser.add_argument("--test-cmd", help="可选：额外项目测试命令")
    parser.add_argument("--runs-dir", help="运行记录目录（默认 <repo>/.triad-runs）")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--skip-confirm", action="store_true", help="跳过契约人工确认")
    parser.add_argument("--allow-passing-baseline", action="store_true", help="允许基线上验收命令已通过（如重构任务）")
    args = parser.parse_args(argv)

    if args.executor == args.reviewer:
        print("[ERROR] 禁止同模型自审：executor 与 reviewer 必须来自不同厂商", file=sys.stderr)
        return 2

    repo = Path(args.repo).resolve()
    run_id = datetime.now(timezone.utc).strftime("triad-%Y%m%d-%H%M%S")
    runs_dir = Path(args.runs_dir).resolve() if args.runs_dir else (repo / ".triad-runs")
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    worktree = run_dir / "worktree"
    branch = f"triad/{run_id}"

    base_sha = run_cmd(["git", "rev-parse", args.base], cwd=repo, timeout=30).stdout.strip()
    if not base_sha:
        print(f"[ERROR] 无法解析基准分支: {args.base}", file=sys.stderr)
        return 2

    if args.contract:
        contract = json.loads(Path(args.contract).read_text(encoding="utf-8"))
    else:
        draft_prompt = (
            f"请根据以下需求，在只读模式下检查当前仓库并起草一份最小验收契约 JSON。\n"
            f"需求：{args.task}\n"
            f"要求：allowed_paths 必须尽量精确；acceptance_cmd 必须是一条改动前会失败、改动后会通过的可执行命令。"
        )
        print(f"[1/5] 正在使用 {args.executor} 起草验收契约...")
        contract = call_structured_agent(args.executor, draft_prompt, CONTRACT_SCHEMA, repo, read_only=True, timeout=180)

    contract_path = run_dir / "contract.json"
    contract_path.write_text(json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\n=== 验收契约 ===")
    print(contract_path.read_text(encoding="utf-8"))

    if not args.skip_confirm:
        ans = input("按 Enter 确认契约并继续，输入 q 退出，或手动编辑上述 contract.json 后输入 r 重新读取: ").strip().lower()
        if ans == "q":
            return 0
        if ans == "r":
            contract = json.loads(contract_path.read_text(encoding="utf-8"))

    wt_res = run_cmd(["git", "worktree", "add", str(worktree), "-b", branch, base_sha], cwd=repo, timeout=60)
    if wt_res.returncode != 0:
        print(f"[ERROR] 创建 worktree 失败: {wt_res.stderr}", file=sys.stderr)
        return 2

    t0 = time.monotonic()
    gate_catches = 0
    reviewer_p0_count = 0
    final_review = None
    verdict = "error"

    try:
        if not args.allow_passing_baseline:
            pre_res = run_cmd(contract["acceptance_cmd"], cwd=worktree, timeout=120, shell=True)
            if pre_res.returncode == 0:
                print("[ERROR] acceptance_cmd 在基线上已经通过，无法作为“先失败后转绿”的证明。请修改验收命令或加 --allow-passing-baseline。", file=sys.stderr)
                return 2

        feedback = ""
        review_round = 0
        gate_round = 0
        snapshot_saved = False

        while True:
            exec_prompt = (
                f"你正在隔离的 git worktree 中完成开发任务。严禁执行 git push。\n"
                f"验收契约：\n{json.dumps(contract, ensure_ascii=False, indent=2)}\n"
                f"需求：{args.task}\n"
            )
            if feedback:
                exec_prompt += f"\n上一轮打回原因，请只针对以下问题修复并自测：\n{feedback}\n"
            exec_prompt += "\n完成后请 commit 你的改动。"

            print(f"[2/5] 执行者 ({args.executor}) 工作中...")
            exec_res = call_executor_agent(args.executor, exec_prompt, worktree, args.timeout)
            if exec_res.timed_out:
                verdict = "timeout"
                break
            ensure_committed(worktree, f"triad: executor iteration")

            gate_round += 1
            print(f"[3/5] 运行确定性门禁 (第 {gate_round} 次)...")
            gate = run_deterministic_gate(worktree, base_sha, contract, args.test_cmd)
            (run_dir / f"gate_round_{gate_round}.json").write_text(
                json.dumps({k: v for k, v in gate.items() if k != "diff_text"}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            if not gate["passed"]:
                gate_catches += 1
                if gate_round >= MAX_GATE_RETRIES + MAX_REVIEW_ROUNDS:
                    verdict = "gate_exhausted"
                    break
                feedback = "确定性门禁未通过：\n" + "\n".join(gate["errors"]) + "\n输出末尾：\n" + gate["acceptance_output"][-1500:]
                continue

            if not snapshot_saved:
                (run_dir / "single_agent_snapshot.diff").write_text(gate["diff_text"], encoding="utf-8")
                snapshot_saved = True

            review_round += 1
            print(f"[4/5] 异构审核者 ({args.reviewer}, 只读, 第 {review_round} 轮) 审查中...")
            diff_slice = gate["diff_text"][:MAX_DIFF_CHARS]
            if len(gate["diff_text"]) > MAX_DIFF_CHARS:
                diff_slice += f"\n\n[DIFF 已截断，原始长度 {len(gate['diff_text'])} 字符]"

            round_rule = (
                "这是第 2 轮复审：只核验第 1 轮指出的 P0 问题是否已修复，以及是否引入了新的 P0 回归，不要发散寻找新问题。"
                if review_round >= 2
                else "请对照验收契约严格审查是否存在 P0 阻断缺陷、需求漂移或测试作弊。只有真实阻塞合并的严重缺陷才可标为 P0，且必须给出精确的 file、line 和 evidence。"
            )
            review_prompt = (
                f"{round_rule}\n\n"
                f"=== 验收契约 ===\n{json.dumps(contract, ensure_ascii=False, indent=2)}\n\n"
                f"=== 测试篡改扫描警告 ===\n{json.dumps(gate['tampering_warnings'], ensure_ascii=False)}\n\n"
                f"=== 确定性门禁输出 ===\nacceptance_exit={gate['acceptance_exit']}\n{gate['acceptance_output'][-2000:]}\n\n"
                f"=== git diff {base_sha[:8]}..HEAD ===\n{diff_slice}"
            )
            raw_review = call_structured_agent(args.reviewer, review_prompt, REVIEW_SCHEMA, worktree, read_only=True, timeout=600)
            final_review = normalize_review_output(raw_review)
            (run_dir / f"review_round_{review_round}.json").write_text(
                json.dumps(final_review, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            p0_items = [f for f in final_review["findings"] if f["severity"] == "P0"]
            reviewer_p0_count += len(p0_items)

            action = decide_next_action(True, final_review, review_round, MAX_REVIEW_ROUNDS)
            if action == "approve_pr":
                fresh, msg = check_baseline_freshness(repo, args.base, base_sha)
                verdict = "approved" if fresh else "baseline_stale"
                print(f"[5/5] 裁判结论: {verdict} ({msg})")
                break
            if action == "retry_executor_review":
                feedback = "异构审核者发现 P0 阻断问题：\n" + json.dumps(p0_items, ensure_ascii=False, indent=2)
                continue
            verdict = "escalate_human"
            print("[5/5] 裁判结论: escalate_human (满 2 轮仍存在 P0 争议，交人工裁决)")
            break

        final_diff = run_cmd(["git", "diff", f"{base_sha}..HEAD"], cwd=worktree, timeout=30).stdout
        (run_dir / "final.diff").write_text(final_diff, encoding="utf-8")
    finally:
        duration = round(time.monotonic() - t0, 2)
        write_summary(
            run_dir, run_id, args.task, args.executor, args.reviewer,
            verdict, duration, gate_catches, reviewer_p0_count, branch, contract, final_review,
        )
        print(f"\n运行完成，结果保存在: {run_dir / 'summary.md'}")
        print(f"隔离工作区保留在: {worktree}（分支 {branch}），确认无用后可执行 git worktree remove {worktree}")

    return 0 if verdict == "approved" else 1


if __name__ == "__main__":
    raise SystemExit(main())
