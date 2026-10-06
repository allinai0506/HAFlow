"""Pure workflow blockers and recovery obligations, independent of DAG readiness."""

import hashlib
import json
import re

_DEFAULT_GATES = {'test': 'implementation', 'review': 'implementation', 'wrapup': 'implementation'}
_FACT_FIELDS = ('task_id', 'workflow_id', 'run_id', 'execution_id', 'workflow_run_id',
                'node', 'stage', 'candidate_sha', 'commit', 'stage_verdict', 'stage_verdict_note',
                'blocker', 'finalize_escalated', 'finalize_escalate_reason',
                'affected_task_ids', 'stage_verdict_affected_task_ids', 'supersedes', 'parent_task_id')


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _generation(workflow):
    return [workflow.get('execution_id') or workflow.get('created_at'), workflow.get('reopened_at')]


def recovery_identity(workflow, facts):
    """Hash semantic recovery facts; activity timestamps and lifecycle versions are excluded."""
    normalized = []
    for fact in facts:
        value = {key: fact.get(key) for key in _FACT_FIELDS}
        value['affected_task_ids'] = sorted(set(value.get('affected_task_ids') or []))
        normalized.append(value)
    normalized.sort(key=lambda value: json.dumps(value, sort_keys=True))
    return _hash({'workflow_id': workflow.get('workflow_id'), 'generation': _generation(workflow),
                  'candidate_sha': workflow.get('candidate_sha'), 'facts': normalized})


def active_tasks(workflow, tasks):
    """Keep current lineage; legacy unscoped tasks remain visible but cannot automate."""
    return [task for task in tasks
            if (not workflow.get('workflow_id') or task.get('workflow_id') in (None, '', workflow.get('workflow_id')))
            and task.get('status') != 'superseded' and not task.get('superseded_by')]


def _blocking(task):
    return (task.get('stage_verdict') == 'blocked'
            or task.get('status') in {'blocked', 'failed', 'rework', 'interrupted'}
            or bool(str(task.get('blocker') or '').strip())
            or bool(task.get('finalize_escalated')))


def _gate(nodes, node_id):
    node = nodes.get(node_id)
    if node is None:
        return None
    if 'gate' in node:
        gate = node['gate']
        if gate is False:
            return None
        if gate:
            return gate.get('retry_node', 'implementation') if isinstance(gate, dict) else None
    return _DEFAULT_GATES.get(node_id)


def _identity_reason(workflow, facts, candidate):
    current = workflow.get('candidate_sha')
    if not candidate or not current:
        return 'candidate_unknown'
    if not all(isinstance(sha, str) and re.fullmatch(r'[0-9a-fA-F]{40}', sha) for sha in (candidate, current)):
        return 'candidate_invalid'
    if candidate != current:
        return 'candidate_mismatch'
    if not workflow.get('workflow_id') or not workflow.get('execution_id'):
        return 'identity_unknown'
    for fact in facts:
        if (workflow.get('execution_id')
                and fact.get('execution_id') != workflow['execution_id']):
            return 'identity_unknown'
        if (fact.get('candidate_sha') or fact.get('commit')) != candidate:
            return 'candidate_mismatch' if (fact.get('candidate_sha') or fact.get('commit')) else 'candidate_unknown'
        if (fact.get('workflow_id') != workflow.get('workflow_id') or not fact.get('task_id')
                or not fact.get('run_id')):
            return 'identity_unknown'
    return None


def assess_workflow(workflow, config, tasks):
    """Return original blocking tasks plus fail-closed grouped recovery decisions."""
    current = active_tasks(workflow, tasks)
    def relevant(task):
        # Historical gate verdicts are immutable, but are not current failures.
        candidate = task.get('candidate_sha')
        return not (task.get('stage_verdict') == 'blocked' and not task.get('finalize_escalated')
                    and candidate and workflow.get('candidate_sha')
                    and re.fullmatch(r'[0-9a-fA-F]{40}', str(candidate))
                    and candidate != workflow['candidate_sha'])
    blockers = [task for task in current if _blocking(task) and relevant(task)]
    nodes = {node.get('id'): node for node in (config or {}).get('nodes', [])}
    groups = {}
    for task in blockers:
        if (task.get('status') == 'rework' and task.get('stage_verdict') != 'blocked'
                and not task.get('blocker') and not task.get('finalize_escalated')):
            continue  # In-flight repair waits; it is not a new failed gate.
        node_id = task.get('node') or task.get('stage')
        kind = 'finalize' if task.get('finalize_escalated') else 'fix_loop'
        retry = node_id if kind == 'finalize' else _gate(nodes, node_id)
        key = (kind, retry, (task.get('candidate_sha') or task.get('commit')))
        groups.setdefault(key, []).append(task)
    obligations = []
    for (kind, retry, candidate), failed in groups.items():
        affected = set()
        for task in failed:
            affected.update(task.get('affected_task_ids') or [])
            affected.update(task.get('stage_verdict_affected_task_ids') or [])
        if not affected:
            affected.update(task['task_id'] for task in current
                            if task.get('task_id') and (task.get('node') or task.get('stage')) == retry
                            and (task.get('candidate_sha') or task.get('commit')) == candidate)
        facts = sorted(failed, key=lambda task: str(task.get('task_id') or ''))
        affected_facts = [task for task in current if task.get('task_id') in affected and task not in facts]
        reason = _identity_reason(workflow, facts + affected_facts, candidate)
        if reason is None and affected - {task.get('task_id') for task in facts + affected_facts}:
            reason = 'identity_unknown'
        if reason is None and (not retry or retry not in nodes):
            reason = 'gate_unknown'
        if reason is None and kind == 'finalize':
            reason = 'finalize_escalated'
        if reason is None and (workflow.get('status') != 'running' or workflow.get('startup_ready') is False):
            reason = 'workflow_inactive'
        if reason is None and kind == 'fix_loop':
            from .node_capacity import launch_capacity_error, node_usage
            verifiers = [n for n in nodes.values() if n.get('id') != 'wrapup'
                         and _gate(nodes, n.get('id')) == retry]
            if any(launch_capacity_error(node_usage(n, tasks, workflow.get('workflow_id')))
                   for n in verifiers):
                reason = 'verifier_budget_exhausted'
        if reason is None and kind == 'fix_loop' and not affected:
            reason = 'affected_tasks_unknown'
        identity_facts = [{**{key: task.get(key) for key in _FACT_FIELDS},
                           'affected_task_ids': sorted(affected)} for task in facts + affected_facts]
        slot = {'workflow_id': workflow.get('workflow_id'), 'generation': _generation(workflow),
                'candidate_sha': candidate, 'retry_node': retry, 'kind': kind}
        obligations.append({'kind': kind, 'retry_node': retry,
                            'gate_nodes': sorted({task.get('node') or task.get('stage') or '' for task in failed}),
                            'task_ids': sorted({task.get('task_id') for task in failed if task.get('task_id')}),
                            'candidate_sha': candidate, 'affected_task_ids': sorted(affected),
                            'status': 'waiting_human' if reason else 'pending',
                            'reason': reason or 'blocked_gate', 'facts': identity_facts,
                            'identity': recovery_identity(workflow, identity_facts), 'identity_key': _hash(slot)})
    obligations.sort(key=lambda obligation: obligation['identity_key'])
    return {'blockers': blockers, 'obligations': obligations, 'can_advance': not blockers}
