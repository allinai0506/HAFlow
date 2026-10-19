"""Stable content fingerprints for Git and shared task workspaces."""

import hashlib
import os
import subprocess
from pathlib import Path


def is_internal_untracked(path, exact, prefixes):
    if path in exact:
        return True

    return path.startswith(prefixes)


def fingerprint_file(repo, relpath):
    path = Path(repo) / relpath

    if not path.exists() and not path.is_symlink():
        return "__MISSING__"

    if path.is_symlink():
        return "symlink:" + os.readlink(path)

    if not path.is_file():
        return "__NON_FILE__"

    h = hashlib.sha256()

    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)

            if not chunk:
                break

            h.update(chunk)

    return h.hexdigest()


def current_workspace_fingerprint(repo, *, is_internal_untracked, fingerprint_file):
    tracked_result = subprocess.run(
        [
            "git",
            "-C",
            repo,
            "diff",
            "--name-only",
            "-z",
            "HEAD",
            "--"
        ],
        text=True,
        capture_output=True
    )

    if tracked_result.returncode != 0:
        raise RuntimeError(
            tracked_result.stderr.strip()
            or tracked_result.stdout.strip()
        )

    tracked_paths = [
        item
        for item in tracked_result.stdout.split("\0")
        if item
    ]

    untracked_result = subprocess.run(
        [
            "git",
            "-C",
            repo,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z"
        ],
        text=True,
        capture_output=True
    )

    if untracked_result.returncode != 0:
        raise RuntimeError(
            untracked_result.stderr.strip()
            or untracked_result.stdout.strip()
        )

    untracked_paths = [
        item
        for item in untracked_result.stdout.split("\0")
        if item and not is_internal_untracked(item)
    ]

    return {
        "tracked": {
            p: fingerprint_file(repo, p)
            for p in tracked_paths
        },
        "untracked": {
            p: fingerprint_file(repo, p)
            for p in untracked_paths
        }
    }


def execution_fields_from_workflow(workflow_record, *, execution_mode):
    """Workflow 记录 → Task 继承的执行契约字段（纯函数）。

    legacy/git 工作流只返回 execution_mode=git；context 工作流附带 binding。
    """
    record = workflow_record or {}
    fields = {"execution_mode": execution_mode(record)}
    if fields["execution_mode"] == "context":
        fields["context"] = dict(record.get("context") or {})
    return fields


def context_workspace_fingerprint(repo, *, is_internal_untracked, fingerprint_file):
    """非 Git Task Workspace 的文件指纹：全部按 untracked 计，忽略内部装配文件。"""
    repo_path = Path(repo)
    untracked = {}

    for path in sorted(repo_path.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(repo_path).as_posix()
        top = rel.split("/", 1)[0]
        if top in (".agent-task-context", ".herdr-loop", ".git"):
            continue
        if is_internal_untracked(rel):
            continue
        untracked[rel] = fingerprint_file(repo, rel)

    return {"tracked": {}, "untracked": untracked}
