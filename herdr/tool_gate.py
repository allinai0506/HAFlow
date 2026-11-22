"""Deterministic tool-node gate execution for HAFlow workflows (W1).

Runs non-LLM verification (scope guard, test tampering scan, and configured
verification commands) against a frozen candidate_sha and records a structured
gate task + workflow note for downstream review and PR delivery.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

FORBIDDEN_PATH_PATTERNS = ("*.db", "*.sqlite", "*.sqlite3", ".env", ".env.*")


def scan_test_tampering(diff_text: str) -> list[str]:
    """Detect deleted assertions or added skip/xfail markers in test files."""
    warnings: list[str] = []
    current_file = ""
    is_test_file = False
    removed_asserts = 0
    added_skips = 0

    for line in (diff_text or "").splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
            is_test_file = bool(
                re.search(r"(^|/)tests?/|test_.*\.py$|_test\.py$|\.test\.[jt]sx?$", current_file)
            )
            continue
        if not is_test_file or line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("-") and re.search(
            r"\b(assert|assertEqual|assertTrue|assertFalse|assertRaises|expect\()\b", line
        ):
            removed_asserts += 1
            warnings.append(f"[{current_file}] 删除断言: {line[1:].strip()[:120]}")
        elif line.startswith("+") and re.search(
            r"(@pytest\.mark\.(skip|xfail)|@unittest\.skip|\.skip\(|xit\(|xtest\()", line
        ):
            added_skips += 1
            warnings.append(f"[{current_file}] 新增跳过测试标记: {line[1:].strip()[:120]}")

    if removed_asserts > 0 or added_skips > 0:
        warnings.insert(
            0,
            f"共检测到 {removed_asserts} 处断言删除、{added_skips} 处 skip/xfail 新增，请审核者重点核查。",
        )
    return warnings


def evaluate_scope(
    changed_files: list[str],
    numstat_output: str,
    allowed_paths: list[str] | None = None,
    max_changed_lines: int | None = None,
) -> list[str]:
    """Check changed files against forbidden patterns, allowed_paths, and line budget."""
    errors: list[str] = []
    for fpath in changed_files:
        name = Path(fpath).name
        if any(fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(fpath, pat) for pat in FORBIDDEN_PATH_PATTERNS):
            errors.append(f"触及禁改文件: {fpath}")
        elif allowed_paths:
            matched = any(
                fnmatch.fnmatch(fpath, p.rstrip("/"))
                or fpath == p.rstrip("/")
                or fpath.startswith(p.rstrip("/") + "/")
                for p in allowed_paths
            )
            if not matched:
                errors.append(f"越界修改文件: {fpath}")

    if max_changed_lines is not None and max_changed_lines > 0:
        total = 0
        for line in (numstat_output or "").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
                total += int(parts[0]) + int(parts[1])
        if total > max_changed_lines:
            errors.append(f"改动行数 {total} 超过上限 {max_changed_lines}")
    return errors


def _run_git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


def run_tool_gate(
    store: Any,
    workflow_id: str,
    node: dict[str, Any],
    *,
    project_root: str,
    candidate_sha: str,
    base_branch: str = "main",
    verify_commands: list[str] | None = None,
    work_dir: str | None = None,
) -> dict[str, Any]:
    """Execute deterministic gate checks and persist a completed gate task + note."""
    node_id = str(node.get("id") or "gate")
    workflow = store.get_workflow(workflow_id) or {}
    repo = Path(work_dir or project_root).expanduser()

    errors: list[str] = []
    tampering_warnings: list[str] = []
    changed_files: list[str] = []
    cmd_outputs: list[str] = []

    if repo.is_dir() and candidate_sha:
        try:
            base_ref = base_branch or "main"
            merge_base = _run_git(repo, "merge-base", base_ref, candidate_sha).strip()
            diff_range = f"{merge_base}..{candidate_sha}"
            changed_files = [
                f.strip()
                for f in _run_git(repo, "diff", "--name-only", diff_range).splitlines()
                if f.strip()
            ]
            numstat = _run_git(repo, "diff", "--numstat", diff_range)
            diff_text = _run_git(repo, "diff", diff_range)
            if not changed_files:
                errors.append("candidate_sha 相对基线没有任何代码改动")
            errors.extend(evaluate_scope(changed_files, numstat))
            tampering_warnings = scan_test_tampering(diff_text)
        except Exception as exc:
            errors.append(f"git diff 检查失败: {exc}")
    else:
        errors.append("缺少有效的 project_root 或 candidate_sha")

    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if repo.is_dir() and candidate_sha and verify_commands:
        checkout_dir = Path(tempfile.mkdtemp(prefix="herdr-tool-gate-"))
        worktree_added = False
        try:
            _run_git(repo, "worktree", "add", "--detach", str(checkout_dir), candidate_sha)
            worktree_added = True
            for cmd in verify_commands:
                try:
                    res = subprocess.run(
                        cmd,
                        cwd=str(checkout_dir),
                        shell=True,
                        capture_output=True,
                        text=True,
                        env=env,
                        timeout=300,
                    )
                    tail = (res.stdout + "\n" + res.stderr).strip()[-1000:]
                    cmd_outputs.append(f"$ {cmd} -> exit={res.returncode}\n{tail}")
                    if res.returncode != 0:
                        errors.append(f"验证命令失败 (exit={res.returncode}): {cmd}")
                except (subprocess.TimeoutExpired, OSError) as exc:
                    errors.append(f"验证命令执行异常 ({type(exc).__name__}): {cmd}")
        except Exception as exc:
            errors.append(f"隔离检出 candidate_sha 失败: {exc}")
        finally:
            if worktree_added:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(checkout_dir)],
                    cwd=str(repo),
                    capture_output=True,
                    timeout=30,
                )
            shutil.rmtree(checkout_dir, ignore_errors=True)

    verdict = "pass" if not errors else "blocked"
    note_lines = [
        f"HERDR_GATE_VERDICT: {verdict}",
        f"candidate_sha: {candidate_sha}",
        f"changed_files ({len(changed_files)}): {', '.join(changed_files[:30])}",
    ]
    if errors:
        note_lines.append("errors:\n- " + "\n- ".join(errors))
    if tampering_warnings:
        note_lines.append("tampering_warnings:\n- " + "\n- ".join(tampering_warnings))
    if cmd_outputs:
        note_lines.append("command_outputs:\n" + "\n---\n".join(cmd_outputs))
    verdict_note = "\n".join(note_lines)

    existing = [
        t for t in store.list_tasks(workflow_id=workflow_id)
        if (t.get("node") or t.get("stage")) == node_id
    ]
    round_num = len(existing) + 1
    task_id = f"{workflow_id}-{node_id}-r{round_num}" if round_num > 1 else f"{workflow_id}-{node_id}"
    now = time.time()
    task_record = {
        "task_id": task_id,
        "run_id": f"run_{uuid.uuid4().hex}",
        "workflow_id": workflow_id,
        "node": node_id,
        "stage": node_id,
        "role": "test",
        "dispatch_role": "test",
        "agent": "tool",
        "status": "completed",
        "candidate_sha": candidate_sha,
        "verified_candidate_sha": candidate_sha,
        "execution_id": workflow.get("execution_id") or workflow_id,
        "candidate_episode_id": workflow.get("candidate_episode_id"),
        "stage_verdict": verdict,
        "stage_verdict_note": verdict_note[:4000],
        "created_at": now,
        "updated_at": now,
    }
    store.save_task(task_record)

    try:
        from . import workflow_docs
        workflow_docs.append_note(
            workflow_id,
            kind="gate",
            title=f"{node_id} 确定性门禁 ({verdict})",
            body=verdict_note,
            node=node_id,
            task_id=task_id,
            source="herdr-controller",
        )
    except Exception:
        pass

    return {
        "task_id": task_id,
        "verdict": verdict,
        "errors": errors,
        "tampering_warnings": tampering_warnings,
        "changed_files": changed_files,
    }
