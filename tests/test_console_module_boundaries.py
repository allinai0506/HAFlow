"""Behavior contracts for Console boundaries and the real script entrypoint."""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from console import herdr_factory_console as console
from herdr import state_db

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_SHA256 = '3cd0257a6d22ab4037870991a88abe64122a20794318f1465ce481df87064214'


def test_page_template_is_byte_identical_to_pre_split_baseline():
    assert hashlib.sha256(console.HTML_TEMPLATE.encode()).hexdigest() == TEMPLATE_SHA256
    assert console.HTML == console.HTML_TEMPLATE.replace(
        '__PRODUCT_NAME__', console.PRODUCT_NAME,
    ).replace('__PRODUCT_TAGLINE__', console.PRODUCT_TAGLINE)


@pytest.fixture
def server(tmp_path, monkeypatch):
    db = tmp_path / 'state.db'
    monkeypatch.setenv('HERDR_STATE_DB', str(db))
    monkeypatch.setenv('WORKFLOWS_FILE', str(tmp_path / 'workflows.json'))
    monkeypatch.setenv('TASKS_FILE', str(tmp_path / 'tasks.json'))
    monkeypatch.setattr(console.common, 'WORKFLOWS_FILE', tmp_path / 'workflows.json')
    for wid in ('owned', 'other'):
        state_db.save_workflow({'workflow_id': wid, 'project_id': 'p', 'title': wid,
                               'status': 'running', 'config': {}}, db_path=db)
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), console.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{httpd.server_port}', db
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_http_rename_persists_and_public_read_returns_it(server):
    base, db = server
    request = urllib.request.Request(
        base + '/api/workflow/rename',
        data=json.dumps({'workflow_id': 'owned', 'title': '拆分验收'}).encode(),
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200
        assert response.headers['Cache-Control'] == 'no-store'
        payload = response.read()
        assert int(response.headers['Content-Length']) == len(payload)
        assert json.loads(payload)['data']['title'] == '拆分验收'
    connection = state_db.get_readonly_db_connection(db)
    try:
        rows = connection.execute('SELECT workflow_id,title FROM workflows').fetchall()
        assert {row['workflow_id']: row['title'] for row in rows} == {
            'owned': '拆分验收', 'other': 'other',
        }
    finally:
        connection.close()
    with urllib.request.urlopen(base + '/api/workflows?project_id=p', timeout=5) as response:
        rows = json.load(response)['data']
        assert {row['workflow_id']: row['title'] for row in rows} == {
            'owned': '拆分验收', 'other': 'other',
        }


@pytest.mark.parametrize('path,status', [('/missing', 404), ('/static/../herdr_factory_console.py', 403)])
def test_http_rejects_unknown_and_escaping_static_paths(server, path, status):
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(server[0] + path, timeout=5)
    assert error.value.code == status
    assert json.load(error.value)['ok'] is False


def test_direct_entrypoint_serves_same_page_from_another_cwd(tmp_path):
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    env = {**os.environ, 'HOME': str(tmp_path), 'HERDR_CONSOLE_PORT': str(port),
           'HERDR_STATE_DB': str(tmp_path / 'state.db'),
           'HERDR_OBSERVER_ENABLED': '0', 'HERDR_OBSERVER_JEV_ENABLED': '0'}
    env.pop('HERDR_ROOT', None)
    env.pop('PYTHONPATH', None)
    process = subprocess.Popen(
        [sys.executable, str(ROOT / 'console/herdr_factory_console.py')], cwd=tmp_path,
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            assert process.poll() is None, process.communicate(timeout=2)
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=1) as response:
                    assert response.read() == console.HTML.encode()
                    break
            except urllib.error.URLError:
                assert time.monotonic() < deadline, 'Console did not start'
                time.sleep(0.02)
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/static/console-tokens.css', timeout=2) as response:
            assert response.read() == (ROOT / 'console/static/console-tokens.css').read_bytes()
    finally:
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
    assert 'HERDR_RUNTIME_FINGERPRINT=' in stdout
    assert not stderr
