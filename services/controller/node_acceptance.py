"""Write one same-node redo or a known-limit acceptance.

The decision lives in herdr.node_acceptance. One node is one transaction:
a failed compare rolls the node back, and the next sweep can try again.
"""

def _apply_group(db_path, action):
    from herdr.state.transition_task import transition_task
    from herdr.state_db import get_db_connection

    conn = get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for task in action["tasks"]:
            version = task.get("version")
            if action["op"] == "rework":
                to_status = "superseded"
                reason = "same_node_redo"
                metadata = {
                    "replacement_pending": True,
                    "same_node_redo_note": action["note"],
                }
            else:
                to_status = task.get("status")
                reason = "accepted_with_limits"
                metadata = {"accepted_with_limits": True}
            result = transition_task(
                task["task_id"],
                to_status,
                reason,
                source="node_acceptance",
                metadata=metadata,
                conn=conn,
                expected_status=task.get("status"),
                expected_version=version if isinstance(version, int) else None,
            )
            if not result.get("ok"):
                conn.execute("ROLLBACK")
                return 0
        conn.execute("COMMIT")
        return len(action["tasks"])
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def apply_node_acceptance(ports, workflow_id, config, tasks):
    """Supersede a first blocked result, or stamp a second one. Returns rows written."""
    from herdr.node_acceptance import acceptance_actions

    gate_ids = []
    for node in (config or {}).get("nodes") or []:
        if not isinstance(node, dict) or not node.get("id"):
            continue
        try:
            is_gate = ports.resolve_gate_config(node, node.get("id")) is not None
        except Exception:
            is_gate = True
        if is_gate:
            gate_ids.append(node.get("id"))
    actions = acceptance_actions(config, tasks, workflow_id, gate_ids=gate_ids)
    written = 0
    db_path = ports._get_store().db_path
    for action in actions:
        count = _apply_group(db_path, action)
        if not count:
            print(
                f"[NODE ACCEPTANCE] workflow={workflow_id} "
                f"node={action['node_id']} {action['op']} skipped"
            )
            continue
        written += count
        task_ids = ",".join(str(task.get("task_id") or "") for task in action["tasks"])
        print(
            f"[NODE ACCEPTANCE] workflow={workflow_id} "
            f"node={action['node_id']} {action['op']} tasks={task_ids}"
        )
    return written


def reread_after_acceptance(ports, workflow_id, record, config, tasks, assessment):
    """Re-read after a redo so advance sees superseded rows, not the old block."""
    from herdr import recovery_store
    from herdr.workflow_progress import assess_workflow

    try:
        wrote = ports.apply_node_acceptance(workflow_id, config, tasks)
    except Exception as exc:
        print(f"[NODE ACCEPTANCE] workflow={workflow_id}: {exc}")
        wrote = 0
    if not wrote:
        return record, config, tasks, assessment
    recovery_store.reconcile(ports._get_store().db_path, workflow_id)
    ports.reconcile_node_dispatches(workflow_id)
    record, config, tasks = recovery_store.read_snapshot(
        ports._get_store().db_path, workflow_id)
    return record, config, tasks, assess_workflow(record, config, tasks)


DEPENDENCIES = {
    "apply_node_acceptance": ("_get_store", "resolve_gate_config"),
}
