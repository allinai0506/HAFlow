"""Check scheduler join evidence and reconcile durable node dispatch ownership.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def _scheduler_join_gate_allows(ports, workflow_id, node, tasks):
    """汇聚门禁放行判定:非 join 节点一律放行(保持原语义)。

    join 节点 = node_type 为 gate 且 depends_on >= 2 的节点。
    判定失败/异常一律拒绝(Fail-Closed)。
    """
    node = node or {}
    store = None
    try:
        from herdr.recovery_store import read_snapshot
        from herdr.business_gate import business_gate_blockers
        from herdr.gate_wait import record_wait
        store = ports._get_store()
        if store.get_workflow(workflow_id):
            authority, config, owned_tasks = read_snapshot(store.db_path, workflow_id)
            missing = business_gate_blockers(store, authority, config, owned_tasks, node.get('id'))
            record_wait(store.db_path, workflow_id, node.get('id'), missing)
            if missing:
                print(f'[BUSINESS GATE WAIT] workflow={workflow_id} node={node.get("id")} tasks={missing}')
                return False
        elif any(t.get('completion_protocol') == 'receipt-v1' for t in tasks):
            return False
    except (OSError, ValueError) as exc:
        print(f'[BUSINESS GATE WAIT] workflow={workflow_id}: {type(exc).__name__}')
        if getattr(store, 'db_path', None):
            try:
                record_wait(store.db_path, workflow_id, node.get('id'), ['<unknown:' + type(exc).__name__ + '>'])
            except (OSError, ValueError) as audit_exc:
                print(f'[BUSINESS GATE AUDIT FAILED] workflow={workflow_id}: {audit_exc}')
        return False
    deps = list(node.get("depends_on") or [])
    node_type = str(node.get("node_type") or "")
    # Join-before-dispatch applies to two shapes:
    # 1. explicit join nodes (node_type=gate with >=2 dependencies);
    # 2. scheduler-engaged fan-in agent nodes with >=2 dependencies
    #    (e.g. wrapup on [test, review]): once the scheduler froze a
    #    candidate for a workflow, every multi-dependency dispatch in that
    #    workflow must prove same-revision verification first.
    # Workflows the scheduler never engaged keep legacy passthrough.
    is_join_shape = (node_type == "gate" and len(deps) >= 2)
    engaged = False
    if not is_join_shape and len(deps) >= 2:
        if ports.scheduler_facts_store is None:
            # Scheduler unavailable, so no candidate identity can be enforced
            # on a fan-in node. Refuse rather than wave the node through.
            print(
                f"[JOIN GATE REFUSED] workflow={workflow_id} "
                f"gate={node.get('id')} reason=scheduler_unavailable"
            )
            return False
        try:
            engaged = bool(
                ports.scheduler_facts_store.latest_frozen_candidate_sha(workflow_id)
            )
        except Exception as exc:
            # "lookup failed" is not "never frozen". Treating the error as
            # un-engaged would classify a scheduler-managed workflow as legacy
            # and let a fan-in node (e.g. wrapup on [test, review]) bypass the
            # gate exactly when the identity of the candidate is unknown.
            # Fail closed (AGENTS.md §4.4); the next sweep retries.
            print(
                f"[JOIN GATE REFUSED] workflow={workflow_id} "
                f"gate={node.get('id')} reason=frozen_lookup_failed: {exc}"
            )
            return False
    if not (is_join_shape or engaged):
        return True
    if ports.scheduler_core is None:
        return False
    expected = ""
    if ports.scheduler_facts_store is not None:
        try:
            expected = ports.scheduler_facts_store.latest_frozen_candidate_sha(workflow_id)
        except Exception as exc:
            # Cannot know which candidate must be matched: refuse. Returning a
            # blank expectation would let the gate compare against nothing.
            print(
                f"[JOIN GATE REFUSED] workflow={workflow_id} "
                f"gate={node.get('id')} reason=frozen_lookup_failed: {exc}"
            )
            return False
    try:
        passed, reason, details = ports.scheduler_core.evaluate_join_gate(
            node, tasks, workflow_id, expected,
            reuse_facts=ports._reverification_gate_facts(workflow_id),
        )
    except Exception as exc:
        print(
            f"[JOIN GATE ERROR] workflow={workflow_id} "
            f"gate={node.get('id')}: {exc}"
        )
        return False
    if ports.scheduler_facts_store is not None:
        try:
            ports.scheduler_facts_store.record_join_gate_verdict(
                workflow_id, str(node.get("id") or ""), passed, reason, details
            )
        except Exception as exc:
            print(f"[JOIN GATE AUDIT WARN] workflow={workflow_id}: {exc}")
    if not passed:
        print(
            f"[JOIN GATE REFUSED] workflow={workflow_id} "
            f"gate={node.get('id')} reason={reason}"
        )
    return passed

def reconcile_node_dispatches(ports, workflow_id, *, discover=True):
    from herdr.node_dispatch_store import reconcile_workflow, operation_for_node
    state = ports.load_stage_state()
    legacy = [key[len(workflow_id) + 1:] for key, value in state.items()
              if key.startswith(workflow_id + ':') and value == 'notified']
    enabled = ports.coordinator_intake_enabled()
    if not enabled:
        record = ports._get_store().get_workflow(workflow_id) or {}
        for node in (record.get('config') or {}).get('nodes') or []:
            if node.get('depends_on'):
                continue
            op = operation_for_node(ports._get_store().db_path, workflow_id, node['id'])
            if op and not op['started'] and op['status'] in ('pending', 'running'):
                # Release the old latch before committing transfer; no direct queue is created yet.
                ports.clear_stage_advance(workflow_id, node['id'])
    return reconcile_workflow(ports._get_store().db_path, workflow_id,
                              legacy_notified=legacy, discover=discover, intake_enabled=enabled)


DEPENDENCIES = {'_scheduler_join_gate_allows': ('_get_store', '_reverification_gate_facts', 'scheduler_core',
                                 'scheduler_facts_store'),
 'reconcile_node_dispatches': ('_get_store', 'clear_stage_advance', 'coordinator_intake_enabled',
                               'load_stage_state')}

__all__ = tuple(DEPENDENCIES)
