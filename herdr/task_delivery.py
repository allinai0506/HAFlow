"""Run-bound repository delivery checks over pinned Task contracts and events."""
import fnmatch
import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path, PurePosixPath
import re
import shlex
import time

from . import state_db
from .bounded_tools import run_bounded, validate_tool_input
from .repo_hygiene import INTERNAL_EXACT, INTERNAL_PREFIXES
from .supervisor.state import redact_text
from .task_resources import workflow_launch_lock
from .workflow_docs import cli_path

MAX_FILES = 4096
MAX_CONTENT_BYTES = 64 * 1024 * 1024


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _relative(value):
    if (not isinstance(value, str) or not value or len(value) > 512 or '\x00' in value
            or '\\' in value or PurePosixPath(value).is_absolute() or '..' in PurePosixPath(value).parts
            or value.startswith(('.git/', '.herdr/', '.herdr-loop/')) or value == '.git'):
        raise ValueError('delivery path must be a bounded repository-relative path')
    return value


def validate_contract(contract):
    if not isinstance(contract, dict) or contract.get('version') != 1:
        raise ValueError('delivery contract requires version=1')
    if set(contract) - {'version', 'allowed_paths', 'required_files', 'checks', 'auto_rework'}:
        raise ValueError('unknown delivery contract field')
    if len(json.dumps(contract).encode()) > 65536:
        raise ValueError('delivery contract exceeds budget')
    allowed = contract.get('allowed_paths')
    if not isinstance(allowed, list) or not 1 <= len(allowed) <= 100:
        raise ValueError('delivery allowed_paths must explicitly declare the authorized scope')
    for path in allowed:
        _relative(path)
    required = contract.get('required_files', [])
    if not isinstance(required, list) or len(required) > 50:
        raise ValueError('delivery required_files exceeds budget')
    for item in required:
        if not isinstance(item, dict) or set(item) - {'path', 'headings'}:
            raise ValueError('invalid required_file')
        path = _relative(item.get('path'))
        if any(c in path for c in '*?['):
            raise ValueError('required_files must name exact files')
        headings = item.get('headings', [])
        if (not isinstance(headings, list) or len(headings) > 20
                or any(not isinstance(h, str) or not h.startswith('## ') or len(h) > 200 or '\n' in h for h in headings)):
            raise ValueError('invalid required headings')
    checks = contract.get('checks', [])
    if not isinstance(checks, list) or len(checks) > 8:
        raise ValueError('delivery checks exceeds budget')
    ids = set()
    for check in checks:
        if not isinstance(check, dict) or set(check) != {'id', 'argv', 'timeout'}:
            raise ValueError('checks require id, argv and timeout')
        if not isinstance(check['id'], str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', check['id']) or check['id'] in ids:
            raise ValueError('invalid or duplicate check id')
        ids.add(check['id'])
        validate_tool_input(check['argv'], timeout=check['timeout'])
    if sum(c['timeout'] for c in checks) > 120:
        raise ValueError('delivery check total timeout exceeds 120 seconds')
    if type(contract.get('auto_rework', False)) is not bool:
        raise ValueError('auto_rework must be boolean')
    return contract


def scope_conflicts(contract):
    validate_contract(contract)
    return [item['path'] for item in contract.get('required_files', [])
            if not any(fnmatch.fnmatchcase(item['path'], pattern) for pattern in contract['allowed_paths'])]


def writable_task(task):
    role = ' '.join(str(task.get(k) or '') for k in ('dispatch_role', 'agent_role', 'role')).lower()
    return (task.get('integration_mode') == 'git' and task.get('execution_mode') != 'context'
            and (task.get('node') or task.get('stage')) not in {'test', 'review'}
            and not re.search(r'(?:^|[^a-z])(review(?:er)?|adversarial|read.?only)(?:$|[^a-z])', role))


_FIX_TOKEN_RE = re.compile(r'(?:^|[^a-z])(fix|bugfix|hotfix)(?:[^a-z]|$)', re.IGNORECASE)
_RETROSPECTIVE_RE = re.compile(r'bug|report|postmortem|retrospective|复盘|review|root.?cause|防复发', re.IGNORECASE)


def _fix_token_hit(value):
    return bool(value and _FIX_TOKEN_RE.search(str(value)))


def fix_task_needs_retrospective(task_id=None, task_type=None, node_id=None, branch=None, label=None):
    """Heuristic: word-boundary fix/bugfix/hotfix in any naming signal."""
    return any(_fix_token_hit(value) for value in (task_id, task_type, node_id, branch, label))


def retrospective_satisfied(contract):
    try:
        required = (contract or {}).get('required_files') or []
    except (AttributeError, TypeError):
        return False
    for item in required:
        if _RETROSPECTIVE_RE.search(str((item or {}).get('path') or '')):
            return True
    return False


def validate_fix_retrospective(task_id=None, task_type=None, node_id=None, branch=None,
                               label=None, contract=None, integration_mode=None):
    """Plan/dispatch gate: fix-like git work must pin a retrospective doc."""
    if str(integration_mode or '').lower() != 'git':
        return None
    if not fix_task_needs_retrospective(task_id=task_id, task_type=task_type,
                                         node_id=node_id, branch=branch, label=label):
        return None
    if contract is None:
        return None
    if retrospective_satisfied(contract):
        return None
    raise ValueError('fix task requires retrospective delivery contract: register a postmortem/复盘 '
                     'required_files entry (e.g. docs/bug-reports/<date>-<slug>.md) in the node '
                     'delivery_contract before dispatch')


def instruction_block(task):
    if not writable_task(task):
        return ''
    contract = task.get('delivery_contract')
    text = ('\nRepository delivery requirements:\n'
            'Read the worker repository AGENTS.md and applicable local rules, actual commit/CI gates, and templates before editing.\n'
            'Reconcile required code, regression tests, engineering documents, and validation evidence with the authorized file scope.\n'
            'A scope conflict is a blocker for the coordinator to adjudicate; do not widen scope or bypass hooks.\n'
            'A self-test score is not repository delivery. Never declare a failed or unknown check successful.\n')
    if contract is None:
        return text + 'No machine delivery contract is configured; report unverified repository delivery requirements explicitly.\n'
    validate_contract(contract)
    return (text + 'Pinned delivery contract (authorized scope and required outputs):\n'
            + json.dumps({**contract, 'checks': [{'id': c['id'], 'timeout': c['timeout']} for c in contract.get('checks', [])]}, ensure_ascii=False) + '\nBefore report-completion run:\n'
            + shlex.quote(str(cli_path())) + ' delivery-check ' + shlex.quote(task['task_id'])
            + '\nOnly a fresh ready receipt permits completion. Files or staged content changing invalidates it.\n')


def diagnostic_tail(stdout, stderr, limit=4096):
    # Redact complete available records before selecting their tail.
    return redact_text((stdout or '') + '\n' + (stderr or ''))[-limit:]


def _git(root, *args):
    try:
        result = run_bounded(['git', '-C', str(root), *args], timeout=10, output_limit=1024 * 1024, redact_output=False)
    except UnicodeDecodeError as exc:
        raise ValueError('delivery Git metadata is not valid UTF-8') from exc
    if result['status'] != 'completed' or result['exit_code']:
        raise ValueError('delivery repository snapshot unavailable')
    return result['stdout']


def _file_hash(path, budget):
    if not path.is_file() or path.is_symlink():
        return None
    before = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            budget[0] += len(chunk)
            if budget[0] > MAX_CONTENT_BYTES:
                raise ValueError('delivery content exceeds snapshot budget')
            digest.update(chunk)
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError('delivery file changed during snapshot')
    return digest.hexdigest()


def repository_snapshot(task):
    root = Path(task.get('clone_path') or '').resolve()
    if not task.get('clone_path') or not root.is_dir():
        raise ValueError('delivery clone unavailable')
    baseline = task.get('baseline_commit')
    if not isinstance(baseline, str) or not re.fullmatch(r'[0-9a-f]{40}', baseline):
        raise ValueError('delivery baseline unknown')
    if Path(_git(root, 'rev-parse', '--show-toplevel').strip()).resolve() != root:
        raise ValueError('delivery requires the registered repository root')
    changed = _git(root, 'diff', '--name-only', '-z', baseline, '--').split('\0')
    inherited = set(task.get('baseline_untracked') or [])
    changed += [p for p in _git(root, 'ls-files', '--others', '--exclude-standard', '-z').split('\0')
                if p and p not in inherited and p not in INTERNAL_EXACT and not p.startswith(INTERNAL_PREFIXES)]
    changed = sorted(set(p for p in changed if p))
    if len(changed) > MAX_FILES:
        raise ValueError('delivery file count exceeds snapshot budget')
    budget = [0]
    files = {}
    for name in changed:
        _relative(name)
        path = root / name
        if not path.resolve().is_relative_to(root) or path.is_symlink():
            files[name] = 'unsafe'
        else:
            files[name] = _file_hash(path, budget)
    rules = {}
    for name in ('AGENTS.md', 'CLAUDE.md', 'RULES.md'):
        path = root / name
        if path.is_symlink():
            raise ValueError('delivery rules symlink is not supported')
        rules[name] = _file_hash(path, budget)
    hooks_setting = run_bounded(['git', '-C', str(root), 'config', '--get', 'core.hooksPath'], timeout=10, redact_output=False)
    if hooks_setting['status'] != 'completed' or hooks_setting['exit_code'] not in (0, 1):
        raise ValueError('delivery hook configuration unknown')
    hookpath = hooks_setting['stdout'].strip()
    git_dir = Path(_git(root, 'rev-parse', '--absolute-git-dir').strip()).resolve()
    rules['.git/config'] = _file_hash(git_dir / 'config', budget)
    hooks = (root / hookpath).resolve() if hookpath else git_dir / 'hooks'
    if not hooks.is_relative_to(root):
        raise ValueError('delivery hooks outside registered clone')
    if hooks.exists():
        for path in hooks.rglob('*'):
            if len(rules) > MAX_FILES:
                raise ValueError('delivery hook files exceed budget')
            if path.is_symlink():
                raise ValueError('delivery hook symlink is not supported')
            if path.is_file():
                rules[str(path.relative_to(root))] = _file_hash(path, budget)
    body = {'head': _git(root, 'rev-parse', 'HEAD').strip(),
            'branch': _git(root, 'branch', '--show-current').strip(), 'files': files, 'rules': rules,
            'index': _git(root, 'diff', '--cached', '--raw', '--no-renames', '--abbrev=40', '-z', baseline, '--'),
            'contract': validate_contract(task['delivery_contract'])}
    return {'fingerprint': _digest(body), 'files': files, 'head': body['head'], 'branch': body['branch']}


def _identity(task):
    return {k: task.get(k) for k in ('task_id', 'workflow_id', 'run_id', 'execution_id', 'completion_epoch', 'clone_path', 'version')}


def _issues(contract, snapshot, root):
    allowed = contract['allowed_paths']
    issues = []
    for name in snapshot['files']:
        if snapshot['files'][name] == 'unsafe':
            issues.append({'code': 'unsafe_path', 'path': name})
        if not any(fnmatch.fnmatchcase(name, pattern) for pattern in allowed):
            issues.append({'code': 'outside_scope', 'path': name})
    for item in contract.get('required_files', []):
        name = item['path']
        if not any(fnmatch.fnmatchcase(name, pattern) for pattern in allowed):
            issues.append({'code': 'scope_conflict', 'path': name})
            continue
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            issues.append({'code': 'unsafe_path', 'path': name})
        elif snapshot['files'].get(name) in (None, 'unsafe') or not path.is_file():
            issues.append({'code': 'required_file_missing', 'path': name})
        elif path.stat().st_size > 1024 * 1024:
            issues.append({'code': 'required_file_budget', 'path': name})
        else:
            lines = path.read_text(errors='replace').splitlines()
            for heading in item.get('headings', []):
                if heading not in lines:
                    issues.append({'code': 'required_section_missing', 'path': name, 'heading': heading})
                    continue
                section = lines[lines.index(heading) + 1:]
                end = next((i for i, line in enumerate(section) if line.startswith('#')), len(section))
                if not any(line.strip() for line in section[:end]):
                    issues.append({'code': 'required_section_empty', 'path': name, 'heading': heading})
    return issues


def check_delivery(task_id, *, store):
    with workflow_launch_lock(store.db_path, 'delivery:' + task_id):
        task = store.get_task(task_id)
        if not task or not task.get('run_id'):
            raise ValueError('delivery task identity unknown')
        if not writable_task(task):
            raise ValueError('read-only task cannot run repository delivery checks')
        if task.get('status') not in {'pending', 'dispatched', 'working', 'rework', 'agent_done', 'completed'}:
            raise ValueError('delivery task not eligible')
        workflow = store.get_workflow(task.get('workflow_id')) or {}
        if workflow.get('status') != 'running' or workflow.get('execution_id') != task.get('execution_id'):
            raise ValueError('delivery workflow identity inactive or unknown')
        contract = validate_contract(task.get('delivery_contract'))
        conn = state_db.get_readonly_db_connection(store.db_path)
        try:
            previous = conn.execute("SELECT event_type,payload_json FROM events WHERE task_id=? AND run_id=? AND event_type IN ('delivery_check_started','delivery_checked') AND source='delivery-check' ORDER BY id DESC LIMIT 1",
                                    (task_id, task['run_id'])).fetchone()
        finally:
            conn.close()
        if previous:
            prior = json.loads(previous['payload_json'])
            if (prior.get('completion_epoch') == task.get('completion_epoch')
                    and (previous['event_type'] == 'delivery_check_started' or prior.get('status') == 'unknown')):
                raise ValueError('delivery prior check outcome unknown; coordinator verification required')
        before = repository_snapshot(task)
        issues = _issues(contract, before, Path(task['clone_path']).resolve())
        checks = []
        status = 'blocked' if issues else 'ready'
        operation_id = None
        if not issues and contract.get('checks'):
            operation_id = uuid.uuid4().hex
            store.record_event('delivery_check_started', {**_identity(task), 'operation_id': operation_id,
                               'fingerprint': before['fingerprint'], 'status': 'started'},
                               task_id=task_id, workflow_id=task['workflow_id'], run_id=task['run_id'], source='delivery-check')
        for check in ([] if issues else contract.get('checks', [])):
            env = dict(os.environ)
            for key in ('HERDR_STATE_DB', 'HERDR_CONTROLLER_DIR', 'TASKS_FILE', 'WORKFLOWS_FILE',
                        'STAGE_STATE_FILE', 'CHECKPOINTS_DIR', 'WORKFLOW_FILE',
                        'JEV_API_KEY', 'TYPESAFE_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY'):
                env.pop(key, None)
            env.update(HERDR_TASK_ID=task_id, HERDR_RUN_ID=task['run_id'],
                       HERDR_OBSERVER_ENABLED='0', HERDR_OBSERVER_JEV_ENABLED='0')
            with tempfile.TemporaryDirectory(prefix='herdr-delivery-home-') as home:
                env['HOME'] = home
                result = run_bounded(check['argv'], cwd=task['clone_path'], timeout=check['timeout'], output_limit=65536, env=env)
            checks.append({'id': check['id'], 'argv_sha256': _digest(check['argv']), 'status': result['status'],
                           'exit_code': result['exit_code'], 'detail': diagnostic_tail(result['stdout'], result['stderr'])})
            if result['status'] != 'completed':
                status = 'unknown'
                break
            if result['exit_code']:
                status = 'blocked'
                issues.append({'code': 'check_failed', 'check_id': check['id']})
                break
        after = repository_snapshot(task)
        if before['fingerprint'] != after['fingerprint']:
            status = 'unknown'
            issues.append({'code': 'repository_changed'})
        issues = [{k: redact_text(v) if isinstance(v, str) else v for k, v in issue.items()} for issue in issues]
        receipt = {**_identity(task), 'status': status, 'operation_id': operation_id, 'fingerprint': before['fingerprint'],
                   'head': before['head'], 'issues': issues, 'checks': checks, 'checked_at': time.time()}
        conn = state_db.get_db_connection(store.db_path)
        try:
            conn.execute('BEGIN IMMEDIATE')
            fresh = state_db._decode_task_row(conn.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone())
            workflow_row = conn.execute('SELECT status,metadata_json FROM workflows WHERE workflow_id=?', (task['workflow_id'],)).fetchone()
            if (_identity(fresh) != _identity(task) or not workflow_row or workflow_row['status'] != 'running'
                    or json.loads(workflow_row['metadata_json']).get('execution_id') != task.get('execution_id')):
                raise ValueError('delivery task or workflow changed during check')
            state_db.record_event({'event_type': 'delivery_checked', 'source': 'delivery-check',
                                   'workflow_id': task['workflow_id'], 'task_id': task_id, 'run_id': task['run_id'],
                                   'node_id': task.get('node') or task.get('stage'), 'payload': receipt}, conn=conn)
            conn.commit()
        finally:
            conn.close()
        return receipt


def receipt_is_current(conn, task, receipt):
    row = conn.execute("SELECT event_type,payload_json FROM events WHERE task_id=? AND run_id=? AND workflow_id=? AND event_type IN ('delivery_checked','delivery_check_started') AND source='delivery-check' ORDER BY id DESC LIMIT 1",
                       (task['task_id'], task.get('run_id'), task.get('workflow_id'))).fetchone()
    return bool(row and row['event_type'] == 'delivery_checked' and json.loads(row['payload_json']) == receipt)


def require_delivery(task, store):
    if task.get('delivery_contract') is None or not writable_task(task):
        return None
    snapshot = repository_snapshot(task)
    conn = state_db.get_readonly_db_connection(store.db_path)
    try:
        row = conn.execute("SELECT payload_json FROM events WHERE task_id=? AND run_id=? AND workflow_id=? AND event_type IN ('delivery_checked','delivery_check_started') AND source='delivery-check' ORDER BY id DESC LIMIT 1",
                           (task['task_id'], task.get('run_id'), task.get('workflow_id'))).fetchone()
    finally:
        conn.close()
    receipt = json.loads(row[0]) if row else {}
    if (receipt.get('status') != 'ready' or receipt.get('fingerprint') != snapshot['fingerprint']
            or any(receipt.get(k) != v for k, v in _identity(task).items())):
        raise ValueError('delivery not ready: run delivery-check and resolve its issues before completion')
    return receipt


def record_delivery_failure(task, receipt, *, store):
    """CAS-bind a real check refusal to its source run in the same event transaction."""
    if (receipt.get('status') != 'blocked' or any(receipt.get(k) != v for k, v in _identity(task).items())):
        return False
    conn = state_db.get_db_connection(store.db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM tasks WHERE task_id=?', (task['task_id'],)).fetchone()
        fresh = state_db._decode_task_row(row) if row else {}
        workflow = conn.execute('SELECT status,metadata_json FROM workflows WHERE workflow_id=?', (task['workflow_id'],)).fetchone()
        if (_identity(fresh) != _identity(task) or fresh.get('status') != 'completed'
                or not receipt_is_current(conn, task, receipt)
                or not workflow or workflow['status'] != 'running'
                or json.loads(workflow['metadata_json']).get('execution_id') != task.get('execution_id')):
            return False
        state_db.update_task_metadata(task['task_id'], {'delivery_failure': receipt,
            'finalize_escalated': True, 'finalize_escalate_reason': 'delivery_incomplete'}, conn=conn)
        state_db.record_event({'event_type': 'finalize_escalated', 'source': 'controller',
            'workflow_id': task['workflow_id'], 'task_id': task['task_id'], 'run_id': task['run_id'],
            'node_id': task.get('node') or task.get('stage'),
            'payload': {'reason': 'delivery_incomplete', 'status': 'completed', 'detail': receipt}}, conn=conn)
        conn.commit()
        return True
    finally:
        conn.close()
