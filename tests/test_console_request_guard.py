"""Regression: console HTTP routes must refuse drive-by, cross-origin and DNS-rebinding requests.

Finding D6/B-04: every console POST route (kernel force-pass, task force-review, rollback,
checkpoint restore, workflow rename, ...) accepted requests from any origin, because
Handler checked neither the Host header, nor a custom request header, nor Origin.
"""
import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from console import common
from console import herdr_factory_console as console
from herdr import state_db

HEADER = 'X-HAFlow-Console-Request'
RENAME = '/api/workflow/rename'
RENAME_BODY = json.dumps({'workflow_id': 'owned', 'title': '被改写'}).encode()
JSON_HEADERS = {'Content-Type': 'application/json'}


@pytest.fixture
def server(tmp_path, monkeypatch):
    db = tmp_path / 'state.db'
    monkeypatch.setenv('HERDR_STATE_DB', str(db))
    monkeypatch.setenv('WORKFLOWS_FILE', str(tmp_path / 'workflows.json'))
    monkeypatch.setenv('TASKS_FILE', str(tmp_path / 'tasks.json'))
    monkeypatch.setattr(common, 'WORKFLOWS_FILE', tmp_path / 'workflows.json')
    state_db.save_workflow({'workflow_id': 'owned', 'project_id': 'p', 'title': '原标题',
                            'status': 'running', 'config': {}}, db_path=db)
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), console.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_port, db
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _send(port, method, path, body=None, headers=None):
    conn = HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def _title(db):
    return state_db.get_workflow('owned', db_path=db)['title']


@pytest.mark.parametrize('method,headers,expected', [
    ('GET', {'Host': '127.0.0.1:8765'}, None),
    ('GET', {'Host': 'localhost:8765'}, None),
    ('GET', {'Host': 'attacker.example:8765'}, 'host_not_allowed'),
    ('GET', {'Host': '127.0.0.1'}, 'host_not_allowed'),
    ('GET', {}, 'host_not_allowed'),
    ('POST', {'Host': '127.0.0.1:8765', HEADER: '1'}, None),
    ('POST', {'Host': '127.0.0.1:8765'}, 'console_request_header_required'),
    ('POST', {'Host': '127.0.0.1:8765', HEADER: '0'}, 'console_request_header_required'),
    ('POST', {'Host': '127.0.0.1:8765', HEADER: '1', 'Origin': 'http://127.0.0.1:8765'}, None),
    ('POST', {'Host': '127.0.0.1:8765', HEADER: '1', 'Origin': 'http://evil.example'}, 'origin_not_allowed'),
    ('POST', {'Host': '127.0.0.1:8765', HEADER: '1', 'Origin': 'null'}, 'origin_not_allowed'),
])
def test_rejection_policy(method, headers, expected):
    assert common.console_request_rejection(method, headers, 8765) == expected


def test_port_80_also_accepts_bare_loopback_host():
    assert common.console_request_rejection('GET', {'Host': '127.0.0.1'}, 80) is None


def test_post_without_console_header_is_rejected_and_writes_nothing(server):
    port, db = server
    status, body = _send(port, 'POST', RENAME, RENAME_BODY,
                         {**JSON_HEADERS, 'Host': f'127.0.0.1:{port}'})
    assert status == 403
    assert json.loads(body)['error'] == 'console_request_header_required'
    assert _title(db) == '原标题'


def test_text_plain_drive_by_post_is_rejected(server):
    port, db = server
    status, _ = _send(port, 'POST', RENAME, RENAME_BODY, {
        'Content-Type': 'text/plain', 'Host': f'127.0.0.1:{port}', 'Origin': 'http://evil.example'})
    assert status == 403
    assert _title(db) == '原标题'


def test_cross_origin_post_with_header_is_rejected(server):
    port, db = server
    status, body = _send(port, 'POST', RENAME, RENAME_BODY, {
        **JSON_HEADERS, HEADER: '1', 'Host': f'127.0.0.1:{port}', 'Origin': 'http://evil.example'})
    assert status == 403
    assert json.loads(body)['error'] == 'origin_not_allowed'
    assert _title(db) == '原标题'


def test_local_console_post_with_header_is_applied(server):
    port, db = server
    status, body = _send(port, 'POST', RENAME, RENAME_BODY, {
        **JSON_HEADERS, HEADER: '1', 'Host': f'127.0.0.1:{port}',
        'Origin': f'http://127.0.0.1:{port}'})
    assert status == 200
    assert json.loads(body)['data']['title'] == '被改写'
    assert _title(db) == '被改写'


def test_dns_rebinding_host_is_rejected_even_for_reads(server):
    port, _ = server
    status, body = _send(port, 'GET', '/', None, {'Host': f'attacker.example:{port}'})
    assert status == 403
    assert b'<html' not in body.lower()


def test_local_page_load_is_still_served(server):
    port, _ = server
    status, body = _send(port, 'GET', '/', None, {'Host': f'127.0.0.1:{port}'})
    assert status == 200
    assert b'<html' in body.lower()


def test_ui_sends_console_header_on_every_api_call():
    assert f"'{HEADER}':'1'" in console.HTML_TEMPLATE
    simple = (Path(common.CONSOLE_STATIC_DIR) / 'simple.js').read_text(encoding='utf-8')
    assert f"'{HEADER}':'1'" in simple
