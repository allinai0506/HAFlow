"""FR-3 repo-hygiene decision layer (pure, no I/O).

Thin assembly lives in ``bin/herdr-task`` (integrate gate + launch
pre-check). All parsing/attribution decisions live here so the CLI stays
under the file-health budget and stays unit-testable without git.
"""

from __future__ import annotations

import ast
import os

MAX_LISTED_FILES = 10
INTERNAL_EXACT = frozenset({".agent-task-context", ".herdr-loop", ".herdr"})
INTERNAL_PREFIXES = (".herdr-loop/", ".herdr/")


def is_internal_untracked(path: str) -> bool:
    """True for clone-infra untracked entries that must never trip exit 5."""
    text = str(path or "")
    if text in INTERNAL_EXACT:
        return True
    return text.startswith(INTERNAL_PREFIXES)


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
