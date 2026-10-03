"""Publish an accepted, integrated candidate without relaxing adoption ownership."""
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import workflow_docs as wd
from .delivery_record import select_effective_delivery, _body_value
from .task_resources import workflow_launch_lock


def repository_identity(remote):
    match = re.fullmatch(r'(?:https://|git@)(github\.com|gitee\.com)[/:]([^/\s:]+)/([^/\s]+?)(?:\.git)?', remote)
    if not match:
        raise ValueError('PR publication requires an identified GitHub or Gitee origin')
    host, owner, repo = match.groups()
    if not re.fullmatch(r'[\w.-]+', owner) or not re.fullmatch(r'[\w.-]+', repo):
        raise ValueError('invalid repository identity')
    return host, owner, repo


class PullRequestClient:
    def __init__(self, host, owner, repo, *, api_root=None):
        self.host = host
        self.root = (api_root or ('https://api.github.com' if host == 'github.com' else 'https://gitee.com/api/v5')).rstrip('/')
        self.path = f'/repos/{owner}/{repo}/pulls'
        self.owner = owner
        self.token = (os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')) if host == 'github.com' else os.environ.get('GITEE_TOKEN')
        if not self.token:
            raise ValueError('PR provider credential is not configured')

    def _request(self, method, values, subpath=''):
        headers = {'Accept':'application/json', 'User-Agent':'HAFlow'}
        if self.host == 'github.com':
            headers['Authorization'] = f'Bearer {self.token}'
            headers['X-GitHub-Api-Version'] = '2022-11-28'
        else:
            values = {**values, 'access_token':self.token}
        url = self.root + self.path + (f'/{subpath}' if subpath else '')
        data = None
        if method == 'GET':
            if values:
                url += '?' + urlencode(values)
        else:
            data = json.dumps(values).encode()
            headers['Content-Type'] = 'application/json'
        try:
            with urlopen(Request(url, data=data, headers=headers, method=method), timeout=20) as response:
                raw = response.read(1024*1024+1)
            if len(raw) > 1024*1024:
                raise ValueError('provider response exceeds budget')
            return json.loads(raw)
        except Exception as exc:
            # URLs and provider bodies can carry credentials or private data.
            raise RuntimeError(f'PR provider request failed ({type(exc).__name__}); reconcile before retry') from None

    def list_open(self, head, base):
        rows = self._request('GET', {'state':'open','head':f'{self.owner}:{head}' if self.host == 'github.com' else head,
                                     'base':base,'per_page':100,'page':1})
        if not isinstance(rows,list) or len(rows) >= 100:
            raise ValueError('PR inventory is incomplete or exceeds budget')
        return rows

    def create(self, head, base, title, body, draft):
        return self._request('POST', {'head':head,'base':base,'title':title,'body':body,'draft':draft})

    def get_pr(self, number):
        return self._request('GET', {}, subpath=str(number))

    def merge(self, number, merge_method='merge'):
        return self._request('PUT', {'merge_method': merge_method}, subpath=f'{number}/merge')


def _git(repo, *args):
    result = subprocess.run(['git','-C',str(repo),*args],capture_output=True,text=True,timeout=30)
    if result.returncode:
        raise ValueError('publication Git operation failed: '+args[0])
    return result.stdout.strip()


def _publication(task, notes, store):
    if task.get('status') not in {'integrated','cleanup_ready','cleaned'}:
        raise ValueError('task must be integrated before PR publication')
    note = select_effective_delivery(wd.annotate_notes(notes))
    sha = task.get('integrated_commit')
    branch = task.get('integration_branch')
    if branch != f"herdr/integration-{task['task_id']}" or task.get('integration_ref') != f"refs/herdr/tasks/{task['task_id']}":
        raise ValueError('publication requires the task-owned integration refs')
    if (not note or not re.fullmatch(r'[0-9a-f]{40}', str(sha or ''))
            or _body_value(note,'candidate_sha') != sha
            or _body_value(note,'delivery_branch') != branch):
        raise ValueError('delivery identity does not bind the integrated candidate')
    if _body_value(note,'review_task') == _body_value(note,'test_gate'):
        raise ValueError('review and test gates must be distinct tasks')
    for field in ('review_task','test_gate'):
        gate = store.get_task(_body_value(note,field)) or {}
        # Generic worker is the default launch transport role, not a gate identity.
        identities = [str(gate.get(key) or '').lower() for key in ('dispatch_role','role')]
        identities.append(str(gate.get('node') or gate.get('stage') or '').lower())
        expected = r'(?:review|reviewer|adversarial)' if field == 'review_task' else r'(?:test|tester|verification)'
        if not any(re.search(r'(?:^|[-_])'+expected+r'(?:$|[-_])',identity) for identity in identities):
            raise ValueError('delivery gate task has no matching review/test role')
        if (gate.get('workflow_id') != task['workflow_id'] or gate.get('stage_verdict') != 'pass'
                or gate.get('candidate_sha') != sha or gate.get('verified_candidate_sha') != sha or gate.get('status') not in {'completed','committed','integrated','cleanup_ready','cleaned'}):
            raise ValueError('delivery gate does not certify this workflow candidate')
    return sha, branch, task.get('base_branch') or _body_value(note,'base') or 'main'


def _matching_pr(rows, branch, base, sha):
    matches = []
    for row in rows:
        head, target = row.get('head') or {}, row.get('base') or {}
        if head.get('ref') == branch and target.get('ref') == base:
            if head.get('sha') != sha or row.get('state') != 'open':
                raise ValueError('existing PR candidate differs; refusing to overwrite')
            matches.append(row)
    if len(matches) > 1:
        raise ValueError('multiple PRs match delivery identity')
    return matches[0] if matches else None


def create_task_pr(store, task_id, *, title, body='', draft=False, client=None):
    if not title.strip() or len(title) > 256 or len(body.encode()) > 65536:
        raise ValueError('PR title/body exceeds publication budget')
    initial = store.get_task(task_id)
    if not initial:
        raise ValueError('task not found')
    with workflow_launch_lock(store.db_path, f"pr:{initial['workflow_id']}"):
        task = store.get_task(task_id)
        sha, branch, base = _publication(task, wd.load_notes(task['workflow_id']), store)
        repo = Path(task.get('source_repo') or '').expanduser()
        if not repo.is_dir():
            raise ValueError('integrated source repository missing')
        for ref in (branch, task.get('integration_ref')):
            if not ref or _git(repo,'rev-parse','--verify',f'{ref}^{{commit}}') != sha:
                raise ValueError('integrated ref differs from recorded candidate')
        _git(repo,'check-ref-format',f'refs/heads/{branch}')
        _git(repo,'check-ref-format',f'refs/heads/{base}')
        identity = repository_identity(_git(repo,'remote','get-url','origin'))
        push_urls = _git(repo,'remote','get-url','--push','--all','origin').splitlines()
        if len(push_urls) != 1 or repository_identity(push_urls[0]) != identity:
            raise ValueError('origin push repository differs from PR provider repository or has multiple targets')
        push_url = push_urls[0]
        client = client or PullRequestClient(*identity)
        rows = client.list_open(branch,base)
        existing = _matching_pr(rows,branch,base,sha)
        remote = _git(repo,'ls-remote',push_url,f'refs/heads/{branch}')
        if remote and remote.split()[0] != sha:
            raise ValueError('remote branch differs from integrated candidate')
        store.record_event('pull_request_intent', {'candidate_sha':sha,'branch':branch,'base':base,
            'provider':identity[0],'repository':'/'.join(identity[1:])},task_id=task_id,
            workflow_id=task['workflow_id'],source='herdr-task')
        if not remote:
            # Empty lease makes a concurrent remote branch creation fail safely.
            _git(repo,'push',f'--force-with-lease=refs/heads/{branch}:',push_url,f'{sha}:refs/heads/{branch}')
        fresh = store.get_task(task_id)
        if (fresh.get('version') != task.get('version') or
                _publication(fresh, wd.load_notes(task['workflow_id']), store) != (sha,branch,base)):
            raise ValueError('delivery changed during publication; PR creation withheld')
        row = existing or client.create(branch,base,title,body,draft)
        verified = _matching_pr([row],branch,base,sha)
        if not verified or not verified.get('number') or not verified.get('html_url'):
            raise ValueError('provider did not attest an open PR for the candidate')
        receipt = {'number':verified['number'],'url':verified['html_url'],'candidate_sha':sha,
                   'branch':branch,'base':base,'state':'open','provider':identity[0]}
        store.record_event('pull_request_created',receipt,task_id=task_id,
                           workflow_id=task['workflow_id'],source='herdr-task')
        return receipt


def merge_task_pr(store, task_id, *, pr_number=None, client=None, merge_method='merge'):
    """Merge an open PR after verifying its head matches the gate-certified candidate sha (§1.6)."""
    initial = store.get_task(task_id)
    if not initial:
        raise ValueError('task not found')
    with workflow_launch_lock(store.db_path, f"pr:{initial['workflow_id']}"):
        task = store.get_task(task_id)
        sha, branch, base = _publication(task, wd.load_notes(task['workflow_id']), store)
        repo = Path(task.get('source_repo') or '').expanduser()
        if not repo.is_dir():
            raise ValueError('integrated source repository missing')
        identity = repository_identity(_git(repo, 'remote', 'get-url', 'origin'))
        client = client or PullRequestClient(*identity)

        if pr_number is not None:
            pr = client.get_pr(pr_number)
        else:
            rows = client.list_open(branch, base)
            pr = _matching_pr(rows, branch, base, sha)
            if not pr:
                raise ValueError('no matching open PR found to merge')

        # §1.6 Fail-closed candidate sha verification: PR head MUST match gate-verified candidate sha
        pr_head_sha = str((pr.get('head') or {}).get('sha') or '').strip()
        if not pr_head_sha:
            raise ValueError('PR provider did not attest candidate head sha')

        from .scheduler import shas_identical
        if not shas_identical(pr_head_sha, sha):
            raise ValueError(
                f"PR head {pr_head_sha} differs from gate-certified candidate {sha}; "
                "refusing to merge unverified candidate"
            )

        target_number = pr.get('number') or pr_number
        client.merge(target_number, merge_method=merge_method)
        receipt = {
            'number': target_number,
            'candidate_sha': sha,
            'merged_sha': pr_head_sha,
            'branch': branch,
            'base': base,
            'merge_method': merge_method,
            'provider': identity[0],
            'status': 'merged',
        }
        store.record_event('pull_request_merged', receipt, task_id=task_id,
                           workflow_id=task['workflow_id'], source='herdr-task')
        return receipt
