"""Single-tool native evidence adapter; observing never changes task verdicts.

Trusts host-owned native records, not Agent reports. No native format currently
proves browser actions + runtime candidate + business assertions/readback, so
this adapter deliberately cannot emit a satisfied result.
"""
import logging
import math
import shlex
import threading
from pathlib import Path

from herdr import state_db
from herdr.execution_obligations import IDENTITY, verify_executions
from herdr.native_session_trace import load_native_actions
from herdr.observation import ObservationStore
from herdr.trajectory import TrajectoryLedger, record_observation_created, record_trajectory_event

LOGGER = logging.getLogger(__name__)


def _direct_nodejs(command):
    # Only a single direct CLI process is attributable. No shell parsing guesses
    # for pipelines, nested wrappers, redirects, substitutions, or command chains.
    if not isinstance(command, str):
        return False
    quote, escaped = None, False
    for char in command:
        if escaped:
            escaped = False
            continue
        if quote == "'":
            if char == "'":
                quote = None
            continue
        if char == "\\":
            escaped = True
        elif quote == '"':
            if char == '"':
                quote = None
            elif char in '$`':
                return False
        elif char in "\"'":
            quote = char
        elif char in '\n\r;|&<>()`$#':
            return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    return len(argv) >= 2 and argv[:2] == ['ego-browser', 'nodejs']


def native_ego_facts(task, actions):
    """Normalize only actual native tool receipts scoped to this attempt.

    Never inspect summary/report content or infer JS browser actions from code.
    Missing timestamps/receipts are not evidence of successful invocation.
    """
    start = max(float(task.get('created_at') or 0), float(task.get('started_at') or 0),
                float(task.get('completion_epoch_started_at') or 0))
    facts = []
    for action in actions:
        timestamp = action.get('timestamp')
        if (not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool)
                or not math.isfinite(timestamp) or timestamp < start
                or not action.get('session_id') or not action.get('source_ref')
                or not _direct_nodejs(action.get('command'))):
            continue
        provider = str(action['source_ref']).split(':', 1)[0]
        shell_tool = {'codex': 'exec_command', 'claude': 'Bash', 'opencode': 'bash'}.get(provider)
        receipt = action.get('ego_native_receipt') or {}
        shell_receipt = (shell_tool is not None and action.get('tool') == shell_tool
                         and receipt.get('provider') == provider)
        verified = shell_receipt and receipt.get('outcome') in {'success', 'failed'}
        # Inferred wrappers and admission/unknown responses are hints only;
        # a native tool response alone does not prove process launch.
        if not shell_receipt and not (provider == 'codex' and action.get('tool') == 'exec'):
            continue
        facts.append({**{k: task.get(k) for k in IDENTITY}, 'tool': 'ego-browser',
                      'source': 'native-session-v1', 'invoked': verified,
                      'outcome': receipt.get('outcome', 'unknown') if verified else 'unknown',
                      'evidence_refs': [action['source_ref']], 'timestamp': timestamp,
                      'evidence_verified': False})
    return facts


def observe_task(task, db_path, *, loader=None):
    """Load authoritative task and native facts, store redacted evidence + result.

    db_path is explicit: never fall back to the production DB. Returned results
    are process telemetry, never inputs to gate pass/blocked or retry decisions.
    Exceptions are contained by observe_task_best_effort at the Controller edge.
    """
    current = state_db.get_task(task.get('task_id'), db_path=db_path)
    if not current or not current.get('execution_contract_v1'):
        return None
    contract = current['execution_contract_v1']
    available = True
    facts = []
    try:
        # Existing adapter verifies cwd/native-session ownership against siblings.
        siblings = state_db.list_tasks(db_path=db_path, limit=10001)
        if len(siblings) > 10000:
            raise ValueError("task ownership scan exceeds budget")
        actions, notes = (loader or load_native_actions)(current, siblings, budget_s=0.25)
        available = not notes
        facts = native_ego_facts(current, actions)
    except Exception:
        available = False
    result = verify_executions(contract, facts, available=available)
    if any(current.get(k) != contract.get(k) for k in IDENTITY):
        result.update(status='unverifiable', reason='contract_identity_changed')
    # No commands, scripts, output tails or screenshots are copied from the
    # native session. Store scoped receipt references and bounded facts only.
    body = {'contract': contract, 'facts': facts, 'result': result}
    store = ObservationStore(Path(db_path))
    observation, created = store.create_with_status(
        run_id=current['run_id'], task_id=current['task_id'], workflow_id=current['workflow_id'],
        source_type='verification', source_ref='ego-execution-v1:' + contract['contract_sha256'],
        content=body, media_type='application/json')
    ledger = TrajectoryLedger(Path(db_path))
    indexed = record_observation_created(current, observation, ledger=ledger)
    # Unique Observation receipts provide the index. No history scan on retry;
    # checked is a best-effort diagnostic, not an accounting record.
    if created or indexed:
        record_trajectory_event(current, 'execution_obligations_checked', ledger=ledger,
                                metadata={'observation_id': observation.observation_id, **result})
    return result


def observe_task_best_effort(task, db_path):
    try:
        return observe_task(task, db_path)
    except Exception:
        # Don't expose sensitive provider/DB error text in diagnostics.
        LOGGER.warning('ego execution obligation observation unavailable')
        return {'status': 'unverifiable', 'reason': 'observation_unavailable', 'mode': 'observe'}


_SCHEDULER = None
_SCHEDULER_LOCK = threading.Lock()


def submit_ego_observation(task, db_path, *, scheduler=None):
    """Use the existing bounded Observer dispatcher; never read logs in poller."""
    if not task or not task.get('execution_contract_v1') or not task.get('run_id'):
        return False
    from herdr.observer.harness import ObservationScheduler
    global _SCHEDULER
    # The existing dispatcher locks admission and bounds inflight workers.
    # This lock only protects lazy process-local dispatcher initialization.
    if scheduler is None:
        with _SCHEDULER_LOCK:
            if _SCHEDULER is None:
                _SCHEDULER = ObservationScheduler(
                    observe=lambda run_id, task, store, config: observe_task_best_effort(task, store),
                    config={'enabled': True, 'interval': 30, 'max_calls_per_run': 24},
                    max_concurrent=1)
            scheduler = _SCHEDULER
    return scheduler.submit(str(Path(db_path).resolve()) + ':' + task['run_id'],
                            task=dict(task), store=Path(db_path))


def main():
    """Explicit DB CLI for inspecting a task without adopting a business verdict."""
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--task', required=True)
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error('database must already exist')
    task = state_db.get_task(args.task, db_path=args.db)
    if not task:
        parser.error('unknown task')
    print(json.dumps(observe_task_best_effort(task, args.db), ensure_ascii=False))


if __name__ == '__main__':
    main()
