"""HTTP-level probe: malformed Codex patch must never surface an internal error.

Tester-side evidence for B-1 / F1 at the real Console route, not the parser only.
Run with: PYTHONPATH=. pytest -q .herdr-test-evidence/test_http_malformed_patch.py
"""
import json
import socket
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from console import herdr_factory_console as console
from tests.test_execution_trace_api import _db, _task

MALFORMED = {
    "direct": 'text(await tools.apply_patch("*** Begin Patch\\n*** Update File: src/a.py\\n\\q"))',
    "const": 'const patch = "*** Begin Patch\\n*** Update File: src/a.py\\n@@\\n-a\\n+b\\q";\ntext(await tools.apply_patch(patch));',
    "git": 'apply_patch "*** Begin Patch\n*** Update File: src/a.py\n@@\n-a\n+b\n*** End Patch\n<<\'EOF\'\ntrailing\nEOF"',
    "hostile": 'const patch = "*** Begin Patch\\u0000\\n*** End Patch";\ntext(await tools.apply_patch(patch));',
}


def _rollout(call_id, input_text):
    rows = [
        {"type": "response_item", "payload": {"type": "session_meta", "cwd": "CLONE", "session_id": "sess-1"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": call_id, "input": input_text, "timestamp": 30}},
    ]
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"


@pytest.fixture
def served(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    clone = tmp_path / "clone"
    (clone / ".codex-home").mkdir(parents=True)
    task = _task()
    task["clone_path"] = str(clone)
    task["runtime"] = {"agent": "codex", "native_session_id": "sess-1"}
    _db(db, [task])
    sessions = tmp_path / "codex-sessions" / "2026" / "01"
    sessions.mkdir(parents=True)
    for index, (name, text) in enumerate(MALFORMED.items()):
        body = _rollout(f"c{index}", text).replace("CLONE", str(clone))
        (sessions / f"rollout-{name}-sess-1.jsonl").write_text(body, encoding="utf-8")
    monkeypatch.setenv("HERDR_STATE_DB", str(db))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "herdr.state_db.get_db_connection",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("writable connection")),
    )
    import herdr.native_session_trace as nst
    monkeypatch.setattr(nst, "default_roots", lambda: {"codex_sessions": sessions})
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = ThreadingHTTPServer(("127.0.0.1", port), console.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port, db
    server.shutdown()
    server.server_close()


INTERNAL_MARKERS = ("Begin Patch", "End Patch", "invalid escape", "Expecting value", "JSONDecodeError", "Traceback", "char 0", "SECRET_PROMPT")


def test_http_never_leaks_internal_parser_errors(served):
    port, _db_path = served
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/task/trajectory?id=task-a&limit=50") as response:
        body = response.read().decode()
        assert response.status == 200
    payload = json.loads(body)
    assert payload["ok"] is True
    for marker in INTERNAL_MARKERS:
        assert marker not in body, marker
    kinds = [event["kind"] for event in payload["data"]["events"]]
    assert "file_edit" not in kinds, kinds
    assert kinds.count("tool_call") >= 1, kinds


def test_multi_file_session_loses_colliding_refs_known_defect(served):
    """Four hostile actions in four files of one session id must all survive.

    Observed: they share source_ref 'codex:sess-1:1' and merge_trace_events
    keeps one. Recorded as DEFECT-2 (pre-existing in the baseline too).
    """
    port, _db_path = served
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/task/trajectory?id=task-a&limit=50") as response:
        payload = json.loads(response.read().decode())
    tool_calls = [e for e in payload["data"]["events"] if e["kind"] == "tool_call"]
    assert len(tool_calls) == len(MALFORMED), f"DEFECT-2: {len(tool_calls)} of {len(MALFORMED)} actions visible"


def test_http_route_does_not_touch_the_production_db(served, tmp_path):
    port, db = served
    assert db.name == "state.db"
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/task/trajectory?id=task-a") as response:
        assert response.status == 200
