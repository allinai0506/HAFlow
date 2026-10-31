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

# Explicit bypass directories and files produced by external indexers, IDEs,
# and live IPC daemons (e.g. CodeGraph MCP daemon, Unix sockets).
# These artifacts are out-of-band diagnostics, not project deliverables or workspace
# source code. Capturing or verifying them violates the boundary rule that
# "side-channel diagnostics must not alter execution status" (AGENTS.md §4.10).
BYPASS_IGNORED_DIRS = frozenset({
    ".codegraph",  # CodeGraph MCP / local index daemon runtime artifacts (db, pid, logs)
    "node_modules",  # JavaScript / TypeScript package dependencies
    "dist",  # Frontend / TypeScript build outputs
    "dist-release",  # Release build outputs
    "build",  # General build output
    "target",  # Java / Rust / Maven build outputs
    ".m2",  # Local Maven artifact cache
    ".gradle",  # Gradle build cache
    ".venv",  # Python virtual environments
    "venv",  # Python virtual environments
    "__pycache__",  # Python bytecode cache
    ".pytest_cache",  # Pytest cache
    ".mypy_cache",  # Mypy type check cache
    ".ruff_cache",  # Ruff linter cache
    ".turbo",  # Turborepo build cache
    ".next",  # Next.js build output
    ".nuxt",  # Nuxt.js build output
    ".cache",  # Generic compiler/indexer cache
})

BYPASS_IGNORED_FILE_SUFFIXES = (
    ".sock",  # Live IPC sockets or socket placeholders
)

BYPASS_IGNORED_FILE_NAMES = frozenset({
    ".DS_Store",  # macOS Finder directory metadata
})


def is_bypass_rel_path(rel: str | Path) -> bool:
    """Check if a relative path belongs to known external indexers or bypass artifacts."""
    parts = Path(rel).parts
    for part in parts[:-1]:
        if part in BYPASS_IGNORED_DIRS:
            return True
    if parts:
        name = parts[-1]
        if name in BYPASS_IGNORED_DIRS or name in BYPASS_IGNORED_FILE_NAMES:
            return True
        if any(name.endswith(suffix) for suffix in BYPASS_IGNORED_FILE_SUFFIXES):
            return True
    return False


def is_residual_clone_shell(root: Path) -> bool:
    """Check if a clone directory contains only external bypass artifacts (no actual workspace)."""
    if not root.is_dir() or root.is_symlink():
        return False
    if (root / ".git").exists() or (root / ".herdr-launch-identity.json").exists():
        return False
    deadline = time.monotonic() + 5
    count = 0
    try:
        for current, dirs, files in os.walk(root, followlinks=False):
            candidates = sorted(dirs + files)
            dirs[:] = [d for d in dirs if d not in BYPASS_IGNORED_DIRS]
            for name in candidates:
                count += 1
                if count > MAX_ENTRIES or time.monotonic() > deadline:
                    return False
                p = Path(current) / name
                rel = p.relative_to(root).as_posix()
                if not is_bypass_rel_path(rel):
                    return False
    except OSError:
        return False
    return True


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


def _declared_deliverable_rel_paths(task: dict | None, root: Path | str | None = None) -> set[str]:
    """Collect relative paths of all declared artifacts/deliverables on a task."""
    if not isinstance(task, dict):
        return set()
    from herdr.deliverables import _ref_rel

    workspaces: list[Path] = []
    if root is not None:
        try:
            workspaces.append(Path(root))
        except Exception:
            pass
    if task.get("workspace"):
        try:
            ws_path = Path(task["workspace"])
            if ws_path not in workspaces:
                workspaces.append(ws_path)
        except Exception:
            pass

    declared = set()
    for key in ("artifacts", "artifact_refs", "changed_artifacts", "deliverables"):
        for item in task.get(key) or []:
            matched = False
            for ws in workspaces:
                rel = _ref_rel(item, ws)
                if rel:
                    declared.add(rel)
                    matched = True
                    break
            if matched:
                continue

            candidates: list[str] = []
            if isinstance(item, str):
                candidates.append(item)
            elif isinstance(item, dict):
                for f in ("rel_path", "path", "ref", "artifact_path", "content_ref"):
                    val = item.get(f)
                    if val:
                        candidates.append(str(val))
                meta = item.get("metadata")
                if isinstance(meta, dict):
                    for f in ("artifact_path", "path", "rel_path"):
                        val = meta.get(f)
                        if val:
                            candidates.append(str(val))
                if not candidates and item.get("name"):
                    candidates.append(str(item.get("name")))

            for raw in candidates:
                text = str(raw or "").strip().replace("\\", "/")
                if text.startswith("./"):
                    text = text[2:]
                if not text or any(ch.isspace() for ch in text) or ".." in Path(text).parts:
                    continue
                if not text.startswith("/"):
                    declared.add(text)
                    break
                raw_path = Path(text).expanduser()
                for ws in workspaces:
                    try:
                        resolved = raw_path.resolve()
                        ws_res = ws.resolve()
                        if ws_res in resolved.parents and resolved != ws_res:
                            rel_p = resolved.relative_to(ws_res).as_posix()
                            if rel_p and ".." not in Path(rel_p).parts:
                                declared.add(rel_p)
                                break
                    except (OSError, ValueError):
                        continue
    return declared


def _is_declared_or_ancestor(rel: str, declared: set[str]) -> bool:
    """Check if rel is an explicitly declared path or an ancestor directory of one."""
    if not declared:
        return False
    norm = rel.rstrip("/") + "/"
    return rel in declared or any(p.startswith(norm) for p in declared)


def _inventory(root, task=None):
    entries = {}
    total = 0
    deadline = time.monotonic() + MAX_SECONDS
    declared = _declared_deliverable_rel_paths(task, root)

    def failed(error):
        raise error

    for current, dirs, files in os.walk(root, followlinks=False, onerror=failed):
        rel_current = Path(current).relative_to(root).as_posix()
        dirs[:] = [
            d for d in dirs
            if d not in BYPASS_IGNORED_DIRS
            or _is_declared_or_ancestor(d if rel_current == "." else f"{rel_current}/{d}", declared)
        ]
        for name in sorted(dirs + files):
            path = Path(current) / name
            info = path.lstat()
            rel = path.relative_to(root).as_posix()
            is_declared = _is_declared_or_ancestor(rel, declared)
            if not is_declared and (
                stat.S_ISSOCK(info.st_mode)
                or name.endswith(".sock")
                or name in BYPASS_IGNORED_FILE_NAMES
                or name in BYPASS_IGNORED_DIRS
            ):
                continue  # Live IPC endpoint, external indexer, or transient metadata.
            if not is_declared and is_bypass_rel_path(rel):
                continue
            if len(entries) >= MAX_ENTRIES or time.monotonic() > deadline:
                raise ValueError("archive inventory budget exceeded")
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
    entries = _inventory(root, task=task)
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
        if _inventory(root, task=task) != entries or (root.stat().st_dev, root.stat().st_ino) != (
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
        if not is_residual_clone_shell(root):
            raise ValueError("clone instance changed")
    manifest = archive_manifest(dict(task, clone_archive=archive))
    current = _inventory(root, task=task)
    declared = _declared_deliverable_rel_paths(task, root)
    manifest_entries = {
        k: v for k, v in manifest["entries"].items() if _is_declared_or_ancestor(k, declared) or not is_bypass_rel_path(k)
    }
    deleting = task.get("clone_reclaim_started") == archive["manifest_sha256"]
    if current != manifest_entries and not (
        is_residual_clone_shell(root)
        or (
            deleting
            and all(manifest_entries.get(rel) == item for rel, item in current.items())
        )
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


def delete_residual_clone_shell(root: str | Path, *, clone_root: str | Path | None = None) -> bool:
    """Safely reclaim a residual clone shell with atomic claim and boundary revalidation.

    Prevents race conditions where a concurrent process or external indexer
    creates a non-bypass regular file after the initial shell check. The directory is
    atomically claimed via rename, revalidated to ensure no non-bypass artifacts
    were introduced, and only deleted if it remains a pure residual shell.
    If revalidation fails, the directory is restored to its original name and preserved.
    """
    raw_path = Path(os.path.expanduser(str(root)))
    if raw_path.is_symlink() or not raw_path.exists():
        return False
    root_path = Path(os.path.realpath(str(raw_path)))
    if not root_path.exists():
        return False
    if clone_root:
        real_clone_root = os.path.realpath(os.path.expanduser(str(clone_root)))
        try:
            common = os.path.commonpath([real_clone_root, str(root_path)])
        except ValueError:
            return False
        if common != real_clone_root or str(root_path) == real_clone_root:
            return False

    if not is_residual_clone_shell(root_path):
        return False

    staged = root_path.parent / f"{root_path.name}.reclaim-{os.getpid()}-{time.time_ns()}"
    try:
        root_path.rename(staged)
    except OSError:
        return False

    if not is_residual_clone_shell(staged):
        try:
            staged.rename(root_path)
        except OSError:
            pass
        return False

    import shutil
    shutil.rmtree(staged, ignore_errors=True)
    return not staged.exists()
