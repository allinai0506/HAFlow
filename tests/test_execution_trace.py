"""Execution trace projection, ownership, and native session parsers."""

import json
import sqlite3
from pathlib import Path

import pytest

from herdr.execution_trace import assemble_trace, project_native_actions, project_task_history, query_task_trace
from herdr.native_session_trace import load_native_actions, parse_claude_jsonl, parse_codex_jsonl, parse_opencode_parts


CLONE = "/tmp/haflow-trace-clone-a"
SECRET = "SECRETVALUE"


def task_doc(**overrides):
    data = {
        "task_id": "task-a",
        "run_id": "run-a",
        "workflow_id": "wf-a",
        "agent": "codex",
        "status": "completed",
        "created_at": 1_700_000_000.0,
        "started_at": 1_700_000_010.0,
        "clone_path": CLONE,
        "status_history": [
            {"to": "pending", "at": 1_700_000_000.0},
            {"to": "dispatched", "at": 1_700_000_005.0},
            {"to": "completed", "at": 1_700_000_100.0},
        ],
        "commit": "9d41c07abc1234",
        "changed_files": ["src/not-observed.py"],
    }
    data.update(overrides)
    return data


def _order(kinds, expected):
    index = 0
    for kind in kinds:
        if index < len(expected) and kind == expected[index]:
            index += 1
    assert index == len(expected), (kinds, expected)


def _codex_fixture():
    rows = [
        {"type": "session_meta", "payload": {"cwd": CLONE, "id": "nested-id", "session_id": "codex-sess"}},
        {"type": "response_item", "timestamp": 1_700_000_020, "payload": {
            "type": "function_call", "name": "read", "call_id": "r1",
            "arguments": json.dumps({"file_path": f"{CLONE}/src/billing.py"}),
        }},
        {"type": "response_item", "timestamp": 1_700_000_030, "payload": {
            "type": "function_call", "name": "apply_patch", "call_id": "e1",
            "arguments": json.dumps({"path": "src/billing.py"}),
        }},
        {"type": "response_item", "timestamp": 1_700_000_040, "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "c1",
            "input": 'text(await tools.exec_command({cmd:"pytest -q"}))',
        }},
        {"type": "response_item", "payload": {
            "type": "custom_tool_call_output", "call_id": "c1",
            "output": [{"type": "text", "text": 'prose "exit_code": 0\n{"exit_code": 1, "output": "SECRET_OUTPUT"}'}],
        }},
        {"type": "response_item", "timestamp": 1_700_000_050, "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "c2", "retry_of": "c1",
            "input": 'text(await tools.exec_command({cmd:"pytest -q"}))',
        }},
        {"type": "response_item", "payload": {
            "type": "custom_tool_call_output", "call_id": "c2",
            "output": [{"type": "text", "text": '{"exit_code": 0, "output": "ok"}'}],
        }},
        {"type": "response_item", "timestamp": 1_700_000_060, "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "c3",
            "input": 'text(await tools.exec_command({cmd:"pytest -q && echo done"}))',
        }},
        {"type": "response_item", "payload": {
            "type": "custom_tool_call_output", "call_id": "c3",
            "output": [{"type": "text", "text": '{"exit_code": 0}'}],
        }},
        {"type": "response_item", "timestamp": 1_700_000_070, "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "c4",
            "input": f'text(await tools.exec_command({{cmd:"pytest --token={SECRET} -q"}}))',
        }},
        {"type": "response_item", "payload": {
            "type": "custom_tool_call_output", "call_id": "c4",
            "output": [{"type": "text", "text": "pytest finished without a structured exit"}],
        }},
    ]
    return "\n".join(json.dumps(row) for row in rows)


def test_real_event_projection_orders_truthful_kinds():
    actions, notes = parse_codex_jsonl(_codex_fixture(), session_id="codex-sess")
    assert notes == []
    stored = [{
        "id": 7,
        "task_id": "task-a",
        "run_id": "run-a",
        "agent_id": "codex",
        "event_type": "initial_dispatched",
        "timestamp": 1_700_000_006,
        "source": "controller",
        "sequence": 1,
        "payload": {"prompt": "SECRET_PROMPT do the task", "status": "dispatched"},
    }]
    page = assemble_trace(task_doc(), stored, actions, limit=50)
    events = page["events"]
    blob = json.dumps(events, ensure_ascii=False)
    assert "SECRET_PROMPT" not in blob
    assert "SECRET_OUTPUT" not in blob
    assert SECRET not in blob
    assert "src/not-observed.py" not in blob
    assert "src/order.py" not in blob
    kinds = [event["kind"] for event in events]
    _order(kinds, [
        "task_created", "task_dispatched", "agent_started", "file_read", "file_edit",
        "test_failed", "retry", "test_passed", "task_completed", "commit",
    ])
    assert kinds.count("retry") == 1
    assert kinds.count("test_passed") == 1
    assert kinds.count("test_failed") == 1
    by_kind = {}
    for event in events:
        by_kind.setdefault(event["kind"], event)
    assert by_kind["file_read"]["title"] == "读取项目文件"
    assert by_kind["file_read"]["detail"] == "Read · src/billing.py"
    assert by_kind["file_read"]["status"] == "neutral"
    assert by_kind["file_read"]["source"] == "native_session"
    assert by_kind["file_edit"]["title"] == "修改代码"
    assert by_kind["file_edit"]["status"] == "info"
    assert by_kind["test_failed"]["status"] == "error"
    assert by_kind["test_failed"]["detail"] == "pytest · exit code 1"
    assert by_kind["test_passed"]["status"] == "success"
    assert by_kind["test_passed"]["detail"] == "pytest · exit code 0"
    assert by_kind["retry"]["title"] == "修复并再次测试"
    assert by_kind["retry"]["detail"] == "Retry · pytest"
    assert by_kind["commit"]["detail"] == "Commit · 9d41c07"
    assert by_kind["commit"]["timestamp"] is None
    assert by_kind["task_completed"]["status"] == "info"
    assert events[-1]["kind"] == "commit"
    compound = next(event for event in events if event["detail"] == "exec · exit code 0")
    assert compound["kind"] == "tool_call"
    assert compound["title"] != "测试通过"
    bare = next(event for event in events if event["kind"] == "tool_call" and event["status"] == "neutral")
    assert bare["title"] == "执行命令"
    assert "pytest" in bare["detail"]
    assert page["availability"] == "agent_actions"
    again = assemble_trace(task_doc(), stored, actions, limit=50)
    assert [event["id"] for event in again["events"]] == [event["id"] for event in events]


def test_newline_or_background_shell_is_not_a_test_pass():
    actions = [
        {"source_ref": "nl", "ordinal": 1, "timestamp": 1, "tool": "exec", "command": "pytest -q\necho done", "exit_code": 0},
        {"source_ref": "bg", "ordinal": 2, "timestamp": 2, "tool": "exec", "command": "pytest -q &", "exit_code": 0},
        {"source_ref": "redir", "ordinal": 3, "timestamp": 3, "tool": "exec", "command": "pytest -q 2>&1", "exit_code": 0},
    ]
    events = {event["source_ref"]: event for event in project_native_actions(actions, task_doc())}
    assert events["nl"]["kind"] == "tool_call"
    assert events["nl"]["title"] != "测试通过"
    assert events["bg"]["kind"] == "tool_call"
    assert events["bg"]["title"] != "测试通过"
    assert events["redir"]["kind"] == "test_passed"


def test_missing_time_is_not_filled_and_similar_commands_are_not_retries():
    actions, _ = parse_codex_jsonl(_codex_fixture(), session_id="codex-sess")
    retried = [item for item in actions if item.get("retry_of")]
    assert len(retried) == 1
    bare = task_doc(created_at=None, started_at=None, commit=None, commit_result="adopted", status_history=[
        {"to": "blocked", "at": None},
        {"to": "committed", "at": None},
    ])
    events = project_task_history(bare)
    assert events
    assert all(event["timestamp"] is None for event in events)
    assert {event["kind"] for event in events} == {"task_blocked", "task_completed"}
    assert all(event["kind"] != "commit" for event in events)


def test_verification_without_proof_stays_neutral():
    rows = [
        {"id": 1, "task_id": "task-a", "run_id": "run-a", "event_type": "verification_completed",
         "timestamp": 10, "source": "trajectory", "sequence": 1, "payload": {"verification": {}}},
        {"id": 2, "task_id": "task-a", "run_id": "run-a", "event_type": "tests_completed",
         "timestamp": 11, "source": "controller", "sequence": 2,
         "payload": {"command": "pytest", "output": "all good SECRET_OUTPUT"}},
        {"id": 3, "task_id": "task-b", "run_id": "run-b", "event_type": "task_completed",
         "timestamp": 12, "source": "trajectory", "sequence": 3, "payload": {}},
        {"id": 4, "task_id": "task-a", "run_id": "run-other", "event_type": "agent_started",
         "timestamp": 9, "source": "trajectory", "sequence": 4, "payload": {}},
    ]
    page = assemble_trace(task_doc(commit=None, status_history=[], created_at=None, started_at=None), rows, [])
    kinds = [event["kind"] for event in page["events"]]
    assert "test_passed" not in kinds
    assert "test_failed" not in kinds
    assert kinds == ["tool_call", "tool_call"]
    assert all(event["status"] == "unknown" for event in page["events"])
    assert "SECRET_OUTPUT" not in json.dumps(page["events"])
    assert all(event["task_id"] == "task-a" and event["run_id"] == "run-a" for event in page["events"])


def test_same_source_dedupes_but_similar_titles_do_not():
    row = {
        "id": 5, "task_id": "task-a", "run_id": "run-a", "event_type": "task_completed",
        "timestamp": 50, "source": "trajectory", "sequence": 2, "payload": {},
    }
    page = assemble_trace(task_doc(commit=None), [row, dict(row)], [])
    completed = [event for event in page["events"] if event["kind"] == "task_completed"]
    assert len(completed) == 2
    assert len({event["source_ref"] for event in completed}) == 2


def _events_db(path: Path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id TEXT, node_id TEXT, task_id TEXT, agent_id TEXT,
            event_type TEXT, payload_json TEXT, timestamp REAL,
            source TEXT, run_id TEXT, sequence INTEGER
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO events (task_id, agent_id, event_type, payload_json, timestamp, source, run_id, sequence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("task-a", "codex", "agent_started", "{}", 1_700_000_011, "trajectory", "run-a", 1),
            ("task-b", "claude", "agent_started", "{}", 1_700_000_012, "trajectory", "run-b", 1),
            ("task-a", "codex", "task_completed", "{}", 1_700_000_200, "trajectory", "run-b", 2),
        ],
    )
    conn.commit()
    conn.close()


def test_query_isolates_task_and_run_without_writable_connection(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _events_db(db)
    monkeypatch.setattr(
        "herdr.state_db.get_db_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("writable connection")),
    )
    before = db.read_bytes()
    page = query_task_trace(
        task_doc(commit=None, pane_id=None),
        [task_doc(), task_doc(task_id="task-b", run_id="run-b", pane_id="w1", clone_path="/tmp/haflow-trace-clone-b")],
        db_path=db,
        roots={"codex_sessions": tmp_path / "missing", "claude_projects": tmp_path / "missing", "opencode_db": tmp_path / "missing.db"},
        limit=20,
    )
    assert db.read_bytes() == before
    assert page["task_id"] == "task-a"
    assert page["run_id"] == "run-a"
    assert any(event["kind"] == "agent_started" and event["source"] == "trajectory" for event in page["events"])
    assert all(event["task_id"] == "task-a" and event["run_id"] == "run-a" for event in page["events"])
    assert page["degraded"] is True
    assert "native_unavailable" in {item["code"] for item in page["degradation"]}


def test_pagination_is_stable_and_rejects_bad_windows():
    actions, _ = parse_codex_jsonl(_codex_fixture(), session_id="codex-sess")
    first = assemble_trace(task_doc(), [], actions, limit=2)
    assert first["has_more"] is True
    assert len(first["events"]) == 2
    second = assemble_trace(task_doc(), [], actions, limit=2, cursor=first["next_cursor"])
    assert first["events"][0]["id"] not in {event["id"] for event in second["events"]}
    assert second["events"][0]["id"] != first["events"][1]["id"]
    again = assemble_trace(task_doc(), [], actions, limit=2, cursor=first["next_cursor"])
    assert [event["id"] for event in again["events"]] == [event["id"] for event in second["events"]]
    with pytest.raises(ValueError):
        assemble_trace(task_doc(), [], actions, limit=0)
    with pytest.raises(ValueError):
        assemble_trace(task_doc(), [], actions, limit=201)
    with pytest.raises(ValueError):
        assemble_trace(task_doc(), [], actions, cursor="missing")


def _write_codex(root: Path, name: str, cwd: str, session_id: str, command: str = "pwd"):
    root.mkdir(parents=True, exist_ok=True)
    text = "\n".join([
        json.dumps({"type": "session_meta", "payload": {"cwd": cwd, "session_id": session_id, "id": "other"}}),
        json.dumps({"type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "c1",
            "input": f'text(await tools.exec_command({{cmd:"{command}"}}))',
        }}),
        json.dumps({"type": "response_item", "payload": {
            "type": "custom_tool_call_output", "call_id": "c1",
            "output": [{"type": "text", "text": '{"exit_code": 0}'}],
        }}),
    ])
    (root / name).write_text(text, encoding="utf-8")


def test_native_ownership_uses_clone_and_recorded_id_not_pane_or_agent_session(tmp_path):
    clone = tmp_path / "clone-a"
    clone.mkdir()
    other = tmp_path / "clone-b"
    other.mkdir()
    sessions = tmp_path / "codex"
    _write_codex(sessions, "owned.jsonl", str(clone), "native-1")
    _write_codex(sessions, "other.jsonl", str(other), "agent-session-same")
    roots = {"codex_sessions": sessions, "claude_projects": tmp_path / "claude", "opencode_db": tmp_path / "none.db"}
    task = task_doc(clone_path=str(clone), agent_session_id="agent-session-same", pane_id="w1", runtime={"pane_id": "w1"})
    actions, notes = load_native_actions(task, [task], roots)
    assert [item["session_id"] for item in actions] == ["native-1"]
    assert notes == []

    shared = task_doc(task_id="task-b", clone_path=str(clone), run_id="run-b", pane_id="w1")
    refused, refused_notes = load_native_actions(task, [task, shared], roots)
    assert refused == []
    assert refused_notes[0]["code"] == "native_unverified"

    pinned = task_doc(clone_path=str(clone), native_session_id="native-9")
    missed, missed_notes = load_native_actions(pinned, [pinned], roots)
    assert missed == []
    assert missed_notes[0]["code"] == "native_unverified"
    pinned["native_session_id"] = "native-1"
    matched, matched_notes = load_native_actions(pinned, [pinned, shared], roots)
    assert [item["session_id"] for item in matched] == ["native-1"]
    assert matched_notes == []


def test_claude_parser_reads_edits_exits_and_skips_corrupt_lines():
    clone = "/tmp/haflow-trace-clone-c"
    text = "\n".join([
        "{not-json",
        json.dumps({
            "cwd": clone, "sessionId": "claude-1", "timestamp": "2026-10-16T02:21:03Z",
            "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": f"{clone}/src/billing.py"}}]},
        }),
        json.dumps({"message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "SECRET_FILE_BODY"}]}}),
        json.dumps({"timestamp": "2026-10-16T02:21:16Z", "message": {"content": [
            {"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": f"{clone}/.env"}},
        ]}}),
        json.dumps({"timestamp": "2026-10-16T02:21:42Z", "message": {"content": [
            {"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "pytest -q"}},
        ]}}),
        json.dumps({"message": {"content": [{"type": "tool_result", "tool_use_id": "t3", "content": "Exit code 1\nFAILED", "is_error": True}]}}),
        json.dumps({"timestamp": "2026-10-16T02:22:08Z", "message": {"content": [
            {"type": "tool_use", "id": "t4", "name": "Bash", "input": {"command": "pytest -q", "retry_of": "t3"}},
        ]}}),
        json.dumps({"message": {"content": [{"type": "tool_result", "tool_use_id": "t4", "content": [{"type": "text", "text": "Exit code 0"}]}]}}),
        json.dumps({"timestamp": "2026-10-16T02:22:20Z", "message": {"content": [
            {"type": "tool_use", "id": "t5", "name": "Bash", "input": {"command": "pytest -q"}},
        ]}}),
        json.dumps({"message": {"content": [{"type": "tool_result", "tool_use_id": "t5", "content": "boom", "is_error": True}]}}),
    ])
    actions, notes = parse_claude_jsonl(text, session_id="claude-1")
    assert notes == []
    page = assemble_trace(task_doc(agent="claude", clone_path=clone, commit=None, status_history=[], created_at=None, started_at=None), [], actions)
    blob = json.dumps(page["events"], ensure_ascii=False)
    assert "SECRET_FILE_BODY" not in blob
    assert ".env" not in blob
    kinds = [event["kind"] for event in page["events"]]
    assert kinds[:5] == ["file_read", "file_edit", "test_failed", "retry", "test_passed"]
    failed_command = next(event for event in page["events"] if event["kind"] == "tool_call")
    assert failed_command["status"] == "error"
    assert failed_command["title"] != "测试失败"
    read = page["events"][0]
    assert read["detail"] == "Read · src/billing.py"
    assert read["timestamp"] is not None


def test_opencode_exit_and_retry_are_structured_only():
    parts = [
        {"id": "p1", "time_created": 1_700_000_300, "data": json.dumps({
            "type": "tool", "tool": "read",
            "state": {"status": "completed", "input": {"filePath": "src/billing.py"}},
        })},
        {"id": "p2", "time_created": 1_700_000_310, "data": json.dumps({
            "type": "tool", "tool": "bash",
            "state": {"status": "completed", "input": {"command": "pytest -q"}, "metadata": {"exit": 1}},
        })},
        {"id": "p3", "time_created": 1_700_000_320, "data": json.dumps({
            "type": "tool", "tool": "bash", "retry_of": "p2",
            "state": {"status": "completed", "input": {"command": "pytest -q"}, "metadata": {"exit": 0}},
        })},
        {"id": "p4", "time_created": 1_700_000_330, "data": json.dumps({
            "type": "tool", "tool": "bash",
            "state": {"status": "completed", "input": {"command": "pytest -q"}, "metadata": {"exit": "0"}},
        })},
        {"id": "bad", "data": "{"},
    ]
    actions, notes = parse_opencode_parts(parts, session_id="oc-1")
    assert notes == []
    page = assemble_trace(task_doc(agent="opencode", commit=None, status_history=[], created_at=None, started_at=None), [], actions)
    kinds = [event["kind"] for event in page["events"]]
    assert kinds == ["file_read", "test_failed", "retry", "test_passed", "tool_call"]
    assert page["events"][-1]["status"] == "neutral"
    assert page["events"][-1]["title"] != "测试通过"


def test_loader_degrades_on_corrupt_missing_and_unmatched_sessions(tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    claude_root = tmp_path / "claude"
    import os
    encoded = "".join(ch if ch.isalnum() else "-" for ch in os.path.abspath(str(clone)))
    folder = claude_root / encoded
    folder.mkdir(parents=True)
    (folder / "broken.jsonl").write_text("{bad\n", encoding="utf-8")
    task = task_doc(agent="claude", clone_path=str(clone), commit=None)
    roots = {"codex_sessions": tmp_path / "codex", "claude_projects": claude_root, "opencode_db": tmp_path / "opencode.db"}
    actions, notes = load_native_actions(task, [task], roots)
    assert actions == []
    assert notes[0]["code"] == "native_corrupt"

    (folder / "broken.jsonl").unlink()
    (folder / "other.jsonl").write_text(json.dumps({
        "cwd": str(tmp_path / "elsewhere"), "sessionId": "nope",
        "message": {"content": [{"type": "tool_use", "id": "t", "name": "Read", "input": {"file_path": "a.py"}}]},
    }), encoding="utf-8")
    actions, notes = load_native_actions(task, [task], roots)
    assert actions == []
    assert notes[0]["code"] == "native_unverified"

    missing = task_doc(agent="opencode", clone_path=str(clone))
    actions, notes = load_native_actions(missing, [missing], {"opencode_db": tmp_path / "absent.db"})
    assert actions == []
    assert notes[0]["code"] == "native_unavailable"
    (tmp_path / "bad.db").write_text("not a database", encoding="utf-8")
    actions, notes = load_native_actions(missing, [missing], {"opencode_db": tmp_path / "bad.db"})
    assert actions == []
    assert notes[0]["code"] in {"native_corrupt", "native_unavailable"}


def test_opencode_loader_matches_directory_exactly(tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT)")
    conn.execute("CREATE TABLE part (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT)")
    conn.execute("INSERT INTO session VALUES (?, ?)", ("keep", str(clone)))
    conn.execute("INSERT INTO session VALUES (?, ?)", ("drop", str(tmp_path / "other")))
    conn.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?)",
        ("part-1", "keep", 10, json.dumps({"type": "tool", "tool": "read", "state": {"status": "completed", "input": {"filePath": "src/billing.py"}}})),
    )
    conn.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?)",
        ("part-2", "drop", 11, json.dumps({"type": "tool", "tool": "read", "state": {"input": {"filePath": "secret.py"}}})),
    )
    conn.commit()
    conn.close()
    task = task_doc(agent="opencode", clone_path=str(clone), commit=None)
    actions, notes = load_native_actions(task, [task], {"opencode_db": db})
    assert notes == []
    assert len(actions) == 1
    assert actions[0]["session_id"] == "keep"
    assert actions[0]["path"] == "src/billing.py"


def test_unscoped_ledger_row_keeps_an_unknown_run():
    row = {
        "id": 9, "task_id": "task-a", "run_id": None, "event_type": "agent_started",
        "timestamp": 30, "source": "trajectory", "sequence": 1, "payload": {},
    }
    page = assemble_trace(task_doc(commit=None, status_history=[], created_at=None, started_at=None), [row], [])
    assert page["run_id"] == "run-a"
    assert len(page["events"]) == 1
    assert page["events"][0]["run_id"] is None
    assert page["events"][0]["task_id"] == "task-a"


def test_transition_task_rows_keep_status_and_time(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    monkeypatch.setattr("herdr.state_db.time.time", lambda: 1_700_000_050.0)
    from herdr.execution_trace import project_stored_event, read_stored_events, read_task_records
    from herdr.state_db import save_task, transition_task

    save_task({
        "task_id": "task-a",
        "workflow_id": "wf-a",
        "run_id": "run-a",
        "agent": "codex",
        "status": "pending",
        "node": "dev",
        "created_at": 1_700_000_000.0,
    }, db_path=db)
    result = transition_task("task-a", "dispatched", "dispatch", source="controller", db_path=db)
    assert result["accepted"] is True
    task = next(item for item in read_task_records(db) if item["task_id"] == "task-a")
    history = project_task_history(task)
    history_dispatch = next(event for event in history if event["kind"] == "task_dispatched")
    assert history_dispatch["timestamp"] == 1_700_000_050.0
    stored, notes = read_stored_events(task, db)
    assert notes == []
    ledger = [project_stored_event(row, task) for row in stored]
    ledger = [event for event in ledger if event and event["kind"] == "task_dispatched"]
    assert len(ledger) == 1
    assert ledger[0]["timestamp"] == 1_700_000_050.0
    assert ledger[0]["run_id"] == "run-a"
    assert history_dispatch["correlation_id"] == ledger[0]["correlation_id"]
    native = [{
        "source_ref": "native:1", "ordinal": 1, "timestamp": 1_700_000_100,
        "tool": "exec", "command": "ls",
    }]
    page = assemble_trace(task, stored, native, limit=50)
    assert sum(event["kind"] == "task_dispatched" for event in page["events"]) == 1
    timed = [event["kind"] for event in page["events"] if event["timestamp"] is not None]
    assert timed.index("task_dispatched") < timed.index("tool_call")


def test_opencode_loader_uses_part_id_for_retry(tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT)")
    conn.execute("CREATE TABLE part (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT)")
    conn.execute("INSERT INTO session VALUES (?, ?)", ("keep", str(clone)))
    conn.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?)",
        ("p1", "keep", 10, json.dumps({
            "type": "tool", "tool": "bash",
            "state": {"status": "completed", "input": {"command": "pytest -q"}, "metadata": {"exit": 1}},
        })),
    )
    conn.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?)",
        ("p2", "keep", 11, json.dumps({
            "type": "tool", "tool": "bash", "retry_of": "p1",
            "state": {"status": "completed", "input": {"command": "pytest -q"}, "metadata": {"exit": 0}},
        })),
    )
    conn.commit()
    conn.close()
    task = task_doc(agent="opencode", clone_path=str(clone), commit=None, status_history=[], created_at=None, started_at=None)
    actions, notes = load_native_actions(task, [task], {"opencode_db": db})
    assert notes == []
    assert actions[1]["retry_of"] == actions[0]["source_ref"]
    page = assemble_trace(task, [], actions, limit=20)
    assert [event["kind"] for event in page["events"]] == ["test_failed", "retry", "test_passed"]


def test_codex_scan_reaches_the_newest_session_beyond_the_cap(tmp_path):
    from herdr.native_session_trace import MAX_SCAN

    root = tmp_path / "sessions"
    old = root / "2020" / "01" / "01"
    new = root / "2026" / "10" / "16"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    clone = tmp_path / "clone"
    clone.mkdir()
    filler = json.dumps({"type": "session_meta", "payload": {"cwd": "/other", "session_id": "old"}}) + "\n"
    for index in range(MAX_SCAN):
        (old / f"session-{index:04d}.jsonl").write_text(filler, encoding="utf-8")
    owned = "\n".join([
        json.dumps({"type": "session_meta", "payload": {"cwd": str(clone), "session_id": "new-session"}}),
        json.dumps({
            "type": "response_item", "timestamp": 1_700_000_020,
            "payload": {
                "type": "function_call", "name": "read", "call_id": "r1",
                "arguments": json.dumps({"file_path": "src/billing.py"}),
            },
        }),
    ]) + "\n"
    (new / "session-last.jsonl").write_text(owned, encoding="utf-8")
    task = task_doc(agent="codex", clone_path=str(clone), commit=None, status_history=[], created_at=None, started_at=None)
    actions, notes = load_native_actions(
        task, [task],
        {"codex_sessions": root, "claude_projects": tmp_path / "missing-claude", "opencode_db": tmp_path / "none.db"},
        budget_s=8,
    )
    assert any(item.get("session_id") == "new-session" for item in actions)
    assert all(note["code"] != "native_unverified" for note in notes)


def test_stale_launch_identity_run_does_not_bind_native_actions(tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    root = tmp_path / "codex" / "2026" / "10" / "16"
    root.mkdir(parents=True)
    session = "\n".join([
        json.dumps({"type": "session_meta", "payload": {"cwd": str(clone), "session_id": "pinned-session"}}),
        json.dumps({"type": "response_item", "timestamp": 20, "payload": {
            "type": "function_call", "name": "read", "call_id": "r1",
            "arguments": json.dumps({"file_path": "src/billing.py"}),
        }}),
    ]) + "\n"
    (root / "session.jsonl").write_text(session, encoding="utf-8")
    identity = clone / ".herdr-launch-identity.json"
    identity.write_text(json.dumps({
        "task_id": "task-a", "run_id": "run-old", "native_session_id": "pinned-session",
    }), encoding="utf-8")
    task = task_doc(
        agent="codex", clone_path=str(clone), run_id="run-new",
        commit=None, status_history=[], created_at=None, started_at=None,
    )
    roots = {"codex_sessions": tmp_path / "codex", "claude_projects": tmp_path / "claude", "opencode_db": tmp_path / "none.db"}
    actions, notes = load_native_actions(task, [task], roots)
    assert actions == []
    assert notes[0]["code"] == "native_unverified"
    assert "没有可核对" in notes[0]["message"]
    identity.write_text(json.dumps({
        "task_id": "task-a", "run_id": "run-new", "native_session_id": "pinned-session",
    }), encoding="utf-8")
    actions, notes = load_native_actions(task, [task], roots)
    assert any(item.get("session_id") == "pinned-session" for item in actions)
    assert notes == []


def test_claude_pinned_session_survives_the_file_cap(tmp_path):
    from herdr.native_session_trace import MAX_FILES

    clone = tmp_path / "clone"
    clone.mkdir()
    import os
    encoded = "".join(ch if ch.isalnum() else "-" for ch in os.path.abspath(str(clone)))
    folder = tmp_path / "claude" / encoded
    folder.mkdir(parents=True)
    pinned = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    for index in range(MAX_FILES):
        name = f"00000000-0000-0000-0000-{index:012d}.jsonl"
        (folder / name).write_text(json.dumps({
            "cwd": str(clone), "sessionId": f"other-{index}",
            "message": {"content": [{"type": "tool_use", "id": "t", "name": "Read", "input": {"file_path": "other.py"}}]},
        }) + "\n", encoding="utf-8")
    (folder / f"{pinned}.jsonl").write_text(json.dumps({
        "cwd": str(clone), "sessionId": pinned,
        "message": {"content": [{"type": "tool_use", "id": "t", "name": "Read", "input": {"file_path": "src/billing.py"}}]},
    }) + "\n", encoding="utf-8")
    task = task_doc(
        agent="claude", clone_path=str(clone), native_session_id=pinned,
        commit=None, status_history=[], created_at=None, started_at=None,
    )
    actions, notes = load_native_actions(
        task, [task],
        {"codex_sessions": tmp_path / "codex", "claude_projects": tmp_path / "claude", "opencode_db": tmp_path / "none.db"},
    )
    assert any(item.get("session_id") == pinned and item.get("path") == "src/billing.py" for item in actions)
    assert all(item.get("session_id") == pinned for item in actions)
    assert any(note["code"] == "native_truncated" for note in notes)


def test_stored_event_cap_keeps_the_current_run_row(tmp_path):
    from herdr.execution_trace import STORED_LIMIT, read_stored_events

    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id TEXT, node_id TEXT, task_id TEXT, agent_id TEXT,
            event_type TEXT, payload_json TEXT, timestamp REAL,
            source TEXT, run_id TEXT, sequence INTEGER
        )
        """
    )
    stale = [("task-a", "codex", "agent_started", "{}", index, "trajectory", "run-old", index) for index in range(STORED_LIMIT)]
    noise = [("task-a", "codex", "debug_log", "{}", 10_000 + index, "trajectory", "run-a", index) for index in range(STORED_LIMIT)]
    conn.executemany(
        """
        INSERT INTO events (task_id, agent_id, event_type, payload_json, timestamp, source, run_id, sequence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        stale + noise + [("task-a", "codex", "task_transition", json.dumps({"to_status": "dispatched"}), 20_000, "controller", "run-a", 1)],
    )
    conn.commit()
    conn.close()
    rows, notes = read_stored_events(task_doc(commit=None), db)
    assert any(row["event_type"] == "task_transition" and row["run_id"] == "run-a" for row in rows)
    assert all(row["run_id"] in (None, "", "run-a") for row in rows)
    assert all(row["event_type"] != "debug_log" for row in rows)
    assert notes == []


def test_runless_task_keeps_the_synthetic_trajectory_run(tmp_path):
    from herdr.execution_trace import read_stored_events, read_task_records
    from herdr.state_db import save_task
    from herdr.trajectory import TrajectoryLedger, record_trajectory_event, run_id_for_task

    db = tmp_path / "state.db"
    task = {
        "task_id": "task-legacy",
        "workflow_id": "wf-a",
        "agent": "codex",
        "status": "working",
        "node": "dev",
        "created_at": 10.0,
    }
    save_task(task, db_path=db)
    record_trajectory_event(task, "task_started", ledger=TrajectoryLedger(db))
    conn = sqlite3.connect(db)
    conn.executemany(
        """
        INSERT INTO events (task_id, agent_id, event_type, payload_json, timestamp, source, run_id, sequence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("task-legacy", "codex", "agent_started", "{}", 11, "trajectory", None, 2),
            ("task-legacy", "codex", "task_completed", "{}", 12, "trajectory", "run-other", 3),
        ],
    )
    conn.commit()
    conn.close()
    loaded = next(item for item in read_task_records(db) if item["task_id"] == "task-legacy")
    assert not loaded.get("run_id")
    rows, notes = read_stored_events(loaded, db)
    synthetic = run_id_for_task(task)
    assert notes == []
    assert any(row["event_type"] == "task_started" and row["run_id"] == synthetic for row in rows)
    assert any(row["event_type"] == "agent_started" and row["run_id"] in (None, "") for row in rows)
    assert all(row["run_id"] != "run-other" for row in rows)
    page = assemble_trace(loaded, rows, [])
    assert page["run_id"] in (None, "")
    trajectory = [event for event in page["events"] if event["source"] == "trajectory"]
    assert any(event["run_id"] == synthetic for event in trajectory)
    assert any(event["run_id"] is None for event in trajectory)


def test_codex_pinned_session_survives_the_scan_cap(tmp_path):
    from herdr.native_session_trace import MAX_SCAN

    root = tmp_path / "sessions"
    new = root / "2026" / "10" / "16"
    old = root / "2020" / "01" / "01"
    new.mkdir(parents=True)
    old.mkdir(parents=True)
    clone = tmp_path / "clone"
    clone.mkdir()
    filler = json.dumps({"type": "session_meta", "payload": {"cwd": "/other", "session_id": "newer"}}) + "\n"
    for index in range(MAX_SCAN):
        (new / f"session-{index:04d}.jsonl").write_text(filler, encoding="utf-8")
    pinned = "pinned-session"
    (old / "session-old.jsonl").write_text("\n".join([
        json.dumps({"type": "session_meta", "payload": {"cwd": str(clone), "session_id": pinned}}),
        json.dumps({
            "type": "response_item", "timestamp": 20,
            "payload": {
                "type": "function_call", "name": "read", "call_id": "r1",
                "arguments": json.dumps({"file_path": "src/billing.py"}),
            },
        }),
    ]) + "\n", encoding="utf-8")
    task = task_doc(
        agent="codex", clone_path=str(clone), native_session_id=pinned,
        commit=None, status_history=[], created_at=None, started_at=None,
    )
    actions, _notes = load_native_actions(
        task, [task],
        {"codex_sessions": root, "claude_projects": tmp_path / "missing-claude", "opencode_db": tmp_path / "none.db"},
        budget_s=8,
    )
    assert any(item.get("session_id") == pinned and item.get("path") == "src/billing.py" for item in actions)
    assert all(item.get("session_id") == pinned for item in actions)
