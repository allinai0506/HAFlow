"""Capture actual source and execution identity for the existing loop receipt."""
import hashlib
import json
import os
from pathlib import Path
import re

from .bounded_tools import run_bounded
from .repo_hygiene import is_internal_untracked


def capture_evaluation_identity(target_dir):
    root = Path(target_dir).resolve()
    result = {'candidate_sha': None, 'source_fingerprint': None, 'epoch': None}
    head = run_bounded(['git', '-C', str(root), 'rev-parse', 'HEAD'], timeout=3, output_limit=4096)
    status = run_bounded(['git', '-C', str(root), 'status', '--porcelain=v1', '--untracked-files=all'], timeout=3)
    if head['status'] == status['status'] == 'completed' and head['exit_code'] == status['exit_code'] == 0:
        sha = head['stdout'].strip()
        # Only the evaluator's own generated workspace is outside candidate code.
        changes = [line for line in status['stdout'].splitlines()
                   if not (line.startswith('?? ') and is_internal_untracked(line[3:]))]
        if not changes and re.fullmatch(r'[0-9a-f]{40,64}', sha):
            result['candidate_sha'] = sha
        result['source_fingerprint'] = hashlib.sha256(json.dumps([sha, changes], sort_keys=True).encode()).hexdigest()
    task_id = os.environ.get('HERDR_TASK_ID')
    run_id = os.environ.get('HERDR_RUN_ID')
    epoch = os.environ.get('HERDR_COMPLETION_EPOCH')
    binding_root = os.environ.get('HERDR_EVALUATION_ROOT')
    # Advisory execution identity is injected by the validated parent. The tool
    # receives no database path or credential; the reader checks current identity.
    if task_id and run_id and epoch and binding_root and Path(binding_root).resolve() == root:
        result.update(task_id=task_id, run_id=run_id, epoch=epoch)
    return result


def evaluation_binding(before, after, *, command, test_exit, runner_exit, errors, test_output):
    unchanged = before == after and before.get('source_fingerprint')
    skipped = re.findall(r'(?:^|[, ])(\d+) skipped\b', test_output, flags=re.MULTILINE)
    return {'candidate_sha': before.get('candidate_sha') if unchanged else None,
            'epoch': before.get('epoch'), 'task_id': before.get('task_id'), 'run_id': before.get('run_id'),
            'source_fingerprint': before.get('source_fingerprint') if unchanged else None,
            'source_unchanged': bool(unchanged), 'execution_mode': 'unknown' if 'evaluator_start_failed' in errors else 'execute',
            'command': command, 'exit_code': test_exit, 'runner_exit_code': runner_exit,
            'skipped_tests': int(skipped[-1]) if skipped else None, 'environment': 'local'}
