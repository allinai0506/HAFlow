"""FR-3 repo-hygiene decision layer (pure, no I/O).

Thin assembly lives in ``bin/herdr-task`` (integrate gate + launch
pre-check). All parsing/attribution decisions live here so the CLI stays
under the file-health budget and stays unit-testable without git.
"""

from __future__ import annotations

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


def parse_porcelain_paths(porcelain: str) -> list[str]:
    """Parse ``git status --porcelain`` lines into paths (tracked only).

    Input is already ``--untracked-files=no``, so every line is a tracked
    entry. Malformed lines are skipped, never guessed.
    """
    paths: list[str] = []
    for line in str(porcelain or "").splitlines():
        if len(line) < 4 or line[:2] in {"??", "!!"}:
            continue
        # Porcelain v1: XY<space>path.  Do not split on whitespace: paths
        # may legally contain spaces.  Rename/copy records use ``old -> new``.
        candidate = line[3:].strip()
        if " -> " in candidate:
            candidate = candidate.split(" -> ", 1)[1].strip()
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
    "owner": str, "owner_source": str, "remediation_cmd": str,
    "task_id": str}``. ``owner`` is ``unknown`` unless a real traceable
    source exists (never guessed from the local git config alone).
    """
    paths = parse_porcelain_paths(porcelain)
    total = len(paths)
    listed = paths[:MAX_LISTED_FILES]
    email = str(configured_email or "").strip()
    if email and task_branch and email not in ("", "unknown"):
        # Config email alone does not prove authorship of the WIP; it is
        # reported as a hint source, but the owner stays unknown unless a
        # stronger trace exists. This keeps the "unknown, don't guess"
        # invariant (AGENTS.md #2, plan-attack FR-3 critique).
        owner = "unknown"
        owner_source = "unavailable"
    elif email:
        owner = "unknown"
        owner_source = "unavailable"
    else:
        owner = "unknown"
        owner_source = "unavailable"
    _ = task_branch
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
    _ = task_id
    shown = ", ".join(paths[:MAX_LISTED_FILES])
    suffix = f" (+{len(paths) - MAX_LISTED_FILES} more)" if len(paths) > 10 else ""
    return (
        "[PRECHECK WARNING] main repo has tracked changes "
        f"({shown}{suffix}); launch continues, integrate will gate with exit 5."
    )


def main_repo_from_env(default: str = "") -> str:
    """Resolve the main-repo path for hygiene probes (test seam)."""
    return os.environ.get("HERDR_MAIN_REPO", default)
