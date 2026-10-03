"""Platform publication occurs after integration and binds the exact candidate."""
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from herdr.state_store import SQLiteStateStore
from herdr import workflow_docs


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    from herdr import workflow_docs as wd
    repo = tmp_path / 'repo'; repo.mkdir()
    git(repo, 'init', '-b', 'main'); git(repo, 'config', 'user.email', 'test@example.com')
    git(repo, 'config', 'user.name', 'Test')
    (repo / 'file').write_text('base'); git(repo, 'add', '.'); git(repo, 'commit', '-m', 'base')
    base = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'switch', '-c', 'herdr/integration-t')
    (repo / 'file').write_text('candidate'); git(repo, 'commit', '-am', 'candidate')
    sha = git(repo, 'rev-parse', 'HEAD')
    remote = tmp_path / 'remote.git'; subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
    git(repo, 'remote', 'add', 'origin', str(remote))
    from herdr import pr_delivery
    real_git=pr_delivery._git
    def transport_metadata(repo,*args):
        if args[:2]==('remote','get-url'):
            return 'https://github.com/test/project.git'
        if args and args[0] in {'ls-remote','push'}:
            args=tuple(str(remote) if value=='https://github.com/test/project.git' else value for value in args)
        return real_git(repo,*args)
    monkeypatch.setattr(pr_delivery,'_git',transport_metadata)
    store = SQLiteStateStore(tmp_path / 'state.db')
    monkeypatch.setenv('HERDR_STATE_DB', str(store.db_path))
    monkeypatch.setenv('HERDR_WORKFLOW_DOCS_DIR', str(tmp_path / 'docs'))
    store.save_workflow({'workflow_id': 'wf', 'status': 'running'})
    store.save_task({'task_id':'t','workflow_id':'wf','status':'integrated','source_repo':str(repo),
                    'integration_branch':'herdr/integration-t','integration_ref':'refs/herdr/tasks/t',
                    'integrated_commit':sha,'base_branch':'main','run_id':'run'})
    git(repo, 'update-ref', 'refs/herdr/tasks/t', sha)
    for tid in ('review', 'test'):
        store.save_task({'task_id':tid,'workflow_id':'wf','status':'completed',
                         'stage_verdict':'pass','candidate_sha':sha,'verified_candidate_sha':sha,'node':tid})
    wd.append_note('wf',kind='delivery',title='candidate',body='',task_id='review',
                   fields={'delivery_id':'candidate-'+sha,'delivery_branch':'herdr/integration-t',
                           'candidate_sha':sha,'review_task':'review','test_gate':'test','base':'main'})
    return store, repo, remote, sha


class Provider:
    def __init__(self): self.rows=[]; self.created=0; self.merged=None
    def list_open(self, head, base): return self.rows
    def create(self, head, base, title, body, draft):
        self.created += 1
        row={'number':1,'html_url':'https://github.com/test/project/pull/1',
             'state':'open','head':{'sha':self.sha,'ref':head},'base':{'ref':base}}
        self.rows=[row]; return row
    def get_pr(self, number):
        for r in self.rows:
            if r.get('number') == number: return r
        return {'number': number, 'html_url': f'https://github.com/test/project/pull/{number}',
                'state': 'open', 'head': {'sha': getattr(self, 'pr_head_sha', self.sha), 'ref': 'herdr/integration-t'},
                'base': {'ref': 'main'}}
    def merge(self, number, merge_method='merge'):
        self.merged = number
        return {'merged': True, 'sha': getattr(self, 'pr_head_sha', self.sha)}



def test_platform_publication_uses_integrated_sha_even_from_anchor(delivery):
    from herdr.pr_delivery import create_task_pr
    store, repo, remote, sha=delivery
    git(repo,'switch','main')
    provider=Provider();provider.sha=sha
    first=create_task_pr(store,'t',title='Fix',client=provider)
    second=create_task_pr(store,'t',title='Fix',client=provider)
    assert first['number']==second['number']==1 and provider.created==1
    assert git(remote,'rev-parse','refs/heads/herdr/integration-t')==sha
    assert git(repo,'branch','--show-current')=='main'
    assert store.list_events(task_id='t',event_type='pull_request_created')


@pytest.mark.parametrize('updates',[{'status':'committed'}, {'integrated_commit':'0'*40}])
def test_pr_rejects_unintegrated_or_changed_candidate(delivery,updates):
    from herdr.pr_delivery import create_task_pr
    store,repo,remote,sha=delivery
    task=store.get_task('t');store.save_task({**task,**updates})
    provider=Provider();provider.sha=sha
    with pytest.raises(ValueError):create_task_pr(store,'t',title='Fix',client=provider)
    assert provider.created==0
    assert subprocess.run(['git','-C',str(remote),'show-ref'],capture_output=True).returncode == 1


def test_pr_gate_rejects_foreign_review(delivery):
    from herdr.pr_delivery import create_task_pr
    store,repo,remote,sha=delivery
    review=store.get_task('review');store.save_task({**review,'workflow_id':'foreign'})
    with pytest.raises(ValueError,match='gate'):create_task_pr(store,'t',title='Fix',client=Provider())


def test_remote_divergence_never_overwrites(delivery):
    from herdr.pr_delivery import create_task_pr
    store,repo,remote,sha=delivery
    base=git(repo,'rev-parse','main')
    git(repo,'push',str(remote),f'{base}:refs/heads/herdr/integration-t')
    provider=Provider();provider.sha=sha
    with pytest.raises(ValueError,match='remote'):create_task_pr(store,'t',title='Fix',client=provider)
    assert git(remote,'rev-parse','refs/heads/herdr/integration-t')==base
    assert provider.created==0


def test_real_http_provider_roundtrip(delivery,monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from herdr.pr_delivery import PullRequestClient, create_task_pr
    store,repo,remote,sha=delivery
    rows=[]; requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            requests.append((self.command,self.path,self.headers.get('Authorization')))
            data=json.dumps(rows).encode();self.send_response(200);self.end_headers();self.wfile.write(data)
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append((self.command,payload,self.headers.get('Authorization')))
            row={'number':7,'html_url':'https://github.com/test/project/pull/7','state':'open',
                 'head':{'ref':payload['head'],'sha':sha},'base':{'ref':payload['base']}}
            rows.append(row);self.send_response(201);self.end_headers();self.wfile.write(json.dumps(row).encode())
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setenv('GH_TOKEN','test-only-token')
    try:
        client=PullRequestClient('github.com','test','project',api_root=f'http://127.0.0.1:{server.server_port}')
        # Exercise the actual CLI parser -> core -> HTTP -> StateStore path.
        import importlib.machinery
        import importlib.util
        import sys
        from herdr import pr_delivery
        path=Path(__file__).resolve().parents[1]/'bin/herdr-task'
        spec=importlib.util.spec_from_loader('bug1002_pr_cli',importlib.machinery.SourceFileLoader('bug1002_pr_cli',str(path)))
        cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
        body_path=repo/'pr-body.txt';body_path.write_text('Review required')
        monkeypatch.setattr(pr_delivery,'PullRequestClient',lambda *args:client)
        monkeypatch.setattr(sys,'argv',['herdr-task','create-pr','t','--title','Candidate','--body-file',str(body_path),'--draft'])
        cli.main()
        result=rows[0]
        replay=create_task_pr(store,'t',title='Candidate',client=client)
        assert result['number']==replay['number']==7
        posts=[r for r in requests if r[0]=='POST'];assert len(posts)==1
        assert posts[0][1]['draft'] is True and posts[0][1]['body']=='Review required'
        assert all(r[2]=='Bearer test-only-token' for r in requests)
        assert 'test-only-token' not in json.dumps(store.list_events(task_id='t'))
    finally:server.shutdown();server.server_close();thread.join()


def test_provider_unknown_create_replays_by_remote_inventory(delivery):
    from herdr.pr_delivery import create_task_pr
    store,repo,remote,sha=delivery
    class Uncertain(Provider):
        def create(self,*args):
            super().create(*args)
            raise RuntimeError('response lost')
    provider=Uncertain();provider.sha=sha
    with pytest.raises(RuntimeError):create_task_pr(store,'t',title='Fix',client=provider)
    assert create_task_pr(store,'t',title='Fix',client=provider)['number']==1
    assert provider.created==1


@pytest.mark.parametrize('urls',['https://github.com/other/private.git',
    'https://github.com/test/project.git\nhttps://github.com/other/private.git'])
def test_push_target_mismatch_has_no_remote_side_effect(delivery,urls,monkeypatch):
    from herdr import pr_delivery
    store,repo,remote,sha=delivery
    original=pr_delivery._git
    def metadata(repo,*args):
        if args[:4]==('remote','get-url','--push','--all'):return urls
        return original(repo,*args)
    monkeypatch.setattr(pr_delivery,'_git',metadata)
    provider=Provider();provider.sha=sha
    with pytest.raises(ValueError,match='push repository'):
        pr_delivery.create_task_pr(store,'t',title='Fix',client=provider)
    assert subprocess.run(['git','-C',str(remote),'show-ref'],capture_output=True).returncode==1
    assert provider.created==0


def test_implementation_cannot_impersonate_both_gates(delivery):
    from herdr.pr_delivery import create_task_pr
    store,repo,remote,sha=delivery
    for tid in ('review','test'):
        task=store.get_task(tid);store.save_task({**task,'node':'implementation'})
    with pytest.raises(ValueError,match='role'):create_task_pr(store,'t',title='Fix',client=Provider())


def test_default_worker_role_preserves_authenticated_gate_node(delivery):
    from herdr.pr_delivery import create_task_pr
    store,repo,remote,sha=delivery
    for tid in ('review','test'):
        gate=store.get_task(tid);store.save_task({**gate,'dispatch_role':'worker'})
    provider=Provider(); provider.sha=sha
    result=create_task_pr(store,'t',title='Fix',client=provider)
    assert provider.created == 1
    assert result['candidate_sha'] == sha


@pytest.mark.parametrize('verified', [None, '0'*40])
def test_pr_requires_actual_gate_candidate_evidence(delivery,verified):
    from herdr.pr_delivery import create_task_pr
    store,repo,remote,sha=delivery
    gate=store.get_task('review');store.save_task({**gate,'verified_candidate_sha':verified})
    provider=Provider();provider.sha=sha
    with pytest.raises(ValueError,match='certify'):
        create_task_pr(store,'t',title='Fix',client=provider)
    assert provider.created == 0
    assert subprocess.run(['git','-C',str(remote),'show-ref'],capture_output=True).returncode == 1


def test_merge_task_pr_verifies_gate_sha_and_merges(delivery):
    from herdr.pr_delivery import merge_task_pr
    store, repo, remote, sha = delivery
    provider = Provider()
    provider.sha = sha
    result = merge_task_pr(store, 't', pr_number=1, client=provider)
    assert result['status'] == 'merged'
    assert result['candidate_sha'] == sha
    assert provider.merged == 1
    assert store.list_events(task_id='t', event_type='pull_request_merged')


def test_merge_task_pr_rejects_unverified_candidate_sha(delivery):
    from herdr.pr_delivery import merge_task_pr
    store, repo, remote, sha = delivery
    provider = Provider()
    provider.sha = sha
    # Simulate §1.6: PR head is an unverified old commit, while gate verified 'sha'
    provider.pr_head_sha = '8be3099a60000000000000000000000000000000'
    with pytest.raises(ValueError, match='differs from gate-certified candidate'):
        merge_task_pr(store, 't', pr_number=1, client=provider)
    assert provider.merged is None
    assert not store.list_events(task_id='t', event_type='pull_request_merged')


def test_merge_pr_cli(delivery, monkeypatch):
    from herdr.pr_delivery import PullRequestClient
    import importlib.machinery
    import importlib.util
    import sys
    from herdr import pr_delivery
    store, repo, remote, sha = delivery
    provider = Provider()
    provider.sha = sha
    monkeypatch.setattr(pr_delivery, 'PullRequestClient', lambda *args: provider)

    path = Path(__file__).resolve().parents[1] / 'bin/herdr-task'
    spec = importlib.util.spec_from_loader('bug1002_merge_cli', importlib.machinery.SourceFileLoader('bug1002_merge_cli', str(path)))
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    monkeypatch.setattr(sys, 'argv', ['herdr-task', 'merge-pr', 't', '--number', '1', '--method', 'squash'])
    cli.main()
    assert provider.merged == 1
    assert store.list_events(task_id='t', event_type='pull_request_merged')
