"""FR-3 repo-hygiene decision layer (pure, no I/O).

Thin assembly lives in ``bin/herdr-task`` (integrate gate + launch
pre-check). All parsing/attribution decisions live here so the CLI stays
under the file-health budget and stays unit-testable without git.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

MAX_LISTED_FILES = 10
INTERNAL_EXACT = frozenset({".agent-task-context", ".herdr-launch-identity.json", ".herdr-loop", ".herdr", ".agents"})
INTERNAL_PREFIXES = (".herdr-loop/", ".herdr/", ".agents/skills/", ".agents/")


def is_internal_untracked(path: str) -> bool:
    """True for clone-infra untracked entries that must never trip exit 5."""
    text = str(path or "")
    if text in INTERNAL_EXACT:
        return True
    return text.startswith(INTERNAL_PREFIXES)


def is_launcher_owned(path: str, root: str | os.PathLike = "") -> bool:
    """True 当该 .agents/ 路径属于 launcher 拥有的基础设施（PR #355 评审遗留）。

    launcher 通过符号链接（cross-volume 时复制回退）把 universal skills
    挂进 ``.agents/skills/<name>``；任务自己创建的真实技能内容是业务
    交付物，不得被 ``.agents/`` 全树排除吞掉。所有权权威来源是装配时
    写入的 ``.herdr-launcher-managed.json`` manifest；缺失时回退 islink
    判别（PR #375 二轮评审：复制回退场景）。
    """
    parts = Path(str(path or "")).parts
    if ".agents" not in parts:
        return True
    idx = parts.index(".agents")
    rest = parts[idx + 1:]
    if not rest or rest[0] != "skills" or len(rest) < 2:
        return True  # .agents 顶层与其他子路径维持排除
    base = Path(root) if root else Path()
    manifest = base / ".agents" / "skills" / ".herdr-launcher-managed.json"
    try:
        if manifest.exists():
            managed = json.loads(manifest.read_text(encoding="utf-8"))
            return parts[idx + 2] in managed
    except (OSError, ValueError):
        pass
    skill_entry = base.joinpath(*parts[: idx + 3])
    try:
        return os.path.islink(str(skill_entry))
    except OSError:
        return True


def _decode_git_path(value: str) -> str:
    """Decode Git's optional C-style quoted path without losing spaces."""
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        try:
            decoded = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            return text
        if isinstance(decoded, str):
            return decoded
    return text


def parse_porcelain_paths(porcelain: str) -> list[str]:
    """Parse tracked ``git status --porcelain`` lines without splitting paths.

    Porcelain reserves the first two columns for status and the third for a
    separator; everything after that separator is a path and may contain
    spaces.  Rename/copy records use ``old -> new`` in the human-readable
    format, so only the new path is relevant to the blocked-file diagnosis.
    Malformed lines are skipped rather than guessed.
    """
    paths: list[str] = []
    for raw_line in str(porcelain or "").splitlines():
        line = raw_line.rstrip("\r")
        if len(line) >= 3 and line[1] == " " and line[0] != " ":
            # Be tolerant of callers that trimmed the leading porcelain pad
            # from the first line; retain the two-column status semantics.
            status = f"{line[0]} "
            payload = line[2:].strip()
        elif len(line) >= 4 and line[2] == " ":
            status = line[:2]
            payload = line[3:].strip()
        else:
            continue
        if status in {"??", "!!"}:
            continue
        if not payload:
            continue
        if "R" in status or "C" in status:
            payload = payload.rsplit(" -> ", 1)[-1].strip()
        candidate = _decode_git_path(payload)
        if candidate and candidate not in paths:
            paths.append(candidate)
    return paths


def diagnose_main_dirty(
    *,
    porcelain: str,
    configured_email: str | None = None,
    task_branch: str | None = None,
    task_id: str | None = None,
) -> dict:
    """Build the exit-5 three-element diagnosis (pure).

    Returns ``{"blocked_files": [...], "blocked_total": int,
    "owner": str, "owner_source": str, "task_branch": str,
    "remediation_cmd": str, "task_id": str}``. ``owner`` is ``unknown``
    unless both a local configured email and the integrating task branch are
    available; the result never invents an author.
    """
    paths = parse_porcelain_paths(porcelain)
    total = len(paths)
    listed = paths[:MAX_LISTED_FILES]
    email = str(configured_email or "").strip()
    branch = str(task_branch or "").strip()
    if email and branch and email != "unknown":
        # This is an explicit, reproducible hint: the main repository's
        # configured identity plus the task branch being integrated.  It is
        # not inferred from an unrelated global Git configuration.
        owner = email
        owner_source = "git_config_user_email"
    else:
        owner = "unknown"
        owner_source = "unavailable"
    remediation = (
        f"herdr-task integrate {task_id}"
        if task_id
        else "herdr-task integrate <task>"
    )
    return {
        "blocked_files": listed,
        "blocked_total": total,
        "owner": owner,
        "owner_source": owner_source,
        "task_branch": branch,
        "remediation_cmd": remediation,
        "task_id": str(task_id or ""),
    }


def launch_precheck_message(
    *, porcelain: str, task_id: str, integration_mode: str
) -> str | None:
    """Warning (never blocking) for ``launch --integration-mode git`` (FR-3.2).

    Returns the warning text when the main repo is dirty, else None.
    Untracked-only dirt is already excluded by ``--untracked-files=no``.
    """
    if str(integration_mode or "") != "git":
        return None
    paths = parse_porcelain_paths(porcelain)
    if not paths:
        return None
    shown = ", ".join(paths[:MAX_LISTED_FILES])
    suffix = f" (+{len(paths) - MAX_LISTED_FILES} more)" if len(paths) > 10 else ""
    return (
        f"[PRECHECK WARNING] task={task_id} main repo has tracked changes "
        f"({shown}{suffix}); launch continues, integrate will gate with exit 5."
    )


def main_repo_from_env(default: str = "") -> str:
    """Resolve the main-repo path for hygiene probes (test seam)."""
    return os.environ.get("HERDR_MAIN_REPO", default)


def is_bypass_untracked(path: str) -> bool:
    """True for untracked bypass artifacts (e.g. .codegraph, .DS_Store, *.sock) produced by indexers/IDEs."""
    from herdr.clone_archive import is_bypass_rel_path
    return is_bypass_rel_path(path)


def teardown_output_diagnosis(porcelain: str, *, task_id: str) -> dict:
    """Reuse hygiene path semantics, including non-infrastructure untracked output."""
    paths = parse_porcelain_paths(porcelain)
    for line in str(porcelain or '').splitlines():
        if line.startswith('?? '):
            path = _decode_git_path(line[3:])
            if (
                path
                and not is_internal_untracked(path)
                and not is_bypass_untracked(path)
                and path not in paths
            ):
                paths.append(path)
    return {'reason': 'unpreserved_worktree_output' if paths else '',
            'blocked_files': paths[:MAX_LISTED_FILES], 'blocked_total': len(paths),
            'remediation_cmd': f'git -C <clone> status; herdr-task commit {task_id}; herdr-task integrate {task_id}'}


def check_source_cleanliness(source_path: str | os.PathLike, *, raise_transient=False) -> tuple[bool, list[str]]:
    """Check if source repo has uncommitted tracked modifications/deletions before worker clone (§2.12).

    Returns (is_clean, dirty_paths). Non-git sources return (True, []).
    """
    import subprocess
    source_p = os.path.expanduser(str(source_path))
    git_dir = os.path.join(source_p, ".git")
    if not os.path.exists(git_dir):
        return True, []
    try:
        proc = subprocess.run(
            ["git", "-C", source_p, "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, check=True, timeout=10
        )
        paths = parse_porcelain_paths(proc.stdout)
        filtered = [p for p in paths if not is_internal_untracked(p)]
        return len(filtered) == 0, filtered
    except Exception as exc:
        import errno
        if raise_transient and (isinstance(exc, subprocess.TimeoutExpired)
                or isinstance(exc, OSError) and exc.errno in (errno.EAGAIN, errno.EINTR, errno.ETIMEDOUT)):
            raise
        return False, [f"cleanliness_check_error:{exc}"]
