"""User-facing deliverables for one task workspace.

A deliverable belongs to one task. Node and workflow views roll up the same
records. A file is `present` only when a contract, a recorded output, or this
task's Git changes identify it. Other workspace files stay `workspace`.
Nothing here is treated as verified.
"""

from __future__ import annotations

import hashlib
import html
import os
import subprocess
import tarfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

PREVIEW_LIMIT = 64 * 1024
DOWNLOAD_LIMIT = 8 * 1024 * 1024
MAX_FILES = 40
_PROBE_WORKERS = 4
_PROBES = ThreadPoolExecutor(max_workers=_PROBE_WORKERS, thread_name_prefix="deliverable")

SKIP_DIRS = {
    ".git", ".herdr", ".herdr-loop", ".hg", ".svn",
    "node_modules", "__pycache__", ".venv", "venv", ".tox",
}
SENSITIVE_NAMES = {
    ".env", ".env.local", ".netrc", "id_rsa", "id_ed25519",
    "credentials.json", ".agent-task-context", ".herdr-launch-identity.json",
}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".kdbx"}
TEXT_SUFFIXES = {".md", ".txt", ".csv", ".json", ".log"}
FILE_SUFFIXES = TEXT_SUFFIXES | {
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
}
STATUS_LABELS = {
    "present": "已产生",
    "historical": "历史产物（任务已替代）",
    "missing": "未产生",
    "unavailable": "不可读取",
    "verified": "已验证",
    "workspace": "工作区文件",
    "unconfirmed": "不可确认",
}
TRUNCATED_NOTICE = "成果列表可能未完整。"
_RECORDED_KEYS = ("artifacts", "artifact_refs", "changed_artifacts", "deliverables", "observations")


class DeliverableError(Exception):
    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message


def pack(items: List[Dict[str, Any]], error: str = "", truncated: bool = False) -> Dict[str, Any]:
    counts = {"present": 0, "missing": 0, "unavailable": 0, "verified": 0, "workspace": 0, "unconfirmed": 0, "historical": 0}
    for item in items:
        status = item.get("status")
        if status in counts:
            counts[status] += 1
    packed: Dict[str, Any] = {"items": items, "counts": counts}
    if error:
        packed["error"] = error
    if truncated:
        packed["truncated"] = True
        packed["notice"] = TRUNCATED_NOTICE
    return packed


def project_for_tasks(
    workflow_id: str,
    tasks: Iterable[Dict[str, Any]],
    nodes: Optional[Iterable[Dict[str, Any]]] = None,
    *,
    include_expectations: bool = True,
) -> Dict[str, Any]:
    node_list = [node for node in (nodes or []) if isinstance(node, dict)]
    task_list = [
        task for task in (tasks or [])
        if isinstance(task, dict) and task.get("workflow_id") == workflow_id
    ]
    items: List[Dict[str, Any]] = []
    error = ""
    truncated = False
    remaining = iter(enumerate(task_list))
    pending = {}
    results = {}
    while True:
        while len(pending) < _PROBE_WORKERS:
            entry = next(remaining, None)
            if entry is None:
                break
            index, task = entry
            pending[_PROBES.submit(_task_items, task, node_list)] = index
        if not pending:
            break
        completed, _ = wait(pending, return_when=FIRST_COMPLETED)
        for future in completed:
            results[pending.pop(future)] = future.result()
    for index in range(len(task_list)):
        rows, task_error, task_truncated = results[index]
        items.extend(rows)
        if task_error and not error:
            error = task_error
        truncated = truncated or task_truncated
    if include_expectations:
        items.extend(_node_gaps(workflow_id, task_list, node_list, items))
    return pack(_dedupe(items), error, truncated)


def read_deliverable(
    workflow_id: str,
    tasks: Iterable[Dict[str, Any]],
    nodes: Optional[Iterable[Dict[str, Any]]],
    task_id: str,
    deliverable_id: str,
    *,
    download: bool = False,
) -> Dict[str, Any]:
    owned = [
        task for task in (tasks or [])
        if isinstance(task, dict)
        and task.get("workflow_id") == workflow_id
        and task.get("task_id") == task_id
    ]
    packed = project_for_tasks(workflow_id, owned, nodes)
    item = next((row for row in packed["items"] if row.get("id") == deliverable_id), None)
    if item is None:
        raise DeliverableError("not_found", 404, "找不到这项成果")
    if not item.get("readable") or item.get("status") not in {"present", "workspace", "historical"}:
        raise DeliverableError("unavailable", 409, item.get("reason") or "成果当前不可读取")
    task = owned[0]
    workspace = _workspace(task)
    archived_blob = None
    if task.get("clone_archive") and (workspace is None or not workspace.exists()):
        from herdr.clone_archive import read_archived_file
        try:
            archived_blob = read_archived_file(task, str(item.get("rel_path") or ""), DOWNLOAD_LIMIT)
        except (OSError, ValueError, tarfile.TarError) as exc:
            raise DeliverableError("unavailable", 409, "归档成果校验失败") from exc
        size = len(archived_blob)
    else:
        if workspace is None:
            raise DeliverableError("forbidden", 403, "不能读取这项成果")
        path = resolve_workspace_file(workspace, str(item.get("rel_path") or ""))
        if path.suffix.lower() not in FILE_SUFFIXES:
            raise DeliverableError("forbidden", 403, "不能读取这项成果")
        size = path.stat().st_size
    if size > DOWNLOAD_LIMIT:
        raise DeliverableError("too_large", 413, "文件超过读取上限")
    if download:
        return {
            "mode": "download",
            "filename": item["name"],
            "body": archived_blob if archived_blob is not None else path.read_bytes(),
            "headers": download_headers(item["name"]),
        }
    if archived_blob is not None:
        blob = archived_blob[:PREVIEW_LIMIT]
    else:
        with path.open("rb") as handle:
            blob = handle.read(PREVIEW_LIMIT)
    text = _preview_text(blob)
    previewable = "view" in (item.get("actions") or [])
    return {
        "mode": "preview",
        "name": item["name"],
        "text": text if previewable else "",
        "html": safe_markdown_html(text) if previewable else "",
        "truncated": size > PREVIEW_LIMIT,
        "total_bytes": size,
        "media_type": _media_type(item["name"]),
        "reason": "" if previewable else "这个文件不能在页面里预览，请下载。",
    }


def resolve_workspace_file(workspace: Path, rel: str) -> Path:
    root = _managed_dir(workspace)
    if root is None:
        raise DeliverableError("forbidden", 403, "不能读取这项成果")
    text = str(rel or "").strip().replace("\\", "/")
    if text.startswith("./"):
        text = text[2:]
    if not text or "\x00" in text or text.startswith(("/", "\\")) or ".." in Path(text).parts:
        raise DeliverableError("forbidden", 403, "不能读取这项成果")
    if _sensitive(text):
        raise DeliverableError("forbidden", 403, "不能读取这项成果")
    try:
        resolved = (root / text).resolve(strict=True)
    except OSError as exc:
        raise DeliverableError("not_found", 404, "找不到这项成果") from exc
    if resolved != root and root not in resolved.parents:
        raise DeliverableError("forbidden", 403, "不能读取这项成果")
    if not resolved.is_file():
        raise DeliverableError("not_found", 404, "找不到这项成果")
    relative = resolved.relative_to(root).as_posix()
    if _sensitive(relative) or any(part in SKIP_DIRS or part.startswith(".herdr") for part in Path(relative).parts[:-1]):
        raise DeliverableError("forbidden", 403, "不能读取这项成果")
    return resolved


def _preview_text(blob: bytes) -> str:
    """Decode a capped byte slice without growing past PREVIEW_LIMIT when re-encoded."""
    text = blob.decode("utf-8", errors="ignore")
    encoded = text.encode("utf-8")
    if len(encoded) <= PREVIEW_LIMIT:
        return text
    return encoded[:PREVIEW_LIMIT].decode("utf-8", errors="ignore")


def download_headers(filename: str) -> Dict[str, str]:
    from urllib.parse import quote

    return {
        "Content-Type": _media_type(filename),
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cache-Control": "no-store",
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
    }


def safe_markdown_html(text: str) -> str:
    escaped = html.escape(text or "", quote=True)
    parts: List[str] = []
    paragraph: List[str] = []
    in_code = False

    def flush() -> None:
        if paragraph:
            parts.append("<p>" + "<br>\n".join(paragraph) + "</p>")
            paragraph.clear()

    for line in escaped.split("\n"):
        if line.startswith("```"):
            if in_code:
                parts.append("</code></pre>")
                in_code = False
            else:
                flush()
                parts.append("<pre><code>")
                in_code = True
            continue
        if in_code:
            parts.append(line + "\n")
            continue
        if line.startswith("### "):
            flush()
            parts.append(f"<h3>{line[4:]}</h3>")
            continue
        if line.startswith("## "):
            flush()
            parts.append(f"<h2>{line[3:]}</h2>")
            continue
        if line.startswith("# "):
            flush()
            parts.append(f"<h1>{line[2:]}</h1>")
            continue
        if line.startswith("- "):
            flush()
            parts.append(f"<ul><li>{line[2:]}</li></ul>")
            continue
        if not line.strip():
            flush()
            continue
        paragraph.append(line)
    if in_code:
        parts.append("</code></pre>")
    flush()
    return "\n".join(parts)


def _task_items(task: Dict[str, Any], nodes: List[Dict[str, Any]]):
    node_id, node_label = _node_of(task, nodes)
    expected = _file_tokens(_expected_for(node_id, nodes))
    retired = _retired_task(task)
    workspace = _workspace(task)
    if task.get("clone_archive") and (workspace is None or not workspace.exists()):
        from herdr.clone_archive import archive_manifest
        try:
            entries = archive_manifest(task)["entries"]
            items = []
            for row in task["clone_archive"].get("deliverables", []):
                rel = row.get("rel_path")
                if not rel:
                    items.append(row)
                    continue
                parts = Path(rel).parts
                if (_sensitive(rel) or any(part in SKIP_DIRS or part.startswith(".herdr") for part in parts[:-1])
                        or Path(rel).suffix.lower() not in FILE_SUFFIXES or entries.get(rel,{}).get("type")!="file"):
                    continue
                items.append(_file_item(task,node_id,node_label,rel,retired,
                                        row.get("status")=="present" or any(_same_output(token,rel) for token in expected)))
            return items, "", False
        except (OSError, ValueError, tarfile.TarError):
            return [], "任务成果归档不可读取或校验失败。", False
    error = ""
    changed: List[str] = []
    head = None
    if task.get("clone_path") and workspace is None:
        error = "任务工作区不在受管目录内，不能读取。"
    elif workspace is not None and (workspace / ".git").exists():
        changed, error = _changed_files(workspace)
        head = _head_sha(workspace)
    present, walk_error, truncated, git_rels = _present_rels(task, workspace, expected, changed, head=head)
    if walk_error and not error:
        error = walk_error
    registered = _registered_rels(task, workspace)
    items = [
        _file_item(
            task, node_id, node_label, rel, retired,
            _is_formal(rel, expected, registered, git_rels),
        )
        for rel in present
    ]
    items.extend(_git_items(task, node_id, node_label, retired, changed, workspace, head=head))
    return items, error, truncated


def _file_tokens(raw_items: Iterable[Any]) -> List[str]:
    tokens: List[str] = []
    for raw in raw_items or []:
        token = _file_output_token(str(raw))
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def _is_formal(rel: str, expected: Iterable[str], registered: Iterable[str], git_rels: Iterable[str]) -> bool:
    if rel in set(git_rels):
        return True
    return any(_same_output(token, rel) for token in list(expected) + list(registered))


def _file_item(task, node_id, node_label, rel, retired, formal) -> Dict[str, Any]:
    name = Path(rel).name
    kind, type_label = _classify(name)
    actions = ["view", "download"] if Path(name).suffix.lower() in TEXT_SUFFIXES else ["download"]
    if retired:
        status, reason = "historical", "所属任务已替代，仅供历史查阅，不计入当前交付。"
    elif formal:
        status, reason = "present", ""
    else:
        status = "workspace"
        reason = "这个文件在任务工作区里，但没有登记为本次任务的交付成果。"
    return _base(
        task, node_id, node_label,
        kind=kind, key=rel, name=name, type_label=type_label,
        status=status, reason=reason, actions=actions,
        readable=True, rel_path=rel,
    )


def _node_gaps(workflow_id, tasks, nodes, items) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for task in tasks:
        node_id, _label = _node_of(task, nodes)
        grouped.setdefault(node_id, []).append(task)
    gaps = []
    for node in nodes:
        node_id = str(node.get("id") or "")
        node_tasks = grouped.get(node_id) or []
        if not node_tasks:
            continue
        label = str(node.get("label") or node_id)
        for token in _file_tokens(node.get("required_outputs") or []):
            if _expectation_satisfied(items, node_tasks, node_id, token):
                continue
            gaps.append(_gap_item(workflow_id, node_id, label, token, node_tasks))
    return gaps


def _retired_task(task: Dict[str, Any]) -> bool:
    return task.get("status") == "superseded" or bool(task.get("superseded_by"))


def _expectation_satisfied(items, tasks, node_id, token) -> bool:
    """A current readable deliverable satisfies the node. Retired files do not."""
    active = {str(task.get("task_id") or "") for task in tasks if not _retired_task(task)}
    for item in items:
        rel = str(item.get("rel_path") or "")
        if str(item.get("node_id")) != str(node_id) or not rel:
            continue
        if str(item.get("task_id") or "") not in active:
            continue
        if item.get("status") == "present" and _same_output(token, rel):
            return True
    for task in tasks:
        if _retired_task(task):
            continue
        workspace = _workspace(task)
        if workspace is not None and _lookup(workspace, token) == "present":
            return True
    return False


def _gap_item(workflow_id, node_id, node_label, token, tasks) -> Dict[str, Any]:
    holder = {"task_id": "", "workflow_id": workflow_id, "agent": ""}
    _kind, type_label = _classify(token)
    active = [task for task in tasks if not _retired_task(task)]
    blocked = False
    absent = False
    for task in active:
        workspace = _workspace(task)
        if workspace is None:
            blocked = True
            continue
        state = _lookup(workspace, token)
        if state == "blocked":
            blocked = True
        elif state == "absent":
            absent = True
    if not active:
        status, reason = "unconfirmed", "已替代任务的文件不能当作当前交付，当前无法确认成果是否存在。"
    elif absent and not blocked:
        status, reason = "missing", "尚未生成"
    elif blocked and not absent:
        status, reason = "unavailable", "任务工作区不在受管目录内，不能读取。"
    elif blocked:
        status, reason = "unconfirmed", "有任务工作区不能读取，不能把这项标成尚未生成。"
    else:
        status, reason = "unconfirmed", "当前无法确认这项成果是否已经产生。"
    return _base(
        holder, node_id, node_label,
        kind="expected", key=f"{node_id}\n{token}", name=Path(token).name or token,
        type_label=type_label, status=status, reason=reason,
        actions=[], readable=False, rel_path="",
    )


def _git_items(task, node_id, node_label, retired, changed, workspace=None, *, head=None) -> List[Dict[str, Any]]:
    items = []
    sha = _commit_sha(task, workspace, head=head)
    if sha:
        items.append(_base(
            task, node_id, node_label, kind="git_commit", key=sha, name=f"提交 {sha[:12]}",
            type_label="代码交付", status="historical" if retired else "present",
            reason="这是历史提交记录，不能当作文件下载。" if retired else "这是提交记录，不能当作文件下载。",
            actions=[], readable=False,
            rel_path="", sha=sha, branch=str(task.get("branch") or task.get("integration_branch") or ""),
        ))
    url = _pr_url(task)
    if url:
        items.append(_base(
            task, node_id, node_label, kind="git_pr", key=url, name="Pull Request",
            type_label="代码交付", status="historical" if retired else "present",
            reason="所属任务已替代。" if retired else "",
            actions=["open"], readable=False, rel_path="", url=url,
        ))
    if changed and not retired:
        shown = changed[:15]
        items.append(_base(
            task, node_id, node_label, kind="git_diff", key="diff", name="文件变更",
            type_label="代码交付", status="present",
            reason="这是变更清单，不能当作单个文件下载。", actions=[],
            readable=False, rel_path="", files=shown, files_changed=len(changed),
        ))
    return items


def _base(task, node_id, node_label, **fields) -> Dict[str, Any]:
    kind = fields["kind"]
    key = fields.pop("key")
    status = fields["status"]
    item = {
        "id": hashlib.sha256(f"{task.get('task_id')}\n{kind}\n{key}".encode()).hexdigest()[:20],
        "task_id": task.get("task_id") or "",
        "workflow_id": task.get("workflow_id") or "",
        "node_id": node_id,
        "node_label": node_label,
        "agent": task.get("agent") or "",
        "status_label": STATUS_LABELS[status],
    }
    item.update(fields)
    return item


def _present_rels(task, workspace, expected, changed, *, head=None):
    found: List[str] = []
    seen = set()
    git_rels = set()
    truncated = False
    walk_error = ""
    if workspace is None:
        return found, walk_error, truncated, git_rels

    def add(rel: str) -> str:
        nonlocal truncated
        rel = str(rel or "").strip().replace("\\", "/")
        if rel.startswith("./"):
            rel = rel[2:]
        if not rel or Path(rel).suffix.lower() not in FILE_SUFFIXES or _sensitive(rel):
            return ""
        if any(part in SKIP_DIRS or part.startswith(".herdr") for part in Path(rel).parts[:-1]):
            return ""
        try:
            resolved = resolve_workspace_file(workspace, rel)
            stored = resolved.relative_to(workspace.resolve()).as_posix()
        except (DeliverableError, ValueError):
            return ""
        if stored in seen:
            return stored
        if len(found) >= MAX_FILES:
            truncated = True
            return ""
        seen.add(stored)
        found.append(stored)
        return stored

    for token in expected:
        add(token)
    for rel in _registered_rels(task, workspace):
        add(rel)
    if (workspace / ".git").exists():
        for rel in changed or []:
            stored = add(rel)
            if stored:
                git_rels.add(stored)
        committed, commit_error = _owned_commit_files(task, workspace, head=head)
        for rel in committed:
            stored = add(rel)
            if stored:
                git_rels.add(stored)
        return found, commit_error or walk_error, truncated, git_rels
    paths, walk_error, walk_truncated = _walk_files(workspace)
    truncated = truncated or walk_truncated
    for candidate in paths:
        add(_catalog_rel(workspace, candidate))
    return found, walk_error, truncated, git_rels


def _walk_files(workspace: Path):
    found = []
    failed = False
    truncated = False

    def onerror(_exc) -> None:
        nonlocal failed
        failed = True

    root = workspace.resolve()
    for current, dirnames, filenames in os.walk(root, followlinks=False, onerror=onerror):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS and not name.startswith(".herdr")]
        for name in filenames:
            if _sensitive(name) or Path(name).suffix.lower() not in FILE_SUFFIXES:
                continue
            if len(found) >= MAX_FILES:
                truncated = True
                break
            found.append(Path(current) / name)
        if truncated:
            break
    error = "无法读取任务工作区，未列出的文件不代表尚未生成。" if failed else ""
    return found, error, truncated


def _registered_rels(task: Dict[str, Any], workspace: Optional[Path]) -> List[str]:
    if workspace is None:
        return []
    found: List[str] = []
    for key in _RECORDED_KEYS:
        raw = task.get(key)
        items = raw if isinstance(raw, list) else []
        for item in items:
            rel = _ref_rel(item, workspace)
            if rel and rel not in found and _lookup(workspace, rel) == "present":
                found.append(rel)
    return found


def _ref_rel(item: Any, workspace: Path) -> str:
    candidates: List[str] = []
    if isinstance(item, str):
        candidates.append(item)
    elif isinstance(item, dict):
        for field in ("rel_path", "path", "ref", "artifact_path", "content_ref"):
            if item.get(field):
                candidates.append(str(item.get(field)))
        meta = item.get("metadata")
        if isinstance(meta, dict):
            for field in ("artifact_path", "path", "rel_path"):
                if meta.get(field):
                    candidates.append(str(meta.get(field)))
        if not candidates and item.get("name"):
            candidates.append(str(item.get("name")))
    for raw in candidates:
        token = _file_output_token(raw)
        if token:
            return token
        try:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                continue
            resolved = path.resolve()
            root = workspace.resolve()
            if resolved == root or root not in resolved.parents:
                continue
            token = _file_output_token(resolved.relative_to(root).as_posix())
            if token:
                return token
        except (OSError, ValueError):
            continue
    return ""


def _catalog_rel(workspace: Path, candidate: Path) -> str:
    try:
        rel = candidate.resolve(strict=True).relative_to(workspace.resolve()).as_posix()
    except (OSError, ValueError):
        return ""
    if _sensitive(rel) or any(part in SKIP_DIRS for part in Path(rel).parts[:-1]):
        return ""
    return rel


def _changed_files(workspace: Path):
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotepath=false", "status", "--porcelain", "--untracked-files=all"],
            cwd=str(workspace), text=True, capture_output=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [], "无法列出 Git 变更，未列出的文件不代表尚未生成。"
    if result.returncode != 0:
        return [], "无法列出 Git 变更，未列出的文件不代表尚未生成。"
    files = []
    for line in (result.stdout or "").splitlines():
        if len(line) < 4:
            continue
        path = _git_path(line[3:])
        if path and not _sensitive(path) and path not in files:
            files.append(path)
    return files, ""


def _commit_files(workspace: Path, sha: str) -> List[str]:
    if not sha:
        return []
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotepath=false", "diff-tree", "--no-commit-id", "--name-only", "-r", sha],
            cwd=str(workspace), text=True, capture_output=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    files = []
    for line in (result.stdout or "").splitlines():
        path = _git_path(line)
        if path and path not in files:
            files.append(path)
    return files


def _git_path(field: str) -> str:
    text = str(field or "").strip()
    if " -> " in text:
        text = text.split(" -> ")[-1].strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        try:
            text = bytes(text[1:-1], "utf-8").decode("unicode_escape").encode("latin1").decode("utf-8")
        except (UnicodeError, ValueError):
            text = text[1:-1]
    return text.replace("\\", "/").strip()


def _file_output_token(token: str) -> str:
    """Return a relative file name, or empty when the node label is prose."""
    text = str(token or "").strip().replace("\\", "/")
    if text.startswith("./"):
        text = text[2:]
    if not text or text.startswith("/") or any(ch.isspace() for ch in text):
        return ""
    if ".." in Path(text).parts or Path(text).suffix.lower() not in FILE_SUFFIXES:
        return ""
    return text


def _lookup(workspace: Path, rel: str) -> str:
    try:
        resolve_workspace_file(workspace, rel)
    except DeliverableError as exc:
        if exc.status == 404:
            return "absent"
        return "blocked"
    return "present"


def _workspace(task: Dict[str, Any]) -> Optional[Path]:
    raw = task.get("clone_path")
    if not raw:
        return None
    return _managed_dir(Path(str(raw)))


def _managed_dir(workspace: Path) -> Optional[Path]:
    try:
        root = Path(os.environ.get("HERDR_CLONES_DIR") or (Path.home() / ".herdr-controller" / "clones")).expanduser().resolve()
        resolved = workspace.expanduser().resolve()
    except OSError:
        return None
    if resolved == root or root not in resolved.parents:
        return None
    return resolved


def _node_of(task, nodes):
    node_id = str(task.get("node") or task.get("stage") or "")
    for node in nodes:
        if str(node.get("id") or "") == node_id:
            return node_id, str(node.get("label") or node_id)
    return node_id, str(task.get("node_label") or task.get("stage_label") or node_id)


def _expected_for(node_id: str, nodes: List[Dict[str, Any]]) -> List[str]:
    for node in nodes:
        if str(node.get("id") or "") != node_id:
            continue
        return [str(item).strip() for item in (node.get("required_outputs") or []) if str(item).strip()]
    return []


def _same_output(expected: str, rel: str) -> bool:
    wanted = expected.replace("\\", "/").lstrip("./")
    actual = rel.replace("\\", "/")
    if wanted == actual:
        return True
    wanted_name = Path(wanted).name
    actual_name = Path(actual).name
    return "/" not in wanted and wanted_name == actual_name


def _classify(name: str):
    suffix = Path(name).suffix.lower()
    if suffix in {".xls", ".xlsx", ".csv"}:
        return "spreadsheet", "表格"
    if suffix == ".pdf":
        return "pdf", "PDF"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return "image", "图片"
    if suffix in {".py", ".js", ".ts", ".go", ".rs", ".java"}:
        return "code", "代码"
    if suffix in {".md", ".txt", ".doc", ".docx", ".ppt", ".pptx"}:
        return "document", "文档"
    return "file", "文件"


def _sensitive(value: str) -> bool:
    name = Path(value).name.lower()
    if name in SENSITIVE_NAMES or name.startswith(".env"):
        return True
    if Path(name).suffix.lower() in SENSITIVE_SUFFIXES:
        return True
    return "secret" in name or "credential" in name


def _sha_text(value) -> str:
    text = str(value or "").strip()
    if len(text) < 7 or len(text) > 64:
        return ""
    return text if all(ch in "0123456789abcdefABCDEF" for ch in text) else ""


def _same_sha(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return left == right or left.startswith(right) or right.startswith(left)


def _baseline_sha(task: Dict[str, Any]) -> str:
    return _sha_text(task.get("baseline_commit"))


def _recorded_delivery_sha(task: Dict[str, Any]) -> str:
    values = [task.get("verified_candidate_sha"), task.get("integrated_commit"), task.get("commit")]
    commit_result = task.get("commit_result")
    if isinstance(commit_result, dict):
        values.extend([commit_result.get("sha"), commit_result.get("commit")])
    elif isinstance(commit_result, str):
        values.append(commit_result)
    for value in values:
        text = _sha_text(value)
        if text:
            return text
    return ""


def _head_sha(workspace: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(workspace), text=True, capture_output=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return _sha_text((result.stdout or "").strip())


def _commit_sha(task: Dict[str, Any], workspace: Optional[Path] = None, *, head=None) -> str:
    """Return this task's own commit, never the launch baseline."""
    baseline = _baseline_sha(task)
    delivery = _recorded_delivery_sha(task)
    if delivery and not _same_sha(delivery, baseline):
        return delivery
    if workspace is not None and (workspace / ".git").exists():
        head = _head_sha(workspace) if head is None else head
        if head and not _same_sha(head, baseline):
            return head
    candidate = _sha_text(task.get("candidate_sha"))
    if candidate and not _same_sha(candidate, baseline):
        return candidate
    return ""


def _owned_commit_files(task: Dict[str, Any], workspace: Path, *, head=None):
    baseline = _baseline_sha(task)
    if baseline:
        end = (_head_sha(workspace) if head is None else head) or _commit_sha(task, workspace, head=head)
        if not end or _same_sha(end, baseline):
            return [], ""
        return _diff_names(workspace, baseline, end)
    sha = _commit_sha(task, workspace, head=head)
    if not sha:
        return [], ""
    return _commit_files(workspace, sha), ""


def _diff_names(workspace: Path, base: str, end: str):
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotepath=false", "diff", "--name-only", base, end],
            cwd=str(workspace), text=True, capture_output=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [], "无法列出相对基线的变更，未列出的文件不代表尚未生成。"
    if result.returncode != 0:
        return [], "无法列出相对基线的变更，未列出的文件不代表尚未生成。"
    files = []
    for line in (result.stdout or "").splitlines():
        path = _git_path(line)
        if path and path not in files:
            files.append(path)
    return files, ""


def _pr_url(task: Dict[str, Any]) -> str:
    values = [task.get("pr_url"), task.get("pull_request_url")]
    pull = task.get("pull_request")
    if isinstance(pull, dict):
        values.extend([pull.get("url"), pull.get("html_url")])
    for value in values:
        text = str(value or "").strip()
        if text.startswith(("https://", "http://")) and " " not in text and "\n" not in text:
            return text
    return ""


def _dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    chosen: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for item in items:
        item_id = item["id"]
        current = chosen.get(item_id)
        if current is None:
            chosen[item_id] = item
            order.append(item_id)
            continue
        if current.get("status") != "present" and item.get("status") == "present":
            chosen[item_id] = item
    return [chosen[item_id] for item_id in order]


def _media_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".html", ".htm"}:
        return "application/octet-stream"
    if suffix == ".md":
        return "text/markdown; charset=utf-8"
    if suffix in {".txt", ".log", ".csv"}:
        return "text/plain; charset=utf-8"
    if suffix == ".json":
        return "application/json"
    if suffix == ".pdf":
        return "application/pdf"
    if suffix == ".xlsx":
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return f"image/{'jpeg' if suffix in {'.jpg', '.jpeg'} else suffix[1:]}"
    return "application/octet-stream"
