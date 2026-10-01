"""Unpublished frozen candidates pass both real Git preflight boundaries."""
from types import SimpleNamespace
import subprocess
import inspect

import pytest
from tests.test_fix_loop_pr1 import _worker, _ht
from herdr.git_coordination import BranchOwnershipError


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def repository(tmp_path):
    remote = tmp_path / 'origin.git'
    subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
    source = tmp_path / 'source'
    subprocess.run(['git', 'clone', str(remote), str(source)], check=True, capture_output=True)
    git(source, 'config', 'user.name', 'Fixture')
    git(source, 'config', 'user.email', 'fixture@example.invalid')
    (source / 'base').write_text('base')
    git(source, 'add', 'base'); git(source, 'commit', '-m', 'base')
    git(source, 'push', 'origin', 'HEAD:main')
    git(source, 'switch', '-c', 'candidate/local')
    (source / 'candidate').write_text('frozen')
    git(source, 'add', 'candidate'); git(source, 'commit', '-m', 'candidate')
    return source, git(source, 'rev-parse', 'HEAD')


def checkout(repo, branch, pin):
    # Exercise old behavior too: an absent pin API must not hide its remote-only refusal.
    kwargs = {"candidate_sha": pin} if "candidate_sha" in inspect.signature(_worker.checkout_onto_branch).parameters else {}
    return _worker.checkout_onto_branch(repo, branch, **kwargs)


def launch_preflight(source, pin, monkeypatch):
    monkeypatch.setattr(_ht, 'load_tasks', lambda: {'tasks': []})
    monkeypatch.setattr(_ht, 'project_for_workflow', lambda _: {
        'project_root': str(source), 'base_branch': 'main', 'execution': {'mode': 'git'}})
    class Routed(Exception): pass
    def route(*a, **kw): raise Routed()
    monkeypatch.setattr(_ht, 'choose_agent', route)
    args = SimpleNamespace(task_id='new-task', workflow_id='wf-temp', node='implementation',
                           agent=None, source=str(source), onto='candidate/local',
                           candidate_sha=pin, integration_mode='git', task_type='feat')
    with pytest.raises(Routed):
        _ht._launch_task(args)


def test_cli_allows_exact_pinned_local_candidate_before_runtime_creation(tmp_path, monkeypatch):
    source, pin = repository(tmp_path)
    launch_preflight(source, pin, monkeypatch)


def test_worker_checks_local_pin_in_independent_clone(tmp_path, monkeypatch):
    source, pin = repository(tmp_path)
    monkeypatch.setattr(_worker, 'CLONE_ROOT', tmp_path / 'clones')
    monkeypatch.setattr(_worker, '_registered_tasks', lambda: [])
    # Real Worker CoW clone, including copied origin and independent metadata.
    (source / 'candidate').write_text('developer WIP must remain untouched')
    clone = _worker.create_clone(source, 'pinned-fixture')
    assert checkout(clone, 'candidate/local', pin) == 'candidate/local'
    assert git(clone, 'rev-parse', 'HEAD') == pin
    assert (source / 'candidate').read_text() == 'developer WIP must remain untouched'
    assert (clone / 'candidate').read_text() == 'frozen'


def test_worker_refuses_clone_branch_moved_since_source_validation(tmp_path, monkeypatch):
    source, pin = repository(tmp_path)
    (source / 'candidate').write_text('moved')
    git(source, 'commit', '-am', 'moved')
    monkeypatch.setattr(_worker, '_registered_tasks', lambda: [])
    with pytest.raises(RuntimeError, match='candidate pin'):
        checkout(source, 'candidate/local', pin)
    assert git(source, 'rev-parse', 'HEAD') != pin


def test_unpinned_unpublished_branch_still_refused(tmp_path, monkeypatch):
    source, _ = repository(tmp_path)
    monkeypatch.setattr(_worker, '_registered_tasks', lambda: [])
    with pytest.raises(RuntimeError, match='Onto branch not found'):
        _worker.checkout_onto_branch(source, 'candidate/local')


def test_pinned_branch_does_not_bypass_active_ownership(tmp_path, monkeypatch):
    source, pin = repository(tmp_path)
    monkeypatch.setattr(_worker, '_registered_tasks', lambda: [
        {'task_id': 'owner', 'branch': 'candidate/local', 'status': 'working'}])
    with pytest.raises(BranchOwnershipError):
        checkout(source, 'candidate/local', pin)


def test_cli_transmits_same_pin_to_worker_without_launching_agent(tmp_path, monkeypatch):
    source, pin = repository(tmp_path)
    monkeypatch.setattr(_ht, 'load_tasks', lambda: {'tasks': []})
    monkeypatch.setattr(_ht, 'project_for_workflow', lambda _: {
        'project_root': str(source), 'base_branch': 'main', 'execution': {'mode': 'git'}})
    monkeypatch.setattr(_ht, 'choose_agent', lambda *a, **kw: 'pi')
    monkeypatch.setattr(_ht, '_preflight_delivery_identity', lambda *a, **kw: None)
    monkeypatch.setattr(_ht, 'ensure_stage_topology', lambda *a: {
        'workspace_id': 'temp', 'anchor_pane_id': 'temp'})
    monkeypatch.setattr(_ht, 'acquire_pane_for_task', lambda *a: None)
    from herdr import workflow_docs
    monkeypatch.setattr(workflow_docs, 'workflow_docs_dir', lambda *a: tmp_path / 'docs')
    original = subprocess.run
    class WorkerBoundary(Exception): pass
    def execute(cmd, **kw):
        if str(cmd[0]).endswith('/herdr-worker.py'):
            assert cmd[cmd.index('--candidate-sha') + 1] == pin
            assert cmd[cmd.index('--onto') + 1] == 'candidate/local'
            raise WorkerBoundary()
        return original(cmd, **kw)
    monkeypatch.setattr(_ht.subprocess, 'run', execute)
    args = SimpleNamespace(task_id='new-task', workflow_id='wf-temp', node='implementation',
                           agent=None, source=str(source), onto='candidate/local',
                           candidate_sha=pin, integration_mode='git', task_type='feat')
    with pytest.raises(WorkerBoundary):
        _ht._launch_task(args)


@pytest.mark.parametrize('symbolic', ['HEAD', 'candidate/local', 'abbreviated'])
def test_worker_requires_full_immutable_sha_not_a_moving_revision(tmp_path, monkeypatch, symbolic):
    source, pin = repository(tmp_path)
    monkeypatch.setattr(_worker, '_registered_tasks', lambda: [])
    claim = pin[:8] if symbolic == 'abbreviated' else symbolic
    with pytest.raises(RuntimeError, match='candidate pin'):
        checkout(source, 'candidate/local', claim)
