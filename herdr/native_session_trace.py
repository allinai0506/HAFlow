"""Read-only adapters for Codex, Claude Code, and OpenCode session facts.

The adapters emit tool facts. They do not decide task ownership beyond the
cwd and session id carried by the session itself. Callers must reject a
session that fails ``session_belongs`` before merging its actions.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


READ_TOOLS = {"read", "read_file"}
EDIT_TOOLS = {"edit", "write", "apply_patch", "str_replace", "strreplace"}
_EXEC_CMD = re.compile(r"exec_command\(\{cmd:\s*\"((?:\\.|[^\"\\])*)\"")
_TASK_ID = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_CLAUDE_UNSAFE = re.compile(r"[^A-Za-z0-9]")

MAX_FILES = 8
MAX_SCAN = 800
MAX_BYTES = 8 * 1024 * 1024
META_BYTES = 128 * 1024
MAX_ACTIONS = 2000


def session_belongs(task: Dict[str, Any], session: Dict[str, Any], *, exclusive: bool) -> bool:
    """Return whether a native session is proven to belong to this task.

    ``agent_session_id`` and pane id are ignored. A recorded ``native_session_id``
    must match the session's own id. Without that id, only an exclusive clone
    directory whose real path equals the session cwd is accepted.
    """
    clone = _norm_path(task.get("clone_path"))
    cwd = _norm_path(session.get("cwd"))
    if not clone or not cwd or clone != cwd:
        return False
    recorded = _recorded_native_id(task)
    session_id = str(session.get("session_id") or "")
    if recorded:
        return bool(session_id) and session_id == recorded
    return bool(exclusive)


def load_native_actions(
    task: Dict[str, Any],
    siblings: Sequence[Dict[str, Any]],
    roots: Optional[Dict[str, Path]] = None,
    *,
    budget_s: float = 4.0,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Load native actions for one task, or explain why they were refused."""
    notes: List[Dict[str, str]] = []
    clone = _norm_path(task.get("clone_path"))
    if not clone:
        notes.append(_note("native_unverified", "任务没有可核对的隔离目录，未读取原生会话。"))
        return [], notes
    if _identity_run_conflict(task):
        notes.append(_note("native_unverified", "启动身份属于其他运行，没有可核对的当前会话。"))
        return [], notes
    exclusive = _clone_exclusive(task, siblings)
    if not exclusive and not _recorded_native_id(task):
        notes.append(_note("native_unverified", "隔离目录不是该任务独占，未合并原生会话。"))
        return [], notes
    bound = dict(task)
    bound["_clone_exclusive"] = exclusive
    roots = roots or default_roots()
    deadline = time.monotonic() + max(0.2, float(budget_s))
    agent = str(bound.get("agent") or (bound.get("runtime") or {}).get("agent") or "")
    actions: List[Dict[str, Any]] = []
    truncated = False
    try:
        if agent == "codex":
            batch, batch_notes, truncated = _load_codex(bound, roots.get("codex_sessions"), deadline)
        elif agent == "claude":
            batch, batch_notes, truncated = _load_claude(bound, roots.get("claude_projects"), deadline)
        elif agent == "opencode":
            batch, batch_notes, truncated = _load_opencode(bound, roots.get("opencode_db"), deadline)
        else:
            batch, batch_notes = [], []
        actions.extend(batch)
        notes.extend(batch_notes)
    except (OSError, sqlite3.Error, TimeoutError):
        notes.append(_note("native_unavailable", "记录来源暂不可用。"))
        return [], notes
    if truncated or len(actions) > MAX_ACTIONS:
        actions = actions[:MAX_ACTIONS]
        notes.append(_note("native_truncated", "执行轨迹已截断，仅显示已读取的部分。"))
    if not actions and not notes and agent in {"codex", "claude", "opencode"}:
        notes.append(_note("native_unverified", "没有找到可归属到该任务的原生会话。"))
    return actions, _dedupe_notes(notes)


def default_roots() -> Dict[str, Path]:
    home = Path.home()
    return {
        "codex_sessions": home / ".codex" / "sessions",
        "claude_projects": home / ".claude" / "projects",
        "opencode_db": home / ".local" / "share" / "opencode" / "opencode.db",
    }


def parse_codex_jsonl(text: str, *, session_id: str = "") -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    calls: List[Dict[str, Any]] = []
    outputs: Dict[str, List[str]] = {}
    notes: List[Dict[str, str]] = []
    bad = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if not isinstance(record, dict) or record.get("type") != "response_item":
            continue
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        kind = payload.get("type")
        call_id = str(payload.get("call_id") or "")
        if kind in {"custom_tool_call", "function_call"}:
            if payload.get("timestamp") is None and record.get("timestamp") is not None:
                payload = dict(payload)
                payload["timestamp"] = record.get("timestamp")
            calls.append(payload)
        elif kind in {"custom_tool_call_output", "function_call_output"} and call_id:
            outputs.setdefault(call_id, []).append(_output_text(payload.get("output")))
    if bad and not calls:
        notes.append(_note("native_corrupt", "部分原生会话记录已损坏，已跳过无法解析的内容。"))
    return _actions_from_codex_calls(calls, outputs, session_id), notes


def parse_claude_jsonl(text: str, *, session_id: str = "") -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    uses: List[Dict[str, Any]] = []
    results: Dict[str, Dict[str, Any]] = {}
    bad = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if not isinstance(record, dict):
            continue
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        content = message.get("content")
        if not isinstance(content, list):
            continue
        timestamp = _parse_time(record.get("timestamp"))
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                uses.append({"block": block, "timestamp": timestamp, "session_id": record.get("sessionId") or session_id})
            elif block.get("type") == "tool_result" and block.get("tool_use_id"):
                results[str(block.get("tool_use_id"))] = block
    actions = []
    known_ids: Dict[str, str] = {}
    for ordinal, item in enumerate(uses, start=1):
        block = item["block"]
        tool = str(block.get("name") or "")
        tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
        tool_id = str(block.get("id") or "")
        result = results.get(tool_id)
        raw_retry = _link_text(tool_input.get("retry_of"))
        retry_of = _known_retry(raw_retry, known_ids, actions)
        action = _tool_action(
            session_id=str(item.get("session_id") or session_id),
            ordinal=ordinal,
            timestamp=item.get("timestamp"),
            tool=tool,
            path=_first_text(tool_input, ("file_path", "filePath", "path")),
            command=_first_text(tool_input, ("command", "cmd")),
            exit_code=_claude_exit(result),
            retry_of=retry_of,
            is_error=bool(result and result.get("is_error") is True),
            ref_prefix="claude",
        )
        actions.append(action)
        if tool_id:
            known_ids[tool_id] = action["source_ref"]
    notes = []
    if bad and not actions:
        notes.append(_note("native_corrupt", "部分原生会话记录已损坏，已跳过无法解析的内容。"))
    return actions, notes


def parse_opencode_parts(parts: Sequence[Dict[str, Any]], *, session_id: str = "") -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    actions = []
    bad = 0
    ordinal = 0
    known_ids: Dict[str, str] = {}
    for part in parts:
        raw = part.get("data")
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                bad += 1
                continue
        elif isinstance(raw, dict):
            data = raw
        else:
            continue
        if data.get("type") != "tool":
            continue
        ordinal += 1
        state = data.get("state") if isinstance(data.get("state"), dict) else {}
        tool_input = state.get("input") if isinstance(state.get("input"), dict) else {}
        metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
        raw_retry = _link_text(tool_input.get("retry_of") or data.get("retry_of"))
        action = _tool_action(
            session_id=session_id,
            ordinal=ordinal,
            timestamp=_parse_time(part.get("time_created")),
            tool=str(data.get("tool") or ""),
            path=_first_text(tool_input, ("filePath", "file_path", "path")),
            command=_first_text(tool_input, ("command", "cmd")),
            exit_code=_int_or_none(metadata.get("exit")),
            retry_of=_known_retry(raw_retry, known_ids, actions),
            is_error=str(state.get("status") or "") == "error",
            ref_prefix="opencode",
        )
        actions.append(action)
        part_id = part.get("id")
        if isinstance(part_id, str) and part_id:
            known_ids[part_id] = action["source_ref"]
    notes = []
    if bad and not actions:
        notes.append(_note("native_corrupt", "部分原生会话记录已损坏，已跳过无法解析的内容。"))
    return actions, notes


def _recent_jsonl(root: Path, limit: int, deadline: float) -> Tuple[List[Path], bool]:
    """Return up to ``limit`` JSONL files, newest directory and file names first.

    Each directory is sorted on its own. The walk stops once the limit is
    filled, so an older date tree is not listed just to sort the whole store.
    """
    found: List[Path] = []
    hit = False

    def walk(directory: Path) -> None:
        nonlocal hit
        if hit or len(found) >= limit or time.monotonic() > deadline:
            hit = True
            return
        try:
            entries = list(directory.iterdir())
        except OSError:
            return
        entries.sort(key=lambda item: item.name, reverse=True)
        for entry in entries:
            if len(found) >= limit or time.monotonic() > deadline:
                hit = True
                return
            name = entry.name
            if name.startswith("."):
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_dir:
                walk(entry)
                continue
            if name.endswith(".jsonl") and not name.endswith(".jsonl.zst"):
                found.append(entry)

    walk(root)
    return found, hit


def _load_codex(task, root, deadline):
    if root is None or not Path(root).is_dir():
        return [], [_note("native_unavailable", "记录来源暂不可用。")], False
    clone = _norm_path(task.get("clone_path"))
    matches: List[Tuple[str, Path]] = []
    candidates, budget_hit = _recent_jsonl(Path(root), MAX_SCAN, deadline)
    for path in candidates:
        cwd, session_id = _codex_meta(path)
        if _norm_path(cwd) == clone and session_id:
            matches.append((session_id, path))
            if len(matches) >= MAX_FILES:
                break
    owned = [(sid, path) for sid, path in matches if session_belongs(task, {"cwd": clone, "session_id": sid}, exclusive=_exclusive_flag(task))]
    actions: List[Dict[str, Any]] = []
    notes: List[Dict[str, str]] = []
    truncated = budget_hit
    for session_id, path in owned:
        if time.monotonic() > deadline:
            truncated = True
            break
        text, cut = _read_text(path, MAX_BYTES)
        truncated = truncated or cut
        batch, batch_notes = parse_codex_jsonl(text, session_id=session_id)
        actions.extend(batch)
        notes.extend(batch_notes)
    if budget_hit:
        notes.append(_note("native_index_budget", "原生会话索引已达到读取上限。"))
    return actions, notes, truncated


def _claude_candidates(folder: Path, recorded: str, deadline: float) -> Tuple[List[Path], bool]:
    """Keep a pinned session even when it sorts after the file cap."""
    names = sorted(folder.glob("*.jsonl"), key=lambda item: item.name)
    pinned = None
    if recorded and "/" not in recorded and "\\" not in recorded and recorded not in {".", ".."}:
        direct = folder / f"{recorded}.jsonl"
        if direct.is_file() and direct.parent == folder:
            pinned = direct
        else:
            for path in names:
                if time.monotonic() > deadline:
                    break
                head, _ = _read_text(path, 65536)
                _, session_id = _claude_identity(head)
                if session_id == recorded:
                    pinned = path
                    break
    ordered = []
    if pinned is not None:
        ordered.append(pinned)
    for path in names:
        if path == pinned:
            continue
        ordered.append(path)
    return ordered[:MAX_FILES], len(ordered) > MAX_FILES


def _load_claude(task, root, deadline):
    if root is None or not Path(root).is_dir():
        return [], [_note("native_unavailable", "记录来源暂不可用。")], False
    clone = _norm_path(task.get("clone_path")) or ""
    folder = Path(root) / _CLAUDE_UNSAFE.sub("-", clone)
    if not folder.is_dir():
        return [], [], False
    actions: List[Dict[str, Any]] = []
    notes: List[Dict[str, str]] = []
    files, truncated = _claude_candidates(folder, _recorded_native_id(task), deadline)
    for path in files:
        if time.monotonic() > deadline:
            truncated = True
            break
        text, cut = _read_text(path, MAX_BYTES)
        truncated = truncated or cut
        cwd, session_id = _claude_identity(text)
        if not cwd and not session_id:
            notes.append(_note("native_corrupt", "部分原生会话记录已损坏，已跳过无法解析的内容。"))
            continue
        if not session_belongs(task, {"cwd": cwd, "session_id": session_id}, exclusive=_exclusive_flag(task)):
            continue
        batch, batch_notes = parse_claude_jsonl(text, session_id=session_id)
        actions.extend(batch)
        notes.extend(batch_notes)
    return actions, notes, truncated


def _load_opencode(task, db_path, deadline):
    if db_path is None or not Path(db_path).is_file():
        return [], [_note("native_unavailable", "记录来源暂不可用。")], False
    if time.monotonic() > deadline:
        return [], [_note("native_index_budget", "原生会话索引已达到读取上限。")], True
    clone = _norm_path(task.get("clone_path"))
    task_id = str(task.get("task_id") or "")
    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    except sqlite3.Error:
        return [], [_note("native_unavailable", "记录来源暂不可用。")], False
    try:
        conn.execute("PRAGMA query_only=ON;")
        rows = conn.execute(
            "SELECT id, directory FROM session WHERE directory = ?",
            (task.get("clone_path"),),
        ).fetchall()
        if not rows and _TASK_ID.match(task_id):
            rows = conn.execute(
                "SELECT id, directory FROM session WHERE directory LIKE ? ESCAPE '\\' LIMIT 20",
                ("%/" + task_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"),),
            ).fetchall()
        owned = []
        for session_id, directory in rows:
            if session_belongs(task, {"cwd": directory, "session_id": str(session_id)}, exclusive=_exclusive_flag(task)):
                owned.append(str(session_id))
        actions: List[Dict[str, Any]] = []
        notes: List[Dict[str, str]] = []
        truncated = False
        for session_id in owned[:MAX_FILES]:
            if time.monotonic() > deadline:
                truncated = True
                break
            parts = conn.execute(
                "SELECT id, time_created, data FROM part WHERE session_id = ? ORDER BY time_created ASC, id ASC LIMIT ?",
                (session_id, MAX_ACTIONS),
            ).fetchall()
            payload = [{"id": item[0], "time_created": item[1], "data": item[2]} for item in parts]
            batch, batch_notes = parse_opencode_parts(payload, session_id=session_id)
            actions.extend(batch)
            notes.extend(batch_notes)
            if len(parts) >= MAX_ACTIONS:
                truncated = True
        return actions, notes, truncated
    except sqlite3.Error:
        return [], [_note("native_corrupt", "部分原生会话记录已损坏，已跳过无法解析的内容。")], False
    finally:
        conn.close()


def _actions_from_codex_calls(calls, outputs, session_id):
    actions = []
    ordinal = 0
    known_ids = []
    for payload in calls:
        call_id = str(payload.get("call_id") or "")
        name = str(payload.get("name") or "")
        timestamp = _codex_time(payload)
        retry_of = _codex_retry(payload, known_ids)
        blob = "\n".join(outputs.get(call_id, []))
        if name == "exec" or payload.get("type") == "custom_tool_call" and name == "exec":
            commands = _exec_commands(str(payload.get("input") or ""))
            exits = _exit_codes(blob)
            assigned = _assign_exits(commands, exits)
            for command, exit_code in assigned:
                ordinal += 1
                ref = f"codex:{session_id}:{ordinal}"
                actions.append(_tool_action(
                    session_id=session_id, ordinal=ordinal, timestamp=timestamp, tool="exec",
                    path=None, command=command, exit_code=exit_code, retry_of=retry_of, is_error=False,
                    ref_prefix="codex",
                ))
                known_ids.append((call_id, ref))
                retry_of = None
            continue
        ordinal += 1
        arguments = _object_args(payload.get("arguments"))
        exits = _exit_codes(blob)
        actions.append(_tool_action(
            session_id=session_id,
            ordinal=ordinal,
            timestamp=timestamp,
            tool=name,
            path=_first_text(arguments, ("path", "file", "file_path", "filePath")),
            command=_first_text(arguments, ("command", "cmd")),
            exit_code=exits[-1] if len(exits) == 1 else None,
            retry_of=retry_of,
            is_error=False,
            ref_prefix="codex",
        ))
        known_ids.append((call_id, f"codex:{session_id}:{ordinal}"))
    return actions


def _exec_commands(raw: str) -> List[str]:
    commands = []
    for match in _EXEC_CMD.finditer(raw or ""):
        try:
            commands.append(json.loads('"' + match.group(1) + '"'))
        except json.JSONDecodeError:
            continue
    return [item for item in commands if isinstance(item, str) and item.strip()]


def _exit_codes(blob: str) -> List[int]:
    if not blob:
        return []
    decoder = json.JSONDecoder()
    index = 0
    found: List[int] = []
    length = len(blob)
    while index < length and len(found) < 50:
        while index < length and blob[index] not in "{[":
            index += 1
        if index >= length:
            break
        try:
            obj, end = decoder.raw_decode(blob, index)
        except json.JSONDecodeError:
            index += 1
            continue
        if isinstance(obj, dict):
            code = _int_or_none(obj.get("exit_code"))
            if code is not None:
                found.append(code)
        index = max(end, index + 1)
    return found


def _assign_exits(commands: Sequence[str], exits: Sequence[int]) -> List[Tuple[str, Optional[int]]]:
    if len(commands) == 1:
        return [(commands[0], exits[-1] if exits else None)]
    if len(commands) == len(exits):
        return list(zip(commands, exits))
    return [(command, None) for command in commands]


def _codex_retry(payload: Dict[str, Any], known_ids: Sequence[Tuple[str, str]]) -> Optional[str]:
    raw = payload.get("retry_of") or payload.get("retried_call_id")
    if not isinstance(raw, str) or not raw:
        return None
    for call_id, ref in known_ids:
        if raw == call_id:
            return ref
    return None


def _codex_meta(path: Path) -> Tuple[Optional[str], str]:
    try:
        with path.open("rb") as handle:
            blob = handle.read(META_BYTES)
    except OSError:
        return None, ""
    text = blob.decode("utf-8", errors="ignore")
    cwd = _json_string_field(text, "cwd")
    session_id = _json_string_field(text, "session_id") or _json_string_field(text, "id")
    return cwd, session_id or ""


def _claude_identity(text: str) -> Tuple[Optional[str], str]:
    cwd = None
    session_id = ""
    for index, line in enumerate(text.splitlines()):
        if index > 40:
            break
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if isinstance(record.get("cwd"), str):
            cwd = record["cwd"]
        if isinstance(record.get("sessionId"), str):
            session_id = record["sessionId"]
        if cwd and session_id:
            break
    return cwd, session_id


def _json_string_field(text: str, name: str) -> Optional[str]:
    match = re.search(rf'"{re.escape(name)}"\s*:\s*"((?:\\.|[^"\\])*)"', text)
    if not match:
        return None
    try:
        value = json.loads('"' + match.group(1) + '"')
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) else None


def _tool_action(*, session_id, ordinal, timestamp, tool, path, command, exit_code, retry_of, is_error, ref_prefix):
    kind = None
    lowered = str(tool or "").replace("-", "_").lower()
    if lowered in READ_TOOLS:
        kind = "file_read"
    elif lowered in EDIT_TOOLS:
        kind = "file_edit"
    return {
        "session_id": str(session_id or ""),
        "ordinal": int(ordinal),
        "timestamp": timestamp if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool) else None,
        "tool": str(tool or ""),
        "path": path if isinstance(path, str) else None,
        "command": command if isinstance(command, str) else None,
        "exit_code": exit_code,
        "retry_of": retry_of if isinstance(retry_of, str) else None,
        "is_error": bool(is_error),
        "kind": kind,
        "source_ref": f"{ref_prefix}:{session_id}:{int(ordinal)}",
    }


def _claude_exit(result: Optional[Dict[str, Any]]) -> Optional[int]:
    if not isinstance(result, dict):
        return None
    for key in ("exit_code", "exitCode", "exit"):
        code = _int_or_none(result.get(key))
        if code is not None:
            return code
    content = result.get("content")
    text = content if isinstance(content, str) else _output_text(content)
    match = re.match(r"Exit code (\d+)\b", text.strip()) if text else None
    if match:
        return int(match.group(1))
    return None


def _output_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _object_args(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _first_text(data: Dict[str, Any], keys: Iterable[str]) -> Optional[str]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _link_text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value.strip() else None


def _known_retry(raw: Optional[str], known_ids: Dict[str, str], actions: Sequence[Dict[str, Any]]) -> Optional[str]:
    if not raw:
        return None
    if raw in known_ids:
        return known_ids[raw]
    if any(item.get("source_ref") == raw for item in actions):
        return raw
    return None


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _parse_time(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1e12:
            number /= 1000.0
        return number
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            pass
        try:
            from datetime import datetime
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed.timestamp()
        except ValueError:
            return None
    return None


def _codex_time(payload: Dict[str, Any]) -> Optional[float]:
    meta = payload.get("internal_chat_message_metadata_passthrough")
    if isinstance(meta, dict):
        parsed = _parse_time(meta.get("create_time"))
        if parsed is not None:
            return parsed
    return _parse_time(payload.get("timestamp"))


def _recorded_native_id(task: Dict[str, Any]) -> str:
    runtime = task.get("runtime") if isinstance(task.get("runtime"), dict) else {}
    for source in (runtime, task):
        value = source.get("native_session_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    identity = _launch_identity(task)
    value = identity.get("native_session_id") if isinstance(identity, dict) else None
    if isinstance(value, str) and value.strip() and identity.get("task_id") == task.get("task_id"):
        if (identity.get("run_id") or None) != (task.get("run_id") or None):
            return ""
        return value.strip()
    return ""


def _identity_run_conflict(task: Dict[str, Any]) -> bool:
    runtime = task.get("runtime") if isinstance(task.get("runtime"), dict) else {}
    for source in (runtime, task):
        value = source.get("native_session_id")
        if isinstance(value, str) and value.strip():
            return False
    identity = _launch_identity(task)
    if not isinstance(identity, dict) or not identity:
        return False
    if identity.get("task_id") != task.get("task_id"):
        return False
    value = identity.get("native_session_id")
    if not isinstance(value, str) or not value.strip():
        return False
    return (identity.get("run_id") or None) != (task.get("run_id") or None)


def _launch_identity(task: Dict[str, Any]) -> Dict[str, Any]:
    clone = task.get("clone_path")
    if not isinstance(clone, str) or not clone:
        return {}
    path = Path(clone) / ".herdr-launch-identity.json"
    try:
        if not path.is_file() or path.stat().st_size > 65536:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _clone_exclusive(task: Dict[str, Any], siblings: Sequence[Dict[str, Any]]) -> bool:
    clone = _norm_path(task.get("clone_path"))
    if not clone:
        return False
    task_id = task.get("task_id")
    for other in siblings:
        if not isinstance(other, dict) or other.get("task_id") == task_id:
            continue
        if _norm_path(other.get("clone_path")) == clone:
            return False
    return True


def _exclusive_flag(task: Dict[str, Any]) -> bool:
    return bool(task.get("_clone_exclusive"))


def _norm_path(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    return os.path.normpath(os.path.abspath(os.path.expanduser(value.strip())))


def _read_text(path: Path, limit: int) -> Tuple[str, bool]:
    with path.open("rb") as handle:
        blob = handle.read(limit + 1)
    cut = len(blob) > limit
    return blob[:limit].decode("utf-8", errors="ignore"), cut


def _note(code: str, message: str) -> Dict[str, str]:
    return {"code": code, "message": message}


def _dedupe_notes(notes: Sequence[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    result = []
    for note in notes:
        key = note.get("code")
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(note))
    return result
