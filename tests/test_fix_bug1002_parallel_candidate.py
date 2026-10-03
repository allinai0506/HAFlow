import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest
from herdr import direct_dispatch as dd
from herdr.state_store import SQLiteStateStore
from tests.test_worker_baseline_anchor import load_worker, git


def repository(tmp_path):
    repo = tmp_path / 'repo'; repo.mkdir()
    git(repo, 'init', '-b', 'main'); git(repo, 'config', 'user.name', 'Test')
    git(repo, 'config', 'user.email', 'test@example.com')
    (repo / 'file').write_text('base'); git(repo, 'add', '.'); git(repo, 'commit', '-m', 'base')
    git(repo, 'switch', '-c', 'herdr/integration-impl')
    (repo / 'file').write_text('candidate'); git(repo, 'commit', '-am', 'candidate')
    sha = git(repo, 'rev-parse', 'HEAD'); git(repo, 'switch', 'main')
    return repo, sha


@pytest.mark.parametrize('node_id', ['test', 'review'])
def test_frozen_verifier_spec_uses_owned_branch(node_id):
    node = dd.normalize_node({'id': node_id})
    spec = dd._dispatch_spec(node, 'requirement', 'goal', [], 'verify-'+node_id,
                             context_branch='herdr/integration-impl', candidate_sha='a'*40)
    assert spec['candidate_sha'] == 'a'*40
    assert 'onto_branch' not in spec


def test_two_real_git_verifiers_share_candidate_without_sharing_branch(tmp_path):
    import shutil
    repo, sha = repository(tmp_path); worker = load_worker()
    owned = []
    for name, agent in [('test', 'codex'), ('review', 'claude')]:
        clone = tmp_path / name; shutil.copytree(repo, clone)
        with patch.object(worker, '_registered_tasks', return_value=owned):
            branch = worker.create_task_branch(clone, name, agent, 'test', 'main', candidate_sha=sha)
        assert git(clone, 'rev-parse', 'HEAD') == sha
        assert git(clone, 'branch', '--show-current') == branch
        owned.append({'task_id':name,'branch':branch,'status':'working'})
    assert owned[0]['branch'] != owned[1]['branch']
    assert git(repo, 'branch', '--show-current') == 'main'


def test_cli_local_onto_without_candidate_rejects_before_intent(tmp_path):
    repo, sha = repository(tmp_path)
    config = tmp_path / 'workflow.json'; config.write_text(json.dumps({'nodes':[{'id':'test'}]}))
    store = SQLiteStateStore(tmp_path / 'state.db')
    store.save_workflow({'workflow_id':'wf','project_id':'p','project_root':str(repo),
                         'workflow_file':str(config),'status':'running','execution_mode':'git'})
    env = {**os.environ,'HOME':str(tmp_path),'HERDR_STATE_DB':str(store.db_path),
           'TASKS_FILE':str(tmp_path/'tasks.json')}
    result = subprocess.run([sys.executable, 'bin/herdr-task', 'launch', '--task-id','verify',
        '--workflow-id','wf','--node','test','--source',str(repo),'--agent','codex',
        '--onto','herdr/integration-impl','--goal','Verify','--prompt','Verify'],
        env=env,text=True,capture_output=True,timeout=10)
    assert result.returncode == 2
    assert '--candidate-sha' in result.stdout + result.stderr
    assert sha in result.stdout + result.stderr
    assert not store.list_events(workflow_id='wf',event_type='launch_intent')


def test_pinned_verifier_keeps_branch_ownership_and_rejects_unknown_candidate(tmp_path):
    from herdr.git_coordination import BranchOwnershipError
    repo, sha = repository(tmp_path); worker = load_worker()
    task = {'task_id':'other','branch':'agent/codex/test-verifier','status':'working'}
    with patch.object(worker, '_registered_tasks', return_value=[task]):
        with pytest.raises(BranchOwnershipError):
            worker.create_task_branch(repo, 'verifier', 'codex', 'test', 'main', candidate_sha=sha)
    with patch.object(worker, '_registered_tasks', return_value=[]):
        with pytest.raises(RuntimeError, match='unavailable'):
            worker.create_task_branch(repo, 'verifier', 'codex', 'test', 'main', candidate_sha='0'*40)
    assert git(repo, 'branch', '--show-current') == 'main'
