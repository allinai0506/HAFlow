"""Console trajectory API: bounds, isolation, no writes, and one live chain."""

import hashlib
import json
import os
import socket
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from console import herdr_factory_console as console
from herdr.execution_trace import query_task_trace, read_trace_tasks
from herdr.state_db import get_readonly_db_connection
from herdr.trajectory import run_id_for_task


def _db(path: Path, tasks=()):
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
    conn.execute(
        """
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            workflow_id TEXT,
            node TEXT,
            stage TEXT,
            agent TEXT,
            status TEXT,
            stage_verdict TEXT,
            stage_verdict_note TEXT,
            pane_id TEXT,
            goal TEXT,
            blocker TEXT,
            payload_json TEXT,
            created_at REAL,
            updated_at REAL,
            version INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        INSERT INTO events (task_id, agent_id, event_type, payload_json, timestamp, source, run_id, sequence)
        VALUES ('task-a', 'codex', 'agent_started', '{"prompt":"SECRET_PROMPT"}', 20, 'trajectory', 'run-a', 1)
        """
    )
    for task in tasks:
        conn.execute(
            """
            INSERT INTO tasks (
                task_id, workflow_id, agent, status, created_at, version, payload_json
            ) VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (
                task.get("task_id"),
                task.get("workflow_id"),
                task.get("agent"),
                task.get("status"),
                task.get("created_at"),
                json.dumps(task),
            ),
        )
    conn.commit()
    conn.close()


def _task(task_id="task-a", run_id="run-a"):
    return {
        "task_id": task_id,
        "run_id": run_id,
        "workflow_id": "wf-a",
        "agent": "codex",
        "status": "cleaned",
        "created_at": 10,
        "clone_path": "",
    }


@pytest.fixture
def trace_api(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    _db(db, [_task(), _task("task-b", "run-b")])
    monkeypatch.setenv("HERDR_STATE_DB", str(db))
    monkeypatch.setattr(
        "herdr.state_db.get_db_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("writable connection")),
    )
    return db


def test_api_returns_task_trace_without_secrets_or_writes(trace_api):
    before = hashlib.sha256(trace_api.read_bytes()).hexdigest()
    conn = get_readonly_db_connection(trace_api)
    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()
    page = console.api_task_trajectory("task-a", "20", None)
    assert page["task_id"] == "task-a"
    assert page["run_id"] == "run-a"
    assert page["availability"] == "task_events"
    assert page["has_more"] is False
    assert any(event["kind"] == "agent_started" for event in page["events"])
    assert all(event["task_id"] == "task-a" for event in page["events"])
    assert "SECRET_PROMPT" not in json.dumps(page)
    conn = get_readonly_db_connection(trace_api)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == count
    conn.close()
    assert hashlib.sha256(trace_api.read_bytes()).hexdigest() == before


def test_api_rejects_missing_task_and_bad_limit(trace_api):
    with pytest.raises(console.TraceQueryError) as missing:
        console.api_task_trajectory("missing", 20, None)
    assert missing.value.status == 404
    with pytest.raises(console.TraceQueryError) as empty:
        console.api_task_trajectory("", 20, None)
    assert empty.value.status == 400
    with pytest.raises(console.TraceQueryError) as bad_limit:
        console.api_task_trajectory("task-a", "0", None)
    assert bad_limit.value.status == 400
    with pytest.raises(console.TraceQueryError) as bad_cursor:
        console.api_task_trajectory("task-a", 20, "no-such-cursor")
    assert bad_cursor.value.status == 400


def test_api_missing_database_does_not_create_one(tmp_path, monkeypatch):
    db = tmp_path / "missing.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(db))
    monkeypatch.setattr(
        "herdr.state_db.get_db_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("writable connection")),
    )
    with pytest.raises(console.TraceQueryError) as missing:
        console.api_task_trajectory("task-a")
    assert missing.value.status == 503
    assert missing.value.message == "任务轨迹数据库暂不可用。"
    assert not db.exists()


def test_trace_lookup_skips_unrelated_task_payloads(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    target = _task("task-a", "run-a")
    target["clone_path"] = str(clone)
    share = _task("task-share", "run-share")
    share["clone_path"] = str(clone) + "/"
    other = _task("task-other", "run-other")
    other["clone_path"] = str(tmp_path / "elsewhere")
    other["blob"] = "UNRELATED_PAYLOAD"
    db = tmp_path / "state.db"
    _db(db, [target, share, other])
    seen = []
    real_loads = json.loads

    def spy(text, *args, **kwargs):
        seen.append(text if isinstance(text, str) else "")
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr("herdr.execution_trace.json.loads", spy)
    records = read_trace_tasks(db, "task-a")
    assert {item["task_id"] for item in records} == {"task-a", "task-share"}
    assert all("UNRELATED_PAYLOAD" not in text for text in seen)
    page = query_task_trace(
        records[0], records, db_path=db,
        roots={"codex_sessions": tmp_path / "missing", "claude_projects": tmp_path / "missing", "opencode_db": tmp_path / "missing.db"},
    )
    assert "native_unverified" in {item["code"] for item in page["degradation"]}


def test_api_empty_task_reports_none(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    empty = {
        "task_id": "empty-task", "run_id": "run-empty", "workflow_id": "wf",
        "agent": "codex", "status": "pending",
    }
    _db(db, [empty])
    monkeypatch.setenv("HERDR_STATE_DB", str(db))
    page = console.api_task_trajectory("empty-task")
    assert page["availability"] == "none"
    assert page["events"] == []


def test_http_trajectory_route(trace_api):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = ThreadingHTTPServer(("127.0.0.1", port), console.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/task/trajectory?id=task-a&limit=10") as response:
            body = json.loads(response.read().decode())
        assert response.status == 200
        assert body["ok"] is True
        assert body["data"]["task_id"] == "task-a"
        assert "SECRET_PROMPT" not in json.dumps(body)
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/task/trajectory?id=missing")
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        assert caught.value.code == 404
        payload = json.loads(caught.value.read().decode())
        assert payload["ok"] is False
        assert "不存在" in payload["error"]
    finally:
        server.shutdown()
        server.server_close()


def _render(events):
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not found")
    html = console.HTML_TEMPLATE
    names = ["esc", "taskDrawerNumTs", "traceClock", "traceDotClass", "traceDetailRows", "renderExecutionTrace", "applyTraceResponse", "toggleTraceEvent"]
    parts = []
    for name in names:
        start = html.find(f"function {name}(")
        assert start >= 0, name
        brace = html.find("{", start)
        depth = 0
        end = None
        for index in range(brace, len(html)):
            if html[index] == "{":
                depth += 1
            elif html[index] == "}":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        parts.append(html[start:end])
    script = "\n".join(parts) + "\nconst html=renderExecutionTrace({status:'ready',availability:'agent_actions',events:" + json.dumps(events) + "});\nif(html.includes('<script>')||html.includes('SECRET')) process.exit(2);\nprocess.stdout.write(html);\n"
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return result.stdout


LOCAL_TASKS = Path.home() / ".herdr-controller" / "tasks.json"
LOCAL_DB = Path.home() / ".herdr-controller" / "state.db"


def _local_task(task_id):
    if not LOCAL_TASKS.is_file() or not LOCAL_DB.is_file():
        pytest.skip("local HAFlow state is absent")
    tasks = json.loads(LOCAL_TASKS.read_text(encoding="utf-8")).get("tasks") or []
    task = next((item for item in tasks if item.get("task_id") == task_id), None)
    if task is None:
        pytest.skip(f"{task_id} is absent")
    return tasks, task


def _count_task_events(task_id):
    conn = get_readonly_db_connection(LOCAL_DB)
    try:
        return conn.execute("SELECT COUNT(*) FROM events WHERE task_id=?", (task_id,)).fetchone()[0]
    finally:
        conn.close()


def test_live_product_chain_reads_real_sessions_without_writing():
    samples = [
        ("wf-haflow-1015-01-impl-collector-hardening", "codex"),
        ("wf-haflow-0923-02-impl-t2", "claude"),
        ("wf-nexusarchive-0917-01-requirements-challenger", "opencode"),
    ]
    available = []
    for task_id, agent in samples:
        if not LOCAL_TASKS.is_file():
            pytest.skip("local HAFlow state is absent")
        tasks = json.loads(LOCAL_TASKS.read_text(encoding="utf-8")).get("tasks") or []
        task = next((item for item in tasks if item.get("task_id") == task_id and item.get("agent") == agent), None)
        if task is not None:
            available.append((tasks, task))
    if not available:
        pytest.skip("no owned live tasks")
    proved_native = False
    for tasks, task in available:
        before = _count_task_events(task["task_id"])
        page = query_task_trace(task, tasks, db_path=LOCAL_DB, limit=200, budget_s=8)
        assert _count_task_events(task["task_id"]) == before
        assert page["task_id"] == task["task_id"]
        assert page["run_id"] == task.get("run_id")
        blob = json.dumps(page)
        assert "base_instructions" not in blob
        allowed = {"agent", "tool", "path", "command", "result", "exit_code", "timestamp", "source", "source_ref"}
        for event in page["events"]:
            assert event["task_id"] == task["task_id"]
            allowed_run = task.get("run_id") or run_id_for_task(task)
            assert event["run_id"] in (None, allowed_run)
            assert set(event.get("details") or {}).issubset(allowed)
            path = (event.get("details") or {}).get("path")
            if path:
                assert not str(path).startswith("/")
                assert ".." not in str(path)
            if event["kind"] == "test_passed":
                assert event["details"].get("exit_code") == 0
            if event["kind"] == "test_failed":
                assert isinstance(event["details"].get("exit_code"), int) and event["details"]["exit_code"] != 0
        native = [event for event in page["events"] if event["source"] == "native_session"]
        if native:
            proved_native = True
            if task["agent"] == "claude":
                assert any(event["kind"] == "file_read" for event in native)
                assert any(event["kind"] == "file_edit" for event in native)
            if task["agent"] == "codex":
                assert any(event["kind"] == "tool_call" for event in native)
                assert all(event["kind"] != "file_read" for event in native)
        rendered = _render(page["events"][:8])
        if page["events"]:
            assert page["events"][0]["title"] in rendered
    assert proved_native, "no ownership-proved native action was read"
