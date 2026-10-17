import subprocess
from pathlib import Path
import pytest
from herdr.state_store import SQLiteStateStore
from herdr import workflow_docs as wd
from herdr.pr_delivery import create_task_pr, merge_task_pr, _publication


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


class Provider:
    def __init__(self, sha):
        self.sha = sha
        self.rows = []
        self.created = 0
        self.merged = None

    def list_open(self, head, base):
        return self.rows

    def create(self, head, base, title, body, draft):
        self.created += 1
        row = {
            'number': 1,
            'html_url': 'https://github.com/test/project/pull/1',
            'state': 'open',
            'head': {'sha': self.sha, 'ref': head},
            'base': {'ref': base},
        }
        self.rows = [row]
        return row

    def get_pr(self, number):
        for r in self.rows:
            if r.get('number') == number:
                return r
        return {
            'number': number,
            'html_url': f'https://github.com/test/project/pull/{number}',
            'state': 'open',
            'head': {'sha': self.sha, 'ref': 'herdr/integration-t1'},
            'base': {'ref': 'main'},
        }

    def merge(self, number, merge_method='merge'):
        self.merged = number
        return {'merged': True, 'sha': self.sha}


@pytest.fixture
def joint_delivery_fixture(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.email', 'test@example.com')
    git(repo, 'config', 'user.name', 'Test')
    (repo / 'file').write_text('base')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'base')
    base_sha = git(repo, 'rev-parse', 'HEAD')

    # Task 1 commit
    git(repo, 'switch', '-c', 'herdr/integration-t1')
    (repo / 'file').write_text('t1 work')
    git(repo, 'commit', '-am', 't1')
    t1_sha = git(repo, 'rev-parse', 'HEAD')

    # Joint candidate commit (advances t1)
    (repo / 'file2').write_text('joint work from t2')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'joint candidate assembling t1 and t2')
    joint_sha = git(repo, 'rev-parse', 'HEAD')

    remote = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
    git(repo, 'remote', 'add', 'origin', str(remote))

    from herdr import pr_delivery
    real_git = pr_delivery._git

    def transport_metadata(r, *args):
        if args[:2] == ('remote', 'get-url'):
            return 'https://github.com/test/project.git'
        if args and args[0] in {'ls-remote', 'push'}:
            args = tuple(str(remote) if value == 'https://github.com/test/project.git' else value for value in args)
        return real_git(r, *args)

    monkeypatch.setattr(pr_delivery, '_git', transport_metadata)

    store = SQLiteStateStore(tmp_path / 'state.db')
    monkeypatch.setenv('HERDR_STATE_DB', str(store.db_path))
    monkeypatch.setenv('HERDR_WORKFLOW_DOCS_DIR', str(tmp_path / 'docs'))

    store.save_workflow({'workflow_id': 'wf-joint', 'status': 'running'})
    store.save_task({
        'task_id': 't1',
        'workflow_id': 'wf-joint',
        'status': 'integrated',
        'source_repo': str(repo),
        'integration_branch': 'herdr/integration-t1',
        'integration_ref': 'refs/herdr/tasks/t1',
        'integrated_commit': t1_sha,
        'base_branch': 'main',
        'run_id': 'run-t1',
    })
    git(repo, 'update-ref', 'refs/herdr/tasks/t1', t1_sha)

    # Gates certified joint_sha
    for tid in ('review', 'test'):
        store.save_task({
            'task_id': tid,
            'workflow_id': 'wf-joint',
            'status': 'completed',
            'stage_verdict': 'pass',
            'candidate_sha': joint_sha,
            'verified_candidate_sha': joint_sha,
            'node': tid,
        })

    # Record delivery note pointing to joint_sha
    wd.append_note(
        'wf-joint',
        kind='delivery',
        title='candidate',
        body='',
        task_id='review',
        fields={
            'delivery_id': 'candidate-' + joint_sha,
            'delivery_branch': 'herdr/integration-t1',
            'candidate_sha': joint_sha,
            'review_task': 'review',
            'test_gate': 'test',
            'base': 'main',
        },
    )

    return store, repo, remote, t1_sha, joint_sha


def test_joint_candidate_publication_and_pr_creation(joint_delivery_fixture):
    store, repo, remote, t1_sha, joint_sha = joint_delivery_fixture
    provider = Provider(joint_sha)

    # t1_sha is ancestor of joint_sha in repo
    res = create_task_pr(store, 't1', title='Joint delivery PR', client=provider)
    assert res['number'] == 1
    assert res['candidate_sha'] == joint_sha
    assert provider.created == 1

    # Remote branch got joint_sha
    assert git(remote, 'rev-parse', 'refs/heads/herdr/integration-t1') == joint_sha

    # Merge PR works and verifies joint_sha
    merge_res = merge_task_pr(store, 't1', pr_number=1, client=provider)
    assert merge_res['status'] == 'merged'
    assert merge_res['candidate_sha'] == joint_sha


def test_joint_candidate_via_delivery_inventory_event(joint_delivery_fixture):
    store, repo, remote, t1_sha, joint_sha = joint_delivery_fixture
    # Record a candidate_frozen event with delivery_inventory
    store.record_event(
        'candidate_frozen',
        {
            'candidate_sha': joint_sha,
            'delivery_inventory': [
                {'task_id': 't1', 'commit': t1_sha},
            ],
        },
        workflow_id='wf-joint',
        source='critical-path-scheduler',
    )

    provider = Provider(joint_sha)
    res = create_task_pr(store, 't1', title='Joint delivery PR via event', client=provider)
    assert res['candidate_sha'] == joint_sha


def test_joint_candidate_rejected_if_unrelated_sha(joint_delivery_fixture):
    store, repo, remote, t1_sha, joint_sha = joint_delivery_fixture
    unrelated_sha = 'a' * 40
    # Task has an unintegrated commit unrelated to joint candidate
    t1 = store.get_task('t1')
    store.save_task({**t1, 'integrated_commit': unrelated_sha})

    provider = Provider(joint_sha)
    with pytest.raises(ValueError, match='delivery identity does not bind'):
        create_task_pr(store, 't1', title='Unrelated PR', client=provider)


def test_joint_candidate_rejected_if_gate_fails_certification(joint_delivery_fixture):
    store, repo, remote, t1_sha, joint_sha = joint_delivery_fixture
    # Review gate only certified t1_sha, not joint_sha
    review = store.get_task('review')
    store.save_task({**review, 'verified_candidate_sha': t1_sha})

    provider = Provider(joint_sha)
    with pytest.raises(ValueError, match='certify'):
        create_task_pr(store, 't1', title='Uncertified PR', client=provider)
