"""Read-only projection of one task's execution trace.

Task documents, the existing SQLite event store, and native session adapters
stay the facts. This module only normalizes, orders, redacts, and pages them.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .native_session_trace import _norm_path, load_native_actions
from .state_db import get_readonly_db_connection
from .supervisor.state import redact_text
from .trajectory import run_id_for_task


MAX_LIMIT = 200
DEFAULT_LIMIT = 100
STORED_LIMIT = 2000
_CORRUPT_SAMPLE_CAP = 8
_MISSING = object()
_PROJECTABLE_EVENT_TYPES = (
    "agent_started", "task_started", "task_completed", "initial_dispatched",
    "task_status_changed", "task_transition",
    "verification_completed", "tests_completed", "blocked_marker_observed",
    "task_artifact_checkpoint", "candidate_published", "candidate_frozen",
    "tool_execution",
)
AGENT_ACTION_KINDS = frozenset({
    "file_read", "file_edit", "tool_call", "test_failed", "test_passed", "retry",
})
_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")
_SECRET_NAME = re.compile(r"(^|/)(\.env|id_rsa|credentials|cookies|secrets)(\.|$)|[.]pem$|[.]key$", re.I)
_SHELL_SPLIT = re.compile(r"&&|\|\||[|;]|`|\$\(|(?<![>&])&(?![&>])|" + "[\n\r]")
_TEST_BINARIES = {"pytest", "py.test", "tox"}
_PUBLIC_FIELDS = (
    "id", "task_id", "run_id", "timestamp", "sequence", "kind", "title",
    "detail", "status", "agent", "source", "source_ref", "details",
)
_DETAIL_FIELDS = (
    "agent", "tool", "path", "command", "result", "exit_code", "timestamp",
    "source", "source_ref", "observation_id", "sha256",
)


_TASK_COLUMNS = (
    "task_id", "workflow_id", "node", "stage", "agent", "status",
    "stage_verdict", "stage_verdict_note", "pane_id", "goal", "blocker",
    "created_at", "updated_at", "version",
)


def _task_from_row(row) -> Dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    task = dict(payload)
    task.update({
        "task_id": row["task_id"],
        "workflow_id": row["workflow_id"],
        "node": row["node"],
        "stage": row["stage"],
        "agent": row["agent"],
        "status": row["status"],
        "stage_verdict": row["stage_verdict"],
        "stage_verdict_note": row["stage_verdict_note"],
        "pane_id": row["pane_id"],
        "goal": row["goal"],
        "blocker": row["blocker"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "version": int(row["version"] or 0),
    })
    return task


def _select_tasks(where: str) -> str:
    return (
        "SELECT "
        + ", ".join(_TASK_COLUMNS)
        + ", payload_json FROM tasks "
        + where
    )


def read_task_records(db_path) -> List[Dict[str, Any]]:
    """Read task documents through the read-only state connection.

    Column values overlay ``payload_json`` the same way ``list_tasks`` does.
    This function does not create the database or run schema migration.
    """
    conn = get_readonly_db_connection(db_path)
    try:
        conn.execute("PRAGMA query_only=ON;")
        rows = conn.execute(
            _select_tasks("ORDER BY created_at ASC, task_id ASC")
        ).fetchall()
    finally:
        conn.close()
    return [_task_from_row(row) for row in rows]


def read_trace_tasks(db_path, task_id: str) -> List[Dict[str, Any]]:
    """Read one task and the other tasks that share its clone directory.

    Unrelated task payloads stay in the database. Clone ownership still
    compares normalized paths, using only the stored path string to decide
    which full documents to decode.
    """
    conn = get_readonly_db_connection(db_path)
    try:
        conn.execute("PRAGMA query_only=ON;")
        row = conn.execute(_select_tasks("WHERE task_id = ?"), (task_id,)).fetchone()
        if row is None:
            return []
        task = _task_from_row(row)
        clone = _norm_path(task.get("clone_path"))
        if not clone:
            return [task]
        paths = conn.execute(
            """
            SELECT task_id, json_extract(payload_json, '$.clone_path') AS clone_path
            FROM tasks
            WHERE task_id != ?
              AND CASE WHEN json_valid(payload_json)
                       THEN json_extract(payload_json, '$.clone_path') END IS NOT NULL
            """,
            (task_id,),
        ).fetchall()
        share = [
            item["task_id"]
            for item in paths
            if _norm_path(item["clone_path"]) == clone
        ]
        if not share:
            return [task]
        marks = ",".join("?" for _ in share)
        siblings = conn.execute(
            _select_tasks(f"WHERE task_id IN ({marks})"),
            tuple(share),
        ).fetchall()
    finally:
        conn.close()
    return [task, *(_task_from_row(item) for item in siblings)]


def query_task_trace(
    task: Dict[str, Any],
    siblings: Sequence[Dict[str, Any]],
    *,
    db_path,
    limit: int = DEFAULT_LIMIT,
    cursor: Optional[str] = None,
    roots=None,
    budget_s: float = 4.0,
) -> Dict[str, Any]:
    """Build one page of a task trace without writing state or starting agents."""
    stored, notes = read_stored_events(task, db_path)
    native, native_notes = load_native_actions(task, siblings, roots, budget_s=budget_s)
    notes.extend(native_notes)
    return assemble_trace(
        task,
        stored,
        native,
        degradation=_dedupe_notes(notes),
        limit=limit,
        cursor=cursor,
    )


def assemble_trace(
    task: Dict[str, Any],
    stored_rows: Sequence[Dict[str, Any]],
    native_actions: Sequence[Dict[str, Any]],
    *,
    degradation: Sequence[Dict[str, str]] = (),
    limit: int = DEFAULT_LIMIT,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    events = []
    verdicts = _verification_verdicts(stored_rows)
    for row in stored_rows:
        if _tests_row_has_verdict(row, verdicts):
            continue
        projected = project_stored_event(row, task)
        if projected is not None:
            events.append(projected)
    events.extend(project_native_actions(native_actions, task))
    events.extend(project_task_history(task))
    merged = merge_trace_events(events)
    page, has_more, next_cursor, page_limit = paginate_trace(merged, limit, cursor)
    availability = trace_availability(merged)
    return {
        "task_id": task.get("task_id"),
        "run_id": task.get("run_id"),
        "workflow_id": task.get("workflow_id"),
        "agent": task.get("agent") or (task.get("runtime") or {}).get("agent"),
        "availability": availability,
        "events": page,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "limit": page_limit,
        "degraded": bool(degradation),
        "degradation": [dict(item) for item in degradation],
    }


def _report_text(value: Any) -> str:
    return _clip(redact_text(value)) if isinstance(value, str) else ""


def _evidence_sha(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) else None


def _transition_correlation(task_id, status, timestamp):
    if timestamp is None or not status:
        return None
    return f"transition:{task_id}:{status}:{float(timestamp):.6f}"


def project_task_history(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    task_id = task.get("task_id")
    run_id = task.get("run_id")
    agent = task.get("agent") or (task.get("runtime") or {}).get("agent")
    events = []
    created = _time(task.get("created_at"))
    if created is not None:
        events.append(_event(
            task, kind="task_created", title="任务已创建", status="neutral",
            timestamp=created, sequence=0, source="task_history",
            source_ref=f"task:{task_id}:created", agent=agent,
            correlation_id=f"created:{task_id}:{created}",
        ))
    history = task.get("status_history") if isinstance(task.get("status_history"), list) else []
    for index, item in enumerate(history):
        if not isinstance(item, dict):
            continue
        status = item.get("to") or item.get("status")
        mapped = _status_kind(status)
        if mapped is None:
            continue
        kind, title, tone = mapped
        item_sha = _commit_sha(item)
        if kind == "commit" and not item_sha:
            # A commit transition without its own SHA is only a completion.
            kind, title, tone = "task_completed", "已提交", "info"
        timestamp = _time(item.get("timestamp"))
        if timestamp is None:
            timestamp = _time(item.get("at"))
        correlation = None
        if status == "pending" and created is not None and timestamp == created:
            correlation = f"created:{task_id}:{created}"
        elif kind == "commit":
            correlation = f"commit:{task_id}:{item_sha}"
        else:
            correlation = _transition_correlation(task_id, status, timestamp)
        events.append(_event(
            task, kind=kind, title=title, status=tone, timestamp=timestamp,
            sequence=index + 1, source="task_history",
            source_ref=f"task:{task_id}:status:{index}", agent=agent,
            correlation_id=correlation,
            detail=f"Commit · {item_sha[:7]}" if kind == "commit" else "",
            details=_details(agent=agent, source="task_history", source_ref=f"task:{task_id}:status:{index}", timestamp=timestamp),
        ))
    runtime = task.get("runtime") if isinstance(task.get("runtime"), dict) else {}
    started = _time(task.get("started_at"))
    if started is None:
        started = _time(runtime.get("started_at"))
    if started is not None:
        events.append(_event(
            task, kind="agent_started", title="开始执行", status="info",
            timestamp=started, sequence=10_000, source="task_history",
            source_ref=f"task:{task_id}:started", agent=agent,
        ))
    sha = _commit_sha(task)
    if sha:
        events.append(_event(
            task, kind="commit", title="生成提交", status="neutral",
            detail=f"Commit · {sha[:7]}", timestamp=None, sequence=10_001,
            source="task_history", source_ref=f"task:{task_id}:commit", agent=agent,
            correlation_id=f"commit:{task_id}:{sha}",
            details=_details(agent=agent, result=sha[:7], source="task_history", source_ref=f"task:{task_id}:commit"),
        ))
    return events


def project_stored_event(row: Dict[str, Any], task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if row.get("task_id") not in (None, task.get("task_id")):
        return None
    row_run = row.get("run_id") or None
    task_run = task.get("run_id") or None
    # A ledger row with no run_id is legacy evidence. Keep it, but do not
    # stamp it with the task's current run. A task that itself has no
    # persisted run_id still owns rows written under run_id_for_task().
    if row_run is None:
        pass
    elif task_run:
        if row_run != task_run:
            return None
    elif not task.get("task_id") or row_run != run_id_for_task(task):
        return None
    event_type = str(row.get("event_type") or "")
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    origin = "trajectory" if row.get("source") == "trajectory" else "task_history"
    source_ref = f"{origin}:{row.get('id')}"
    agent = row.get("agent_id") or payload.get("agent") or task.get("agent")
    timestamp = _time(row.get("timestamp"))
    sequence = row.get("sequence") if isinstance(row.get("sequence"), int) else int(row.get("id") or 0)
    base = dict(kind=None, title="", status="neutral", detail="", details={})

    if event_type in {"agent_started", "task_started"}:
        base.update(kind="agent_started", title="开始执行", status="info")
    elif event_type == "task_completed":
        base.update(kind="task_completed", title="任务完成", status="info")
    elif event_type == "initial_dispatched":
        target = task.get("goal") or task.get("stage_label") or task.get("node") or task.get("stage")
        parts = [f"执行者：{agent}" if agent else "", f"目标：{target}" if target else ""]
        base.update(kind="task_dispatched", title="任务已派发", status="info",
                    detail=_report_text(" · ".join(p for p in parts if p)))
    elif event_type == "task_artifact_checkpoint":
        summary = _report_text(payload.get("summary"))
        next_step = _report_text(payload.get("next_step"))
        step = payload.get("step")
        title = f"执行者报告 · 第 {step} 步" if isinstance(step, int) else "执行者报告"
        detail = " · ".join(p for p in [summary, f"下一步：{next_step}" if next_step else ""] if p)
        base.update(kind="progress_report", title=title, detail=detail)
        base["details"] = _details(
            agent=agent, result=summary, observation_id=_safe_token(payload.get("observation_id")),
            sha256=_evidence_sha(payload.get("sha256")),
            source=origin, source_ref=source_ref, timestamp=timestamp,
        )
    elif event_type in {"candidate_published", "candidate_frozen"}:
        sha = payload.get("candidate_sha")
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            return None
        base.update(kind="candidate", title="候选版本已发布" if event_type == "candidate_published" else "候选版本已冻结",
                    detail=f"Commit · {sha}")
    elif event_type == "tool_execution":
        result = _report_text(payload.get("status"))
        effects = _report_text(payload.get("side_effects"))
        exit_code = payload.get("exit_code")
        exit_code = exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None
        base.update(kind="tool_receipt", title="工具执行回执",
                    detail=f"状态：{result or 'unknown'} · 退出码：{exit_code if exit_code is not None else 'unknown'} · 副作用：{effects or 'unknown'}")
        base["details"] = _details(
            agent=agent, result=result, exit_code=exit_code,
            observation_id=_safe_token(payload.get("observation_id")), sha256=_evidence_sha(payload.get("sha256")),
            source=origin, source_ref=source_ref, timestamp=timestamp,
        )
    elif event_type in {"task_status_changed", "task_transition"}:
        to_status = (
            metadata.get("to_status")
            or payload.get("to_status")
            or payload.get("status")
            or row.get("status")
        )
        mapped = _status_kind(to_status)
        if mapped is None:
            return None
        kind, title, tone = mapped
        if kind == "commit":
            kind, title, tone = "task_completed", "已提交", "info"
        base.update(kind=kind, title=title, status=tone)
        created = _time(task.get("created_at"))
        if to_status == "pending" and created is not None and timestamp == created:
            base["correlation_id"] = f"created:{task.get('task_id')}:{created}"
        else:
            base["correlation_id"] = _transition_correlation(task.get("task_id"), to_status, timestamp)
    elif event_type in {"verification_completed", "tests_completed"}:
        kind, title, tone, detail, result = _verification_view(payload, event_type)
        base.update(kind=kind, title=title, status=tone, detail=detail)
        base["details"] = _details(agent=agent, result=result, source=origin, source_ref=source_ref, timestamp=timestamp)
    elif event_type == "blocked_marker_observed":
        base.update(kind="task_blocked", title="任务已阻塞", status="error")
    else:
        return None
    if not base["details"]:
        base["details"] = _details(agent=agent, source=origin, source_ref=source_ref, timestamp=timestamp)
    return _event(
        task, kind=base["kind"], title=base["title"], status=base["status"],
        detail=base["detail"], timestamp=timestamp, sequence=sequence,
        source=origin, source_ref=source_ref, agent=agent, details=base["details"],
        run_id=row_run, correlation_id=base.get("correlation_id"),
    )


def project_native_actions(actions: Sequence[Dict[str, Any]], task: Dict[str, Any]) -> List[Dict[str, Any]]:
    known = {}
    events = []
    agent = task.get("agent")
    for action in actions:
        source_ref = str(action.get("source_ref") or "")
        if not source_ref:
            continue
        timestamp = _time(action.get("timestamp"))
        ordinal = action.get("ordinal") if isinstance(action.get("ordinal"), int) else 0
        sequence = ordinal * 2 + 1
        retry_of = action.get("retry_of")
        action = dict(action)
        if isinstance(action.get("path"), str):
            action["path"] = _relativize_path(action["path"], task.get("clone_path"))
        classified = _classify_action(action)
        previous = known.get(retry_of) if isinstance(retry_of, str) else None
        valid_retry = (
            previous is not None
            and previous.get("session_id") == action.get("session_id")
            and isinstance(previous.get("ordinal"), int)
            and previous["ordinal"] < ordinal
            and retry_of != source_ref
        )
        if valid_retry:
            runner = classified.get("runner") or classified.get("command") or classified.get("tool")
            events.append(_event(
                task, kind="retry", title=_retry_title(previous, classified), status="info",
                detail=_clip(f"Retry · {runner}" if runner else "Retry"),
                timestamp=timestamp, sequence=max(sequence - 1, 0), source="native_session",
                source_ref=source_ref + ":retry", agent=agent,
                details=_details(
                    agent=agent, tool=classified.get("tool"), command=runner,
                    source="native_session", source_ref=source_ref + ":retry", timestamp=timestamp,
                ),
            ))
        correlation = None
        if classified["kind"] == "commit":
            sha = action.get("sha")
            if isinstance(sha, str) and sha:
                correlation = f"commit:{task.get('task_id')}:{sha}"
        events.append(_event(
            task, kind=classified["kind"], title=classified["title"], status=classified["status"],
            detail=classified["detail"], timestamp=timestamp, sequence=sequence,
            source="native_session", source_ref=source_ref, agent=agent,
            details=classified["details"], correlation_id=correlation,
        ))
        known.setdefault(source_ref, action)
    return events


def _retry_title(previous: dict[str, Any], classified: dict[str, Any]) -> str:
    """Name a retry by what ran before and what runs now, never by guess."""
    prior_kind = _classify_action(previous).get("kind")
    current_test = classified.get("kind") in {"test_passed", "test_failed"}
    prior_test = prior_kind in {"test_passed", "test_failed"}
    if current_test and prior_test:
        return "修复并再次测试"
    if current_test:
        return "再次测试"
    return "重试工具调用"


def merge_trace_events(events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen_ref = set()
    seen_corr = set()
    merged = []
    for event in events:
        ref = (event.get("source"), event.get("source_ref"))
        if ref in seen_ref:
            continue
        correlation = event.get("correlation_id")
        if correlation:
            if correlation in seen_corr:
                continue
            seen_corr.add(correlation)
        seen_ref.add(ref)
        merged.append(_public_event(event))
    merged.sort(key=_sort_key)
    return merged


def paginate_trace(events: Sequence[Dict[str, Any]], limit: int, cursor: Optional[str]):
    try:
        page_limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit 无效") from exc
    if page_limit < 1 or page_limit > MAX_LIMIT:
        raise ValueError("limit 无效")
    start = 0
    if cursor:
        ids = [event.get("id") for event in events]
        if cursor not in ids:
            raise ValueError("cursor 无效")
        start = ids.index(cursor) + 1
    page = list(events[start:start + page_limit])
    has_more = start + page_limit < len(events)
    next_cursor = page[-1]["id"] if has_more and page else None
    return page, has_more, next_cursor, page_limit


def trace_availability(events: Sequence[Dict[str, Any]]) -> str:
    kinds = {event.get("kind") for event in events}
    if kinds & AGENT_ACTION_KINDS:
        return "agent_actions"
    if kinds & {"progress_report", "candidate", "tool_receipt"}:
        return "recorded_actions"
    if events:
        return "task_events"
    return "none"


def decode_stored_payload(payload_json: Any) -> Tuple[Dict[str, Any], bool]:
    """Decode one ledger ``payload_json`` into ``(payload, corrupt)``.

    NULL, empty, and valid object documents are not corrupt: legacy empty
    payloads stay ``(obj, False)``. Invalid JSON or valid non-object JSON is
    corrupt and yields no payload.
    """
    if payload_json is None or payload_json == "":
        return {}, False
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return {}, True
    if not isinstance(payload, dict):
        return {}, True
    return payload, False


def _corrupt_payload_note(corrupt_ids: Sequence[Any]) -> Dict[str, Any]:
    """One bounded aggregate note; sample row ids only, never payload text."""
    return {
        "code": "stored_payload_corrupt",
        "message": "部分账本事件载荷已损坏，已跳过无法解析的内容。",
        "details": {
            "corrupt_count": len(corrupt_ids),
            "sample_ids": [str(value) for value in corrupt_ids[:_CORRUPT_SAMPLE_CAP]],
        },
    }


def read_stored_events(task: Dict[str, Any], db_path) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    try:
        conn = get_readonly_db_connection(db_path)
    except FileNotFoundError:
        return [], [{"code": "trajectory_unavailable", "message": "任务轨迹数据库暂不可用。"}]
    try:
        try:
            conn.execute("PRAGMA query_only=ON;")
            task_run = task.get("run_id") or None
            if not task_run and task.get("task_id"):
                task_run = run_id_for_task(task)
            type_marks = ",".join("?" for _ in _PROJECTABLE_EVENT_TYPES)
            if task_run:
                run_sql = "(run_id IS NULL OR run_id = '' OR run_id = ?)"
                run_params = (task_run,)
            else:
                run_sql = "(run_id IS NULL OR run_id = '')"
                run_params = ()
            rows = conn.execute(
                f"""
                SELECT id, task_id, agent_id, event_type, timestamp, source, run_id, sequence, payload_json
                FROM events
                WHERE task_id = ?
                  AND event_type IN ({type_marks})
                  AND {run_sql}
                ORDER BY timestamp ASC, id ASC
                LIMIT ?
                """,
                (task.get("task_id"), *_PROJECTABLE_EVENT_TYPES, *run_params, STORED_LIMIT),
            ).fetchall()
            if len(rows) >= STORED_LIMIT:
                seen = {row["id"] for row in rows}
                rows = list(rows) + [
                    row for row in _receipts_for_scope(conn, task.get("task_id"), run_sql, run_params)
                    if row["id"] not in seen
                ]
        except sqlite3.Error:
            return [], [{"code": "trajectory_unavailable", "message": "任务轨迹数据库暂不可用。"}]
    finally:
        conn.close()
    events = []
    corrupt_ids = []
    for row in rows:
        payload, corrupt = decode_stored_payload(row["payload_json"])
        if corrupt:
            corrupt_ids.append(row["id"])
            continue
        events.append({
            "id": row["id"],
            "task_id": row["task_id"],
            "agent_id": row["agent_id"],
            "event_type": row["event_type"],
            "timestamp": row["timestamp"],
            "source": row["source"],
            "run_id": row["run_id"],
            "sequence": row["sequence"],
            "payload": payload,
        })
    notes = []
    if corrupt_ids:
        notes.append(_corrupt_payload_note(corrupt_ids))
    if len(rows) >= STORED_LIMIT:
        notes.append({"code": "native_truncated", "message": "执行轨迹已截断，仅显示已读取的部分。"})
    return events, notes


def _receipts_for_scope(conn, task_id, run_sql: str, run_params: Sequence[Any]) -> List[Any]:
    """Read verification receipts for the same scope, past the stored window.

    The window keeps the oldest rows, so a later authoritative receipt can
    fall outside it while its superseded ``tests_completed`` row stays in
    view. Receipts are one per checkpoint, so reading them stays bounded.
    """
    return conn.execute(
        f"""
        SELECT id, task_id, agent_id, event_type, timestamp, source, run_id, sequence, payload_json
        FROM events
        WHERE task_id = ?
          AND event_type = 'verification_completed'
          AND {run_sql}
        ORDER BY timestamp ASC, id ASC
        LIMIT ?
        """,
        (task_id, *run_params, STORED_LIMIT),
    ).fetchall()


def _classify_action(action: Dict[str, Any]) -> Dict[str, Any]:
    tool = _safe_token(action.get("tool"))
    path = _safe_path(action.get("path"))
    command_text = action.get("command") if isinstance(action.get("command"), str) else ""
    compound = bool(_SHELL_SPLIT.search(command_text))
    argv = _argv(command_text) if command_text and not compound else []
    runner = _test_runner(argv) if not compound else None
    command_name = runner or (_display_command(argv) if argv else None)
    exit_code = action.get("exit_code") if isinstance(action.get("exit_code"), int) else None
    kind = action.get("kind")
    timestamp = _time(action.get("timestamp"))
    source_ref = str(action.get("source_ref") or "")
    if kind == "commit":
        sha = action.get("sha") if isinstance(action.get("sha"), str) and _SHA.match(action.get("sha")) else ""
        return _classified("commit", "生成提交", "neutral", f"Commit · {sha[:7]}" if sha else "Commit", tool, None, None, None, timestamp, source_ref)
    if kind == "file_read":
        return _classified("file_read", "读取项目文件", "neutral", _file_detail("Read", path), tool or "Read", path, None, None, timestamp, source_ref)
    if kind == "file_edit":
        return _classified("file_edit", "修改代码", "info", _file_detail("Edit", path), tool or "Edit", path, None, None, timestamp, source_ref)
    if runner and exit_code == 0:
        return _classified("test_passed", "测试通过", "success", f"{runner} · exit code 0", tool, None, runner, exit_code, timestamp, source_ref)
    if runner and exit_code not in (None, 0):
        return _classified("test_failed", "测试失败", "error", f"{runner} · exit code {exit_code}", tool, None, runner, exit_code, timestamp, source_ref)
    status = "neutral"
    title = "执行命令" if command_name else "调用工具"
    if exit_code == 0:
        status = "success"
    elif isinstance(exit_code, int):
        status = "error"
        title = "命令失败" if command_name else "调用失败"
    elif action.get("is_error") is True:
        status = "error"
        title = "命令失败" if command_name else "调用失败"
    label = command_name or tool or "工具"
    detail = label
    if isinstance(exit_code, int):
        detail = f"{label} · exit code {exit_code}"
    return _classified("tool_call", title, status, detail, tool, path, command_name, exit_code, timestamp, source_ref)


def _classified(kind, title, status, detail, tool, path, command, exit_code, timestamp, source_ref):
    result = f"exit code {exit_code}" if isinstance(exit_code, int) else None
    return {
        "kind": kind,
        "title": title,
        "status": status,
        "detail": _clip(detail),
        "runner": command,
        "command": command,
        "tool": tool,
        "details": _details(
            tool=tool, path=path, command=command, result=result, exit_code=exit_code,
            timestamp=timestamp, source="native_session", source_ref=source_ref,
        ),
    }


def _verification_view(payload: Dict[str, Any], event_type: str):
    verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else payload
    if event_type == "verification_completed" and isinstance(payload.get("verification"), dict):
        passed = payload["verification"].get("passed")
        if passed is True:
            return "test_passed", "测试通过", "success", "验证通过", "验证通过"
        if passed is False:
            return "test_failed", "测试失败", "error", "验证未通过", "验证未通过"
        return "tool_call", "验证已记录", "unknown", "", None
    failing = verification.get("failing_count")
    if isinstance(failing, int) and failing > 0:
        return "test_failed", "测试失败", "error", f"失败 {failing}", f"失败 {failing}"
    # Equal pass/total counts are not a pass. Convergence and the
    # verification_completed receipt are the authoritative verdict.
    return "tool_call", "验证已记录", "unknown", "", None


def _row_evidence_id(row: Dict[str, Any]) -> Optional[str]:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else {}
    for value in (payload.get("evidence_id"), verification.get("evidence_id")):
        if isinstance(value, str) and value:
            return value
    return None


def _verification_verdicts(rows: Sequence[Dict[str, Any]]) -> Dict[str, bool]:
    verdicts = {}
    for row in rows:
        if str(row.get("event_type") or "") != "verification_completed":
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else None
        if not isinstance(verification, dict) or not isinstance(verification.get("passed"), bool):
            continue
        evidence_id = _row_evidence_id(row)
        if evidence_id:
            verdicts[evidence_id] = verification["passed"]
    return verdicts


def _tests_row_has_verdict(row: Dict[str, Any], verdicts: Dict[str, bool]) -> bool:
    if str(row.get("event_type") or "") != "tests_completed":
        return False
    evidence_id = _row_evidence_id(row)
    return bool(evidence_id and evidence_id in verdicts)


def _status_kind(status: Any):
    table = {
        "pending": ("task_created", "任务已创建", "neutral"),
        "dispatched": ("task_dispatched", "任务已派发", "info"),
        "working": ("agent_started", "开始执行", "info"),
        "paused": ("task_dispatched", "任务已暂停", "neutral"),
        "blocked": ("task_blocked", "任务已阻塞", "error"),
        "failed": ("task_blocked", "任务失败", "error"),
        "rework": ("agent_started", "继续执行", "info"),
        "agent_done": ("agent_started", "执行者已完成", "neutral"),
        "completed": ("task_completed", "任务完成", "info"),
        "committed": ("commit", "已提交", "neutral"),
        "integrated": ("task_completed", "已集成", "info"),
        "cleanup_ready": ("task_completed", "待归档", "neutral"),
        "cleaned": ("task_completed", "已完成", "neutral"),
    }
    return table.get(str(status or ""))


def _event(task, *, kind, title, status, timestamp, sequence, source, source_ref, agent=None,
           detail="", details=None, correlation_id=None, run_id=_MISSING):
    if details is None:
        details = _details(agent=agent, source=source, source_ref=source_ref, timestamp=timestamp)
    event = {
        "id": f"trace:{source}:{source_ref}",
        "task_id": task.get("task_id"),
        "run_id": task.get("run_id") if run_id is _MISSING else run_id,
        "timestamp": timestamp,
        "sequence": sequence if isinstance(sequence, int) else 0,
        "kind": kind,
        "title": title,
        "detail": _clip(detail),
        "status": status,
        "agent": agent or task.get("agent"),
        "source": source,
        "source_ref": source_ref,
        "details": details,
    }
    if correlation_id:
        event["correlation_id"] = correlation_id
    return event


def _public_event(event: Dict[str, Any]) -> Dict[str, Any]:
    public = {key: event.get(key) for key in _PUBLIC_FIELDS}
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    public["details"] = {key: details[key] for key in _DETAIL_FIELDS if key in details and details[key] is not None}
    return public


def _details(**fields):
    details = {}
    for key in _DETAIL_FIELDS:
        value = fields.get(key)
        if value is None or value == "":
            continue
        details[key] = value
    return details


def _sort_key(event: Dict[str, Any]):
    timestamp = event.get("timestamp")
    sequence = event.get("sequence") if isinstance(event.get("sequence"), int) else 0
    ident = str(event.get("id") or "")
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
        return (0, float(timestamp), sequence, ident)
    return (1, 0.0, sequence, ident)


def _commit_sha(task: Dict[str, Any]) -> Optional[str]:
    for key in ("commit", "integrated_commit"):
        value = task.get(key)
        if isinstance(value, str) and _SHA.match(value):
            return value
    result = task.get("commit_result")
    if isinstance(result, str) and _SHA.match(result):
        return result
    if isinstance(result, dict):
        for key in ("commit", "sha"):
            value = result.get(key)
            if isinstance(value, str) and _SHA.match(value):
                return value
    return None


def _argv(command: str) -> List[str]:
    try:
        return [item for item in shlex.split(command) if item]
    except ValueError:
        return []


def _test_runner(argv: Sequence[str]) -> Optional[str]:
    if not argv:
        return None
    base = os.path.basename(argv[0])
    if base in _TEST_BINARIES:
        return "pytest" if base.startswith("py") else base
    if base in {"python", "python3"} and len(argv) >= 3 and argv[1] == "-m" and argv[2] in _TEST_BINARIES:
        return "pytest"
    if base in {"npm", "pnpm", "yarn"} and len(argv) >= 2 and argv[1] == "test":
        return f"{base} test"
    if base == "go" and len(argv) >= 2 and argv[1] == "test":
        return "go test"
    if base == "cargo" and len(argv) >= 2 and argv[1] == "test":
        return "cargo test"
    return None


def _display_command(argv: Sequence[str]) -> Optional[str]:
    if not argv:
        return None
    base = os.path.basename(argv[0])
    if base in {"bash", "sh", "zsh"} and "-c" in argv:
        index = argv.index("-c")
        if index + 1 < len(argv) and not _SHELL_SPLIT.search(argv[index + 1]):
            inner = _argv(argv[index + 1])
            return _display_command(inner)
        return "shell"
    return _safe_token(base)


def _safe_token(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    token = value.strip()
    if not _TOKEN.match(token):
        return None
    return token


def _relativize_path(value: str, clone: Any) -> str:
    if not isinstance(clone, str) or not clone.strip():
        return value
    root = os.path.normpath(os.path.expanduser(clone.strip()))
    target = os.path.normpath(os.path.expanduser(value.strip()))
    try:
        rel = os.path.relpath(target, root)
    except ValueError:
        return value
    if rel == "." or rel.startswith(".."):
        return value
    return rel


def _safe_path(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.replace("\\", "/").strip()
    if not text or any(char in text for char in "<>\"'`\n\r"):
        return None
    name = os.path.basename(text)
    if _SECRET_NAME.search(text) or _SECRET_NAME.search(name):
        return None
    if text.startswith("/") or text.startswith("~"):
        name = name.strip()
        return name if name and name not in {".", ".."} and "/" not in name else None
    if text.startswith("../") or text == "..":
        return None
    if len(text) > 180:
        text = text[-180:]
    return text


def _file_detail(label: str, path: Optional[str]) -> str:
    return f"{label} · {path}" if path else label


def _time(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _clip(value: str) -> str:
    text = " ".join(str(value or "").split())
    return text[:160]


def _dedupe_notes(notes: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    result = []
    for note in notes:
        code = note.get("code")
        if code in seen:
            continue
        seen.add(code)
        result.append({"code": code, "message": note.get("message") or ""})
    return result
