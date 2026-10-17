"""Private local recovery snapshots for workflow teardown; never model input.

Keep complete sandbox contents (including Git history) so closing a workflow
cannot destroy its only copy of a document or failed-task diagnostic. Archives
are bounded, verified and published atomically before the source is deleted.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tarfile
import tempfile
import time
from pathlib import Path

from herdr.workflow_docs import workflow_docs_dir

MAX_BYTES = 1024 * 1024 * 1024
MAX_ENTRIES = 100000
MAX_SECONDS = 120


def _digest(handle):
    digest = hashlib.sha256()
    total = 0
    deadline = time.monotonic() + MAX_SECONDS
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        total += len(chunk)
        if total > MAX_BYTES or time.monotonic() > deadline:
            raise ValueError("archive hashing budget exceeded")
        digest.update(chunk)
    return digest.hexdigest()


def _root(task, clone_root):
    raw = Path(task["clone_path"]).expanduser()
    base = Path(clone_root).resolve()
    if raw.is_symlink() or raw.resolve() == base or base not in raw.resolve().parents:
        raise ValueError("unsafe clone path")
    root = raw.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("clone is not a directory")
    tag = root / ".herdr-launch-identity.json"
    if tag.is_symlink():
        raise ValueError("invalid clone identity")
    if tag.exists():
        if tag.is_symlink() or tag.stat().st_size > 8192:
            raise ValueError("invalid clone identity")
        identity = json.loads(tag.read_text())
        if not isinstance(identity, dict):
            raise ValueError("invalid clone identity")
        if identity.get("task_id") != task["task_id"] or (
            task.get("run_id") and identity.get("run_id") != task["run_id"]
        ):
            raise ValueError("clone identity changed")
    return root


def _inventory(root):
    entries = {}
    total = 0
    deadline = time.monotonic() + MAX_SECONDS

    def failed(error):
        raise error

    for current, dirs, files in os.walk(root, followlinks=False, onerror=failed):
        for name in sorted(dirs + files):
            path = Path(current) / name
            info = path.lstat()
            if stat.S_ISSOCK(info.st_mode):
                continue  # Live IPC endpoint, not recoverable file content.
            if len(entries) >= MAX_ENTRIES or time.monotonic() > deadline:
                raise ValueError("archive inventory budget exceeded")
            rel = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode):
                item = {"type": "symlink", "target": os.readlink(path)}
            elif stat.S_ISDIR(info.st_mode):
                item = {"type": "directory"}
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > MAX_BYTES:
                    raise ValueError("archive byte budget exceeded")
                with os.fdopen(
                    os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb"
                ) as handle:
                    item = {
                        "type": "file",
                        "size": info.st_size,
                        "sha256": _digest(handle),
                    }
            else:
                raise ValueError("unsupported sandbox special file")
            entries[rel] = item
    return entries


def archive_directory(task):
    identity = json.dumps(
        [task["workflow_id"], task["task_id"], task.get("run_id")], sort_keys=True
    )
    return (
        workflow_docs_dir(task["workflow_id"])
        / "clone-archives"
        / hashlib.sha256(identity.encode()).hexdigest()
    )


def preserve_clone(task, clone_root):
    root = _root(task, clone_root)
    if task.get("clone_archive"):
        verify_source(task, clone_root, task["clone_archive"])
        return task["clone_archive"]
    info = root.stat()
    entries = _inventory(root)
    manifest = {
        "task_id": task["task_id"],
        "workflow_id": task["workflow_id"],
        "run_id": task.get("run_id"),
        "entries": entries,
    }
    encoded = json.dumps(manifest, sort_keys=True).encode()
    manifest_sha = hashlib.sha256(encoded).hexdigest()
    destination = archive_directory(task)
    anchor = workflow_docs_dir(task["workflow_id"])
    for parent in (destination, destination.parent, anchor):
        if parent.is_symlink():
            raise ValueError("archive directory symlink")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.chmod(0o700)
    path = destination / (manifest_sha + ".tar.gz")
    fd, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=destination)
    try:
        os.close(fd)
        with tarfile.open(temporary, "w:gz", dereference=False) as tar:
            member = tarfile.TarInfo("manifest.json")
            member.size = len(encoded)
            tar.addfile(member, io.BytesIO(encoded))
            for rel in entries:
                tar.inodes.clear()  # Store each regular file independently of hardlinks.
                tar.add(root / rel, arcname="workspace/" + rel, recursive=False)
        # Verify archived bytes against the inventory, not just the gzip CRC.
        with tarfile.open(temporary, "r:gz") as tar:
            for member in tar:
                if member.name == "manifest.json":
                    continue
                rel = member.name.removeprefix("workspace/")
                expected = entries[rel]
                if expected["type"] == "file":
                    if (
                        not member.isfile()
                        or _digest(tar.extractfile(member)) != expected["sha256"]
                    ):
                        raise ValueError("archive content verification failed")
                elif expected["type"] == "symlink" and (
                    not member.issym() or member.linkname != expected["target"]
                ):
                    raise ValueError("archive link verification failed")
        if _inventory(root) != entries or (root.stat().st_dev, root.stat().st_ino) != (
            info.st_dev,
            info.st_ino,
        ):
            raise ValueError("clone changed during archive")
        with open(temporary, "rb") as handle:
            os.fsync(handle.fileno())
            archive_sha = _digest(handle)
        os.replace(temporary, path)
        directory_fd = os.open(destination, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return {
            "path": str(path),
            "sha256": archive_sha,
            "manifest_sha256": manifest_sha,
            "device": info.st_dev,
            "inode": info.st_ino,
        }
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def archive_manifest(task):
    archive = task["clone_archive"]
    if not re.fullmatch(r"[0-9a-f]{64}", str(archive.get("manifest_sha256", ""))):
        raise ValueError("invalid archive digest")
    path = Path(archive["path"])
    expected = archive_directory(task) / (archive["manifest_sha256"] + ".tar.gz")
    if (
        path.is_symlink()
        or path.resolve() != expected.resolve()
        or any(
            parent.is_symlink()
            for parent in (
                path.parent,
                path.parent.parent,
                workflow_docs_dir(task["workflow_id"]),
            )
        )
    ):
        raise ValueError("archive ownership mismatch")
    with tarfile.open(path, "r:gz") as tar:
        for count, member in enumerate(tar):
            if count > MAX_ENTRIES:
                raise ValueError("archive entry budget exceeded")
            if (
                member.name == "manifest.json"
                and member.isfile()
                and member.size <= 32 * 1024 * 1024
            ):
                blob = tar.extractfile(member).read()
                if hashlib.sha256(blob).hexdigest() != archive["manifest_sha256"]:
                    raise ValueError("archive manifest changed")
                data = json.loads(blob)
                if [data.get(k) for k in ("task_id", "workflow_id", "run_id")] != [
                    task.get(k) for k in ("task_id", "workflow_id", "run_id")
                ]:
                    raise ValueError("archive task identity mismatch")
                return data
    raise ValueError("archive manifest missing")


def verify_source(task, clone_root, archive):
    root = _root(task, clone_root)
    info = root.stat()
    if (info.st_dev, info.st_ino) != (archive["device"], archive["inode"]):
        raise ValueError("clone instance changed")
    manifest = archive_manifest(dict(task, clone_archive=archive))
    current = _inventory(root)
    deleting = task.get("clone_reclaim_started") == archive["manifest_sha256"]
    if current != manifest["entries"] and not (
        deleting
        and all(manifest["entries"].get(rel) == item for rel, item in current.items())
    ):
        raise ValueError("clone output changed after archive")
    with open(archive["path"], "rb") as handle:
        if _digest(handle) != archive["sha256"]:
            raise ValueError("recovery archive changed")


def read_archived_file(task, rel, limit):
    entries = archive_manifest(task)["entries"]
    item = entries.get(rel)
    if not item or item["type"] != "file" or item["size"] > limit:
        raise ValueError("archive file unavailable or too large")
    with tarfile.open(task["clone_archive"]["path"], "r:gz") as tar:
        for count, member in enumerate(tar):
            if count > MAX_ENTRIES:
                break
            if (
                member.name == "workspace/" + rel
                and member.isfile()
                and member.size == item["size"]
            ):
                blob = tar.extractfile(member).read(limit + 1)
                if hashlib.sha256(blob).hexdigest() == item["sha256"]:
                    return blob
                break
    raise ValueError("archive file verification failed")


def plan_clone(task, clone_root):
    """Validate ownership and archive budgets without publishing files or metadata."""
    root = _root(task, clone_root)
    if task.get("clone_archive"):
        verify_source(task, clone_root, task["clone_archive"])
    else:
        _inventory(root)
    return {
        "archive_required": True,
        "archive_would_create": not bool(task.get("clone_archive")),
        "clone_would_delete": True,
        "deletion_condition": "archive verified and source identity/content unchanged",
    }
