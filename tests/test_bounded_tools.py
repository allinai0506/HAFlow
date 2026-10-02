import importlib
import sys
from pathlib import Path
import pytest


def api():
    from importlib.util import find_spec
    assert find_spec('herdr.bounded_tools'), 'bounded execution API missing'
    return importlib.import_module('herdr.bounded_tools')


def test_nul_and_budget_rejected_before_any_process(tmp_path):
    target = tmp_path / 'must-not-exist'
    with pytest.raises(ValueError, match='NUL'):
        api().run_bounded([sys.executable, '-c', f"open({str(target)!r}, 'w').write('effect')", '\x00'])
    with pytest.raises(ValueError, match='budget'):
        api().run_bounded([sys.executable, 'x' * 100000])
    assert not target.exists()


def test_execution_has_bounded_redacted_output_and_exit_code():
    result = api().run_bounded([sys.executable, '-c', "print('password=secret123'); raise SystemExit(3)"])
    assert result['status'] == 'completed' and result['exit_code'] == 3
    assert 'secret123' not in result['stdout']
    assert result['side_effects'] == 'possible'


def test_timeout_reports_uncertain_effect_and_owns_process(tmp_path):
    target = tmp_path / 'effect'
    result = api().run_bounded([sys.executable, '-c', f"import time; open({str(target)!r},'w').write('effect'); time.sleep(30)"], timeout=0.5)
    assert result['status'] == 'timeout' and result['side_effects'] == 'unknown'
    assert result['owned_process_group'] > 0 and target.read_text() == 'effect'


def test_output_limit_stops_stream_without_unbounded_buffer():
    result = api().run_bounded([sys.executable, '-c', "import sys; sys.stdout.write('x'*1000000); sys.stdout.flush()"], output_limit=1024)
    assert result['status'] == 'output_limit'
    assert len(result['stdout'].encode()) <= 1024
    assert result['side_effects'] == 'unknown'


def test_real_cli_tool_run_timeout_persists_uncertainty(tmp_path):
    import os, json, subprocess
    from herdr.state_store import get_state_store
    from herdr.observation import ObservationStore
    store = get_state_store(tmp_path / 'state.db')
    store.save_task({'task_id': 't', 'workflow_id': 'wf', 'node': 'n', 'run_id': 'r', 'completion_epoch': 'e', 'status': 'working'})
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'HOME': str(tmp_path), 'HERDR_STATE_DB': str(store.db_path), 'TASKS_FILE': str(tmp_path / 'tasks.json'), 'WORKFLOWS_FILE': str(tmp_path / 'workflows.json')}
    result = subprocess.run([sys.executable, str(root / 'bin/herdr-task'), 'tool-run', '--task-id', 't', '--run-id', 'r', '--epoch', 'e', '--timeout', '0.5', '--', sys.executable, '-c', "import time; print('password=secret123', flush=True); time.sleep(30)"], env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == 124, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt['side_effects'] == 'unknown' and receipt['status'] == 'timeout'
    assert 'secret123' not in result.stdout
    events = store.list_events(event_type='tool_execution')
    assert len(events) == 1 and events[0]['payload']['side_effects'] == 'unknown'
    assert events[0]['payload']['observation_id'] == receipt['observation_id']
    assert ObservationStore(store.db_path).verify(receipt['observation_id'])['valid']
    assert store.get_task('t')['status'] == 'working'


def test_output_cap_does_not_publish_partial_credential():
    result = api().run_bounded([sys.executable, '-c', "print('sk-abcdefghijk12345', flush=True)"], output_limit=8)
    assert result['status'] == 'output_limit'
    assert 'sk-' not in result['stdout']


def test_timeout_drops_incomplete_credential_record():
    result = api().run_bounded([sys.executable, '-c', "import sys,time; sys.stdout.write('sk-abcde'); sys.stdout.flush(); time.sleep(30)"], timeout=0.5)
    assert result['status'] == 'timeout'
    assert 'sk-' not in result['stdout']


def test_timeout_cleans_owned_children_even_if_parent_exits_on_term():
    import os, subprocess, signal
    code = """import os,signal,time
r,w=os.pipe()
pid=os.fork()
if pid==0:
 os.close(r); signal.signal(signal.SIGTERM, signal.SIG_IGN); os.write(w,b'ready'); os.close(w); time.sleep(30)
else:
 os.close(w); os.read(r,5); os.close(r); print(pid,flush=True); time.sleep(30)
"""
    result = api().run_bounded([sys.executable, '-c', code], timeout=0.5)
    child = int(result['stdout'].strip())
    state = subprocess.run(['ps', '-o', 'stat=', '-p', str(child)], text=True, capture_output=True).stdout.strip()
    try:
        assert not state or state.startswith('Z'), f'owned child still running: {state}'
    finally:
        try: os.killpg(result['owned_process_group'], signal.SIGKILL)
        except ProcessLookupError: pass
