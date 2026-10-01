"""Supervisor summaries must describe the current atomic evaluation, not old reports."""
import json
from pathlib import Path

import pytest
from herdr.evaluator import init_loop
from herdr.supervisor.evidence import collect_execution_evidence, summarize_loop
from tests.test_loop_reset_evidence_identity import evaluated
from tests.test_task_loop_noninteractive import load_cli


@pytest.mark.parametrize('failed', [False, True])
def test_real_reset_does_not_publish_old_metrics_or_blocker(tmp_path, failed):
    loop, _ = evaluated(tmp_path, failed=failed)
    init_loop(tmp_path, 'fresh unevaluated', test_cmd="printf '3 passed in 0.01s\\n'")
    current_display = (loop / 'METRICS.json').read_bytes()
    summary = summarize_loop(str(tmp_path))
    assert summary['loop_status'] == 'initialized'
    assert summary['iteration'] == 0
    assert not summary.get('blocker_report')
    assert summary.get('total_tests', 0) == 0
    assert summary.get('passed_tests', 0) == 0
    assert summary.get('failing_count', 0) == 0
    assert summary.get('composite_score', 0) == 0
    collected = collect_execution_evidence({'clone_path': str(tmp_path)}, trigger='tests_completed',
                                           git_runner=lambda *args: '')
    assert collected['tests'] == summary
    assert (loop / 'METRICS.json').read_bytes() == current_display
    if failed:
        assert (loop / 'BLOCKER.md').exists()
    load_cli('c31c_fresh_eval', 'herdr-loop').run_evaluation(tmp_path)
    fresh = summarize_loop(str(tmp_path))
    assert fresh['converged']
    assert fresh['passed_tests'] == fresh['total_tests'] == 3
    assert not fresh.get('blocker_report')


def test_current_exhaustion_keeps_its_blocker_and_counts(tmp_path):
    loop, _ = evaluated(tmp_path, failed=True)
    summary = summarize_loop(str(tmp_path))
    assert summary['loop_status'] == 'exhausted'
    assert summary['blocker_report']
    assert summary['total_tests'] == 1
    assert summary['passed_tests'] == 0
    assert summary['failing_count'] == 1
    assert (loop / 'BLOCKER.md').exists()


@pytest.mark.parametrize('invalid', ['{broken', '[]', '{"iteration": 1, "ts": 123}', '[' * 1500 + '0' + ']' * 1500])
def test_invalid_or_thin_snapshot_never_falls_back_to_old_reports(tmp_path, invalid):
    loop, _ = evaluated(tmp_path, failed=True)
    (loop / 'EVAL_DONE.json').write_text(invalid)
    assert summarize_loop(str(tmp_path)) is None


def test_legacy_without_snapshot_keeps_counts_but_not_stale_blocker(tmp_path):
    loop, _ = evaluated(tmp_path)
    (loop / 'EVAL_DONE.json').unlink()
    (loop / 'BLOCKER.md').write_text('historical blocker')
    summary = summarize_loop(str(tmp_path))
    assert summary['converged']
    assert summary['passed_tests'] == 1
    assert not summary.get('blocker_report')


def test_atomic_read_replacement_cannot_mix_snapshot_with_new_display_files(tmp_path, monkeypatch):
    loop, _ = evaluated(tmp_path)
    snap = loop / 'EVAL_DONE.json'
    old = snap.read_bytes()
    newer = json.loads(old)
    newer.update(status='exhausted', converged=False, total_tests=9, passed_tests=0,
                 failing_tests=['new failure'], composite_score=0)
    # Display files may have advanced before current EVAL_DONE publication.
    (loop / 'METRICS.json').write_text(json.dumps(newer))
    (loop / 'STATE.md').write_text('- **status**: exhausted\n- **iteration**: 9\n')
    original_read = Path.read_bytes
    reads = []
    def replace_after_read(path):
        result = original_read(path)
        if path == snap:
            reads.append(result)
            path.write_text(json.dumps(newer))
        return result
    monkeypatch.setattr(Path, 'read_bytes', replace_after_read)
    summary = summarize_loop(str(tmp_path))
    assert len(reads) == 1
    assert summary['loop_status'] == 'converged'
    assert summary['total_tests'] == summary['passed_tests'] == 1
    assert summary['composite_score'] == 100


@pytest.mark.parametrize('failed', [False, True])
def test_partial_init_metrics_write_failure_cannot_republish_old_facts(tmp_path, monkeypatch, failed):
    loop, _ = evaluated(tmp_path, failed=failed)
    old_metrics = (loop / 'METRICS.json').read_bytes()
    original = Path.write_text
    def fail_metrics(path, *args, **kwargs):
        if path == loop / 'METRICS.json':
            raise OSError('controlled reset display failure')
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'write_text', fail_metrics)
        with pytest.raises(OSError, match='controlled reset display'):
            init_loop(tmp_path, 'partially initialized new contract', test_cmd='false')
    assert (loop / 'METRICS.json').read_bytes() == old_metrics
    summary = summarize_loop(str(tmp_path))
    assert summary['loop_status'] == 'initialized'
    assert summary['iteration'] == 0
    assert summary.get('total_tests', 0) == 0
    assert summary.get('composite_score', 0) == 0
    assert not summary.get('blocker_report')


def test_unreadable_current_receipt_does_not_fallback_or_escape(tmp_path, monkeypatch):
    loop, _ = evaluated(tmp_path, failed=True)
    original = Path.read_bytes
    def unreadable(path):
        if path == loop / 'EVAL_DONE.json':
            raise PermissionError('controlled receipt read denial')
        return original(path)
    monkeypatch.setattr(Path, 'read_bytes', unreadable)
    assert summarize_loop(str(tmp_path)) is None
