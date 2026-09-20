"""Cross-process coordination for Git-backed Herdr tasks."""

from __future__ import annotations

import fcntl
import hashlib
import os
import subprocess
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional


ACTIVE_BRANCH_STATUSES = frozenset(
    {
        "pending",
        "dispatched",
        "working",
        "blocked",
        "agent_done",
        "rework",
        "paused",
        "interrupted",
        "completed",
        "committed",
        "integrated",
        "cleanup_ready",
    }
)


class BranchOwnershipError(RuntimeError):
    """Raised when an active task already owns a requested branch."""


class GitProcessActiveError(RuntimeError):
    """Raised when another Git process still operates on a repository."""


class GitOperationBusyError(RuntimeError):
    """Raised when another coordinator holds the repository operation lock."""


def ensure_branch_available(
    branch: str,
    tasks: Iterable[Mapping[str, object]],
    *,
    task_id: Optional[str] = None,
) -> None:
    """Ensure ``branch`` is owned by no other active task."""
    for task in tasks:
        owner = str(task.get("task_id") or "")
        if owner == task_id or task.get("status") not in ACTIVE_BRANCH_STATUSES:
            continue
        if task.get("branch") == branch:
            raise BranchOwnershipError(
                f"Git branch is already owned by active task {owner}: {branch}"
            )


def _default_process_provider():
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        text=True,
        capture_output=True,
        check=False,
    )
    return [
        tuple(line.strip().split(None, 1))
        for line in result.stdout.splitlines()
        if line.strip() and len(line.strip().split(None, 1)) == 2
    ]


def ensure_no_git_processes(
    repo: os.PathLike[str] | str,
    *,
    process_provider: Optional[Callable[[], Iterable[tuple[str, str]]]] = None,
) -> None:
    """Fail closed when another Git command still references ``repo``."""
    repo_path = Path(repo).expanduser()
    repo_text = str(repo_path.resolve())
    repo_variants = {repo_text, str(repo_path)}
    provider = process_provider or _default_process_provider
    current_pid = str(os.getpid())
    active = []
    for pid, command in provider():
        if str(pid) == current_pid:
            continue
        command_text = str(command)
        if "git" in Path(command_text.split(None, 1)[0]).name and any(
            variant in command_text for variant in repo_variants
        ):
            active.append(f"{pid}: {command_text}")
    if active:
        raise GitProcessActiveError(
            f"Git process still active for {repo_text}: " + "; ".join(active)
        )


class GitOperationLock:
    """A non-blocking, process-wide lock keyed by the real repository path."""

    def __init__(self, repo: os.PathLike[str] | str, *, lock_root: Optional[Path] = None):
        self.repo = Path(repo).expanduser().resolve()
        root = lock_root or Path(
            os.environ.get("HERDR_GIT_LOCK_ROOT", "~/.herdr-controller/locks/git")
        ).expanduser()
        key = hashlib.sha256(str(self.repo).encode("utf-8")).hexdigest()[:20]
        self.path = root / f"{key}.lock"
        self._handle = None

    def try_acquire(self) -> bool:
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        self._handle = handle
        return True

    def acquire(self) -> None:
        if not self.try_acquire():
            raise GitOperationBusyError(f"Git operation already active for {self.repo}")

    def release(self) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
        return False
