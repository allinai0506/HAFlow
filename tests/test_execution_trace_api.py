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
        allowed = {"agent", "tool", "path", "command", "result", "exit_code", "timestamp", "source", "source_ref", "observation_id", "sha256"}
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


def test_ledger_reports_dispatch_artifacts_and_candidate_without_native_session(tmp_path, monkeypatch):
    task = _task()
    task.update(agent='qodercli', goal='核验候选版本的启动信任与回滚', node='cleanroom_test')
    path = tmp_path / 'state.db'
    _db(path, [task])
    payloads = [
        ('initial_dispatched', {'prompt': 'PRIVATE_PROMPT', 'identity_path': '/private/credential'}),
        ('task_artifact_checkpoint', {'step': 2, 'summary': '已执行174项回归；password=SECRET123',
            'next_step': '核验回滚', 'observation_id': 'obs-report', 'sha256': 'a' * 64,
            'size_bytes': 1173, 'content': 'PRIVATE_CONTENT'}),
        ('candidate_published', {'candidate_sha': 'b' * 40}),
        ('tool_execution', {'status': 'timeout', 'exit_code': None, 'side_effects': 'unknown',
            'observation_id': 'obs-tool', 'sha256': 'c' * 64}),
    ]
    conn = sqlite3.connect(path)
    conn.execute('UPDATE tasks SET goal=? WHERE task_id=?', (task['goal'], 'task-a'))
    for i, (kind, payload) in enumerate(payloads, 30):
        conn.execute('INSERT INTO events (task_id,event_type,payload_json,timestamp,source,run_id) VALUES (?,?,?,?,?,?)',
            ('task-a', kind, json.dumps(payload), i, 'checkpoint', 'run-a'))
    conn.execute('INSERT INTO events (task_id,event_type,payload_json,timestamp,run_id) VALUES (?,?,?,?,?)',
        ('task-a', 'task_artifact_checkpoint', '{"summary":"WRONG_RUN"}', 50, 'other-run'))
    conn.commit()
    conn.close()
    monkeypatch.setattr(console.read_api, 'state_db_path', lambda: path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = console.api_task_trajectory('task-a')
    encoded = json.dumps(result, ensure_ascii=False)
    assert '核验候选版本的启动信任与回滚' in encoded
    assert '已执行174项回归' in encoded
    assert '核验回滚' in encoded
    assert 'obs-report' in encoded and 'a' * 64 in encoded
    assert 'b' * 40 in encoded
    assert 'timeout' in encoded and 'unknown' in encoded
    for secret in ('PRIVATE_PROMPT', 'PRIVATE_CONTENT', '/private/credential', 'SECRET123', 'WRONG_RUN'):
        assert secret not in encoded
    report = next(e for e in result['events'] if e['kind'] == 'progress_report')
    assert report['status'] == 'neutral'  # Agent report is not an acceptance verdict.
    assert result['availability'] == 'recorded_actions'
    assert any(n['code'] == 'native_unsupported' for n in result['degradation'])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_issue261_api_surfaces_execution_receipts_past_the_stored_window(tmp_path, monkeypatch):
    """Issue #261: receipts written after the 2000-row window must reach the
    real trajectory API, scoped to their own task/run only."""
    from herdr.execution_trace import STORED_LIMIT

    task = _task()
    db = tmp_path / "state.db"
    _db(db, [task, _task("task-b", "run-b")])
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO events (task_id,agent_id,event_type,payload_json,timestamp,source,run_id,sequence)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [("task-a", "codex", "task_started", "{}", 10 + i, "trajectory", "run-a", i + 2)
         for i in range(STORED_LIMIT)],
    )
    conn.executemany(
        "INSERT INTO events (task_id,agent_id,event_type,payload_json,timestamp,source,run_id,sequence)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [
            ("task-a", "codex", "task_artifact_checkpoint",
             json.dumps({"summary": "TAIL_REPORT", "step": 9}), 100_001, "checkpoint", "run-a", 5_001),
            ("task-a", "codex", "candidate_published",
             json.dumps({"candidate_sha": "b" * 40}), 100_002, "checkpoint", "run-a", 5_002),
            ("task-a", "codex", "tool_execution",
             json.dumps({"status": "ok", "exit_code": 0}), 100_003, "checkpoint", "run-a", 5_003),
            # Same task, different run: must stay out of this run's trace.
            ("task-a", "codex", "task_artifact_checkpoint",
             json.dumps({"summary": "OTHER_RUN_REPORT"}), 100_004, "checkpoint", "run-other", 5_004),
            # Other task in the same run scope: must not leak in.
            ("task-b", "codex", "task_artifact_checkpoint",
             json.dumps({"summary": "OTHER_TASK_REPORT"}), 100_005, "checkpoint", "run-a", 5_005),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("HERDR_STATE_DB", str(db))

    kinds = set()
    cursor = None
    for _ in range(50):
        page = console.api_task_trajectory("task-a", "200", cursor)
        kinds.update(event["kind"] for event in page["events"])
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    assert {"progress_report", "candidate", "tool_receipt"} <= kinds
    encoded = json.dumps(page, ensure_ascii=False)
    assert "TAIL_REPORT" in encoded
    assert "OTHER_RUN_REPORT" not in encoded
    assert "OTHER_TASK_REPORT" not in encoded


def test_issue261_api_surfaces_latest_receipt_of_each_type(tmp_path, monkeypatch):
    """Issue #261 P2: a saturated checkpoint type must not hide the later
    publication and tool receipt in the real Console trajectory API."""
    from herdr.execution_trace import STORED_LIMIT

    task = _task()
    db = tmp_path / "state.db"
    _db(db, [task])
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO events (task_id,agent_id,event_type,payload_json,timestamp,source,run_id,sequence)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [("task-a", "codex", "task_started", "{}", 10 + i, "trajectory", "run-a", i + 2)
         for i in range(STORED_LIMIT)],
    )
    conn.executemany(
        "INSERT INTO events (task_id,agent_id,event_type,payload_json,timestamp,source,run_id,sequence)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [("task-a", "codex", "task_artifact_checkpoint",
          json.dumps({"summary": f"checkpoint {i}", "step": i}),
          10_000 + i, "checkpoint", "run-a", 3_000 + i)
         for i in range(STORED_LIMIT)],
    )
    conn.executemany(
        "INSERT INTO events (task_id,agent_id,event_type,payload_json,timestamp,source,run_id,sequence)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [
            ("task-a", "codex", "candidate_published",
             json.dumps({"candidate_sha": "d" * 40}), 100_000, "herdr-task", "run-a", 9_000),
            ("task-a", "codex", "tool_execution",
             json.dumps({"status": "ok", "exit_code": 0}), 100_001, "checkpoint", "run-a", 9_001),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("HERDR_STATE_DB", str(db))

    kinds = set()
    cursor = None
    for _ in range(80):
        page = console.api_task_trajectory("task-a", "200", cursor)
        kinds.update(event["kind"] for event in page["events"])
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    assert {"progress_report", "candidate", "tool_receipt"} <= kinds, \
        "a full checkpoint type must not hide the later publication and tool receipt"


def test_issue262_api_shows_the_freeze_only_on_the_publishing_task(tmp_path, monkeypatch):
    """Issue #262: a workflow-level candidate_frozen reaches the trajectory of
    the task that published it, through the real publish/freeze chain."""
    import subprocess

    from herdr import scheduler_facts
    from herdr.state_store import SQLiteStateStore

    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              text=True, capture_output=True).stdout.strip()

    _git("init", "-q", "-b", "main")
    _git("config", "user.email", "fixture@example.invalid")
    _git("config", "user.name", "fixture")
    (repo / "f").write_text("candidate")
    _git("add", ".")
    _git("commit", "-qm", "candidate", "--no-gpg-sign")
    sha = _git("rev-parse", "HEAD")

    store = SQLiteStateStore(tmp_path / "state.db")
    store.save_workflow({"workflow_id": "wf-262", "status": "running",
                         "execution_id": "exec-262", "project_root": str(repo)})
    for name in ("task-pub", "task-other"):
        store.save_task({
            "task_id": name, "workflow_id": "wf-262", "node": "implementation",
            "execution_id": "exec-262", "run_id": "run-" + name, "status": "cleaned",
            "integration_mode": "git", "commit": sha, "integrated_commit": sha,
            "integration_ref": f"refs/herdr/tasks/{name}",
            "integration_publication_episode_id": 0,
        })
    _git("update-ref", "refs/herdr/tasks/task-pub", sha)
    publication = scheduler_facts.publish_integrated_candidate(store, "task-pub")
    frozen = scheduler_facts.record_candidate_frozen(
        "wf-262", sha, source_node="implementation", db_path=store.db_path,
        expected_episode_id=0, publication_event_id=publication["id"])
    assert frozen["status"] == "created"

    monkeypatch.setenv("HERDR_STATE_DB", str(store.db_path))
    published = console.api_task_trajectory("task-pub", "200", None)
    assert any(event["kind"] == "candidate" and event.get("title") == "候选版本已冻结"
               and sha in (event.get("detail") or "")
               for event in published["events"]), "publishing task must show the freeze"
    other = console.api_task_trajectory("task-other", "200", None)
    assert not any(event["kind"] == "candidate" and event.get("title") == "候选版本已冻结"
                   and sha in (event.get("detail") or "")
                   for event in other["events"]), "freeze must not leak to other tasks"
