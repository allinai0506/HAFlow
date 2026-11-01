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
        if fact.get('integrated_commit'):
            value['integrated_commit'] = fact['integrated_commit']
        if 'delivery_failure' in fact:
            value.update(delivery_failure=fact['delivery_failure'], delivery_rework_attempts=fact.get('delivery_rework_attempts'))
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
    # A second blocked delivery on a non-gate node is a known limit. The
    # verdict stays blocked; it just no longer stops the next node.
    if task.get('accepted_with_limits') and not task.get('finalize_escalated'):
        return False
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
        if gate is True:
            return _DEFAULT_GATES.get(node_id) or 'implementation'
        if isinstance(gate, dict):
            return gate.get('retry_node') or gate.get('retry_target') or 'implementation'
        if isinstance(gate, str):
            return gate
        return None
    return _DEFAULT_GATES.get(node_id)


# Launch-phase refusals never started an agent run; they are transport
# bookkeeping, not execution facts.
_LAUNCH_REFUSAL_REASONS = frozenset({
    'router_isolation_rejected', 'router_policy_rejected', 'router_health_rejected',
    'launch_preallocation_rejected', 'baseline_stale',
})


def _launch_refused(task):
    """True for zero-resource launch-refusal rows (no agent ever started).

    wf-haflow-1022-01: a router-refused registration row (no execution_id)
    joined a watchdog-killed task in one fix_loop group and flipped the whole
    group's identity to ``identity_unknown`` -> waiting_human. Launch refusals
    are not quality facts and must not poison identity validation; they are
    owned by the router-recovery / manual-relaunch paths.
    """
    return str(task.get('failure_reason') or '') in _LAUNCH_REFUSAL_REASONS


def candidate_responsible_source(workflow, tasks, candidate_sha, retry_node=None):
    """Identify the legitimate candidate producer node and task(s).

    Returns (source_node, target_task_ids).
    When the candidate is produced by wrapup:
      returns ('wrapup', [wrapup_task_id])
    When the candidate is produced by an implementation/craft node:
      returns (source_node, [impl_task_ids...])
    Otherwise returns (None, []).
    """
    if not candidate_sha or not isinstance(candidate_sha, str):
        return None, []
    if not re.fullmatch(r'[0-9a-fA-F]{40}', candidate_sha):
        return None, []

    wid = workflow.get('workflow_id')
    eid = workflow.get('execution_id')

    # 1. Check wrapup producer
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        if wid and task.get('workflow_id') not in (None, '', wid):
            continue
        if eid and task.get('execution_id') != eid:
            continue
        if (task.get('node') or task.get('stage')) != 'wrapup':
            continue
        if task.get('status') == 'superseded' or task.get('superseded_by'):
            continue
        if not task.get('run_id'):
            continue
        delivery = task.get('wrapup_delivery') or {}
        matches_candidate = (
            task.get('integrated_commit') == candidate_sha
            or delivery.get('integrated_sha') == candidate_sha
            or task.get('commit') == candidate_sha
        )
        if matches_candidate:
            if delivery:
                if eid and delivery.get('execution_id') != eid:
                    continue
                if delivery.get('run_id') != task.get('run_id'):
                    continue
            return 'wrapup', [task['task_id']]

    # 2. Check implementation producer (supporting dynamically named nodes like 'craft')
    impl_targets = []
    matched_nodes = set()
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        if wid and task.get('workflow_id') not in (None, '', wid):
            continue
        if eid and task.get('execution_id') != eid:
            continue
        task_node = task.get('node') or task.get('stage')
        if task_node in ('wrapup', 'test', 'review', 'acceptance'):
            continue
        if retry_node and task_node not in (retry_node, 'implementation'):
            continue
        if task.get('status') == 'superseded' or task.get('superseded_by'):
            continue
        if not task.get('run_id'):
            continue
        delivery = task.get('wrapup_delivery') or {}
        matches_candidate = (
            task.get('commit') == candidate_sha
            or task.get('integrated_commit') == candidate_sha
            or delivery.get('integrated_sha') == candidate_sha
        )
        if matches_candidate:
            impl_targets.append(task['task_id'])
            if task_node:
                matched_nodes.add(task_node)
    if impl_targets:
        source_name = retry_node if (retry_node and retry_node in matched_nodes) else (
            list(matched_nodes)[0] if len(matched_nodes) == 1 else 'implementation'
        )
        return source_name, sorted(impl_targets)

    # 3. Check workflow snapshot candidate_source_node
    if workflow.get('candidate_source_node') == 'wrapup' and workflow.get('candidate_sha') == candidate_sha:
        for task in tasks or []:
            if isinstance(task, dict) and (task.get('node') or task.get('stage')) == 'wrapup':
                if (not wid or task.get('workflow_id') in (None, '', wid)) and (not eid or task.get('execution_id') == eid):
                    if task.get('status') != 'superseded' and not task.get('superseded_by') and task.get('run_id'):
                        return 'wrapup', [task['task_id']]

    return None, []


def gate_retry_node(config, node_id):
    """Public gate accessor: the node a gate retries ('' when not a gate).

    The single definition of "which node ids are gates" lives here: an explicit
    ``gate`` contract on the node, or the ``_DEFAULT_GATES`` fallback.  Callers
    that must not restate that rule (e.g. the delivery-pipeline projection)
    ask this function instead.
    """
    nodes = {node.get('id'): node for node in (config or {}).get('nodes') or []
             if isinstance(node, dict) and node.get('id')}
    return _gate(nodes, node_id) or ''


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
        cand_values = {
            fact.get('candidate_sha'),
            fact.get('commit'),
            fact.get('integrated_commit'),
            (fact.get('wrapup_delivery') or {}).get('integrated_sha'),
        } - {None, ''}
        if candidate not in cand_values:
            return 'candidate_mismatch' if cand_values else 'candidate_unknown'
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
    blockers = [task for task in current if _blocking(task) and relevant(task)
                and not _launch_refused(task)]
    nodes = {node.get('id'): node for node in (config or {}).get('nodes', [])}
    groups = {}
    for task in blockers:
        if (task.get('status') == 'rework' and task.get('stage_verdict') != 'blocked'
                and not task.get('blocker') and not task.get('finalize_escalated')):
            continue  # In-flight repair waits; it is not a new failed gate.
        node_id = task.get('node') or task.get('stage')
        kind = ('delivery' if task.get('finalize_escalate_reason') == 'delivery_incomplete'
                and task.get('delivery_failure') else 'finalize' if task.get('finalize_escalated') else 'fix_loop')
        candidate = (task.get('commit') or task.get('candidate_sha'))
        if kind in {'finalize', 'delivery'}:
            retry = node_id
        else:
            gate_target = _gate(nodes, node_id)
            if gate_target is not None:
                source_node, _ = candidate_responsible_source(workflow, current, candidate, retry_node=gate_target)
                retry = source_node or gate_target
            else:
                retry = None
        key = (kind, retry, candidate)
        groups.setdefault(key, []).append(task)
    obligations = []
    for (kind, retry, candidate), failed in groups.items():
        affected = set()
        for task in failed:
            affected.update(task.get('affected_task_ids') or [])
            affected.update(task.get('stage_verdict_affected_task_ids') or [])
        used_fallback = False
        if not affected:
            source_node, source_targets = candidate_responsible_source(workflow, current, candidate, retry_node=retry)
            if source_node == retry and source_targets:
                affected.update(source_targets)
            else:
                affected.update(task['task_id'] for task in current
                                if task.get('task_id') and (task.get('node') or task.get('stage')) == retry
                                and candidate in {
                                    task.get('commit'),
                                    task.get('candidate_sha'),
                                    task.get('integrated_commit'),
                                    (task.get('wrapup_delivery') or {}).get('integrated_sha'),
                                })
            if not affected and retry:
                # Fallback for parallel tasks / multi-task DAGs: attribute to all active tasks under retry node
                affected.update(task['task_id'] for task in current
                                if task.get('task_id') and (task.get('node') or task.get('stage')) == retry
                                and task.get('status') not in ('superseded', 'failed'))
                used_fallback = True
        facts = sorted(failed, key=lambda task: str(task.get('task_id') or ''))
        affected_facts = [task for task in current if task.get('task_id') in affected and task not in facts]
        if kind == 'delivery':
            from .delivery_rework import repair_reason
            reason = next((r for r in map(repair_reason, facts) if r), None)
            if (not workflow.get('execution_id') or any(t.get('execution_id') != workflow['execution_id'] for t in facts)):
                reason = 'delivery_identity_unknown'
        elif used_fallback:
            reason = _identity_reason(workflow, facts, candidate)
            if reason is None:
                for t in affected_facts:
                    if (workflow.get('execution_id') and t.get('execution_id') != workflow['execution_id']) \
                            or (t.get('workflow_id') != workflow.get('workflow_id')) \
                            or not t.get('task_id') or not t.get('run_id'):
                        reason = 'identity_unknown'
                        break
        else:
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
            if retry == 'wrapup':
                verifiers = [n for n in nodes.values() if n.get('id') != 'wrapup'
                             and (n.get('id') in {t.get('node') or t.get('stage') for t in failed}
                                  or n.get('id') in {'test', 'review', 'acceptance'})]
            else:
                verifiers = [n for n in nodes.values() if n.get('id') != 'wrapup'
                             and _gate(nodes, n.get('id')) == retry]
            if any(launch_capacity_error(node_usage(n, tasks, workflow.get('workflow_id')))
                   for n in verifiers):
                reason = 'verifier_budget_exhausted'
        if reason is None and kind == 'fix_loop' and not affected:
            reason = 'affected_tasks_unknown'
        identity_facts = [{**{key: task.get(key) for key in _FACT_FIELDS},
                           **({'integrated_commit': task['integrated_commit']} if task.get('integrated_commit') else {}),
                           'affected_task_ids': sorted(affected)} for task in facts + affected_facts]
        if kind == 'delivery':
            for fact, task in zip(identity_facts, facts + affected_facts):
                fact.update(delivery_failure=task.get('delivery_failure'), delivery_rework_attempts=task.get('delivery_rework_attempts'))
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
