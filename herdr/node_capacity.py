"""Pure node dispatch budgets; cumulative history is separate from concurrency."""
from .transitions import ACTIVE_TASK_STATUSES

CONCURRENT_STATUSES = ACTIVE_TASK_STATUSES | {'pending'}


def pane_reference(task):
    runtime = task.get('runtime')
    if isinstance(runtime, dict) and 'pane_id' in runtime:
        return runtime['pane_id']
    return task.get('pane_id')


def _positive_limit(value, field):
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise ValueError(f'{field} must be a positive integer')
    return value


def node_usage(node, tasks, workflow_id=None):
    nid = node.get('id') or node.get('key')
    selected = [t for t in tasks if (t.get('node') or t.get('stage')) == nid
                and (workflow_id is None or t.get('workflow_id') == workflow_id)]
    policy = node.get('agent_policy') or node.get('worker_policy') or {}
    if not isinstance(policy, dict):
        raise ValueError(f'node={nid}: agent_policy must be an object')
    concurrency = _positive_limit(policy.get('max_concurrency'), 'max_concurrency')
    legacy = _positive_limit(policy.get('max_agents'), 'max_agents')
    total_limit = _positive_limit(node.get('max_tasks_per_node'), 'max_tasks_per_node')
    # Only old configurations use a cumulative confirmation threshold. New
    # fields have distinct hard contracts; acknowledgement cannot bypass them.
    warning_limit = legacy if total_limit is None and 'max_concurrency' not in policy else None
    active = [t for t in selected if t.get('status') in CONCURRENT_STATUSES]
    panes = sorted({str(pane_reference(t)) for t in selected if pane_reference(t)})
    total_overflow = total_limit is not None and len(selected) > total_limit
    concurrent_overflow = concurrency is not None and len(active) > concurrency
    # Unified retirement predicate (status==superseded or superseded_by):
    # a retired row can never revive, so it must not hold the hard budget.
    retired = [t for t in selected if t.get('status') == 'superseded' or t.get('superseded_by')]
    budget_tasks = [t for t in selected if t not in retired]
    locked_overflow = total_limit is not None and len(budget_tasks) + 1 > total_limit
    return {
        'node': nid, 'task_count': len(selected), 'active_task_count': len(active),
        'registered_task_count': len(selected),
        'budget_task_count': len(budget_tasks),
        'superseded_task_count': sum(t.get('status') == 'superseded' for t in selected),
        'retired_task_count': len(retired),
        'task_count_source': 'all_registered_including_superseded',
        'active_task_count_source': 'concurrent_statuses',
        'pane_count': len(panes), 'pane_ids': panes,
        'pane_count_source': 'persisted_references',
        'orphan_pane_count': len({str(pane_reference(t)) for t in selected
                                  if t.get('status') in {'failed', 'superseded', 'cleaned'}
                                  and pane_reference(t)}),
        'task_ids': sorted(str(t['task_id']) for t in selected),
        'max_concurrency': concurrency, 'max_tasks_per_node': total_limit,
        'legacy_max_agents': legacy, 'confirmation_threshold': warning_limit,
        'overflow': total_overflow or concurrent_overflow or
                    (warning_limit is not None and len(selected) > warning_limit),
        'locked_overflow': locked_overflow,
    }


def launch_capacity_error(usage):
    """Hard budgets apply even when an operator acknowledges legacy overflow."""
    count = usage.get('budget_task_count', usage['task_count']) + 1
    limit = usage['max_tasks_per_node']
    if limit is not None and count > limit:
        return (f'task count {count} exceeds max_tasks_per_node={limit}; '
                f'registered={usage["task_count"]} including {usage.get("superseded_task_count", 0)} superseded '
                f'({usage.get("retired_task_count", usage.get("superseded_task_count", 0))} retired); '
                f'budget={usage.get("budget_task_count", usage["task_count"])}; '
                f'active={usage["active_task_count"]}')
    # The old owner stays active until successful replacement delivery.
    # Launch therefore requires a real free slot even for replacements.
    active = usage['active_task_count'] + 1
    limit = usage['max_concurrency']
    if limit is not None and active > limit:
        return f'concurrent task count {active} exceeds max_concurrency={limit}'
    return None
