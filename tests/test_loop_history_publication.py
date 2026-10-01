"""A failed history publication cannot destroy the previous evaluation receipt."""
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
from herdr.evaluator import init_loop
from herdr.supervisor.evidence import extract_test_evidence
from tests.test_loop_reset_evidence_identity import evaluated
from tests.test_task_loop_noninteractive import ROOT


@pytest.mark.parametrize('failed', [False, True])
@pytest.mark.parametrize('fault', ['mkdir', 'write', 'replace', 'interruption'])
def test_archive_failure_keeps_old_contract_and_receipt_then_retry_recovers(tmp_path, monkeypatch, failed, fault):
    loop, previous = evaluated(tmp_path, failed=failed)
    original = (loop / 'EVAL_DONE.json').read_bytes()
    goal = (loop / 'GOAL.md').read_bytes()
    methods = {name: getattr(Path, name) for name in ['mkdir', 'write_bytes', 'replace']}
    method = {'mkdir': 'mkdir', 'write': 'write_bytes', 'replace': 'replace',
              'interruption': 'mkdir'}[fault]

    def broken(path, *args, **kwargs):
        if path == loop / 'history' or path.parent == loop / 'history':
            if fault == 'interruption':
                raise SystemExit(73)
            raise OSError('controlled archive failure')
        return methods[method](path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, method, broken)
        with pytest.raises(SystemExit if fault == 'interruption' else OSError):
            init_loop(tmp_path, 'replacement not published', test_cmd='false')
    assert (loop / 'GOAL.md').read_bytes() == goal
    assert (loop / 'EVAL_DONE.json').read_bytes() == original
    assert extract_test_evidence(str(tmp_path))['snapshot_sha256'] == previous['snapshot_sha256']
    init_loop(tmp_path, 'successful retry', test_cmd='false')
    receipt = loop / 'history' / ('EVAL_DONE-' + hashlib.sha256(original).hexdigest() + '.json')
    assert receipt.read_bytes() == original
    assert extract_test_evidence(str(tmp_path)) is None


@pytest.mark.parametrize('failed', [False, True])
def test_native_cli_filesystem_failure_preserves_bytes_for_retry(tmp_path, failed):
    loop, _ = evaluated(tmp_path, failed=failed)
    original = (loop / 'EVAL_DONE.json').read_bytes()
    history = loop / 'history'
    history.write_text('controlled non-directory obstacle')
    args = [sys.executable, str(ROOT / 'bin/herdr-loop'), 'init',
            '--dir', str(tmp_path), '--goal', 'native retry', '--test-cmd', 'false']
    first = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert first.returncode != 0
    assert (loop / 'EVAL_DONE.json').read_bytes() == original
    history.unlink()
    retry = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert retry.returncode == 0
    assert (history / ('EVAL_DONE-' + hashlib.sha256(original).hexdigest() + '.json')).read_bytes() == original
    assert extract_test_evidence(str(tmp_path)) is None


def test_conflicting_history_does_not_replace_current_contract(tmp_path):
    loop, _ = evaluated(tmp_path)
    original = (loop / 'EVAL_DONE.json').read_bytes()
    history = loop / 'history'
    history.mkdir()
    (history / ('EVAL_DONE-' + hashlib.sha256(original).hexdigest() + '.json')).write_bytes(b'conflict')
    with pytest.raises(RuntimeError, match='Conflicting historical'):
        init_loop(tmp_path, 'unpublished contract', test_cmd='false')
    assert (loop / 'EVAL_DONE.json').read_bytes() == original


def test_existing_matching_history_is_idempotently_reused(tmp_path):
    loop, _ = evaluated(tmp_path)
    original = (loop / 'EVAL_DONE.json').read_bytes()
    history = loop / 'history'
    history.mkdir()
    receipt = history / ('EVAL_DONE-' + hashlib.sha256(original).hexdigest() + '.json')
    receipt.write_bytes(original)
    init_loop(tmp_path, 'normal existing archive', test_cmd='false')
    assert receipt.read_bytes() == original
    assert extract_test_evidence(str(tmp_path)) is None
