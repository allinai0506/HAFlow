"""Resetting a loop cannot keep old evidence current or alias a fresh run."""
from pathlib import Path
import subprocess
import sys

import pytest
from herdr.evaluator import init_loop, read_state
from herdr.supervisor.evidence import extract_test_evidence
from tests.test_task_loop_noninteractive import load_cli, ROOT


def evaluated(tmp_path, *, failed=False):
    directory = init_loop(tmp_path, 'previous contract', max_iterations=1,
                          test_cmd='false' if failed else "printf '1 passed in 0.1s\\n'")
    loop = load_cli('loop_reset_previous', 'herdr-loop')
    loop.run_evaluation(tmp_path)
    evidence = extract_test_evidence(str(tmp_path))
    assert evidence is not None
    return directory, evidence


@pytest.mark.parametrize('failed', [False, True])
def test_reinit_invalidates_previous_success_or_exhaustion_without_erasing_logs(tmp_path, failed):
    directory, previous = evaluated(tmp_path, failed=failed)
    log_before = (directory / 'logs/test.log').read_bytes()
    snapshot_before = (directory / 'EVAL_DONE.json').read_bytes()
    init_loop(tmp_path, 'new contract not yet evaluated', test_cmd='false')
    assert read_state(directory)['status'] == 'initialized'
    assert extract_test_evidence(str(tmp_path)) is None
    assert (directory / 'logs/test.log').read_bytes() == log_before
    receipt = directory / 'history' / f"EVAL_DONE-{previous['snapshot_sha256']}.json"
    assert receipt.read_bytes() == snapshot_before
    if failed:
        assert (directory / 'BLOCKER.md').exists()  # Historical document preserved.


def test_fresh_evaluation_after_reset_has_distinct_identity_even_with_equal_counts(tmp_path):
    _, old = evaluated(tmp_path)
    init_loop(tmp_path, 'new contract with equal test counts', test_cmd="printf '1 passed in 0.1s\\n'")
    load_cli('loop_reset_fresh', 'herdr-loop').run_evaluation(tmp_path)
    fresh = extract_test_evidence(str(tmp_path))
    assert fresh['converged']
    assert fresh['iteration'] == old['iteration'] == 1
    assert fresh['passed_tests'] == old['passed_tests'] == 1
    assert fresh['snapshot_sha256'] != old['snapshot_sha256']
    assert fresh['evidence_id'] != old['evidence_id']


def test_exact_snapshot_keeps_identity_across_reads_and_process_restart(tmp_path):
    _, first = evaluated(tmp_path)
    assert extract_test_evidence(str(tmp_path))['evidence_id'] == first['evidence_id']
    code = "from herdr.supervisor.evidence import extract_test_evidence;import sys;print(extract_test_evidence(sys.argv[1])['evidence_id'])"
    restart = subprocess.run([sys.executable, '-c', code, str(tmp_path)], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
    assert restart.returncode == 0
    assert restart.stdout.strip() == first['evidence_id']


def test_failed_new_contract_write_cannot_leave_old_success_current(tmp_path, monkeypatch):
    directory, _ = evaluated(tmp_path)
    original = Path.write_text
    def fail_goal(path, *a, **kw):
        if path == directory / 'GOAL.md':
            raise OSError('controlled new contract write failure')
        return original(path, *a, **kw)
    monkeypatch.setattr(Path, 'write_text', fail_goal)
    with pytest.raises(OSError):
        init_loop(tmp_path, 'new contract failed to initialize', test_cmd='false')
    assert extract_test_evidence(str(tmp_path)) is None
