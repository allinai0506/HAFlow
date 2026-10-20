"""Acquire durable dispatch responsibility for coordinator stage events."""


def admit_stage_dispatch(
    item,
    node,
    target_node_id,
    *,
    get_store,
    coordinator_intake_enabled,
    reconcile_node_dispatches,
    find_node,
    scheduler_join_gate_allows,
    load_tasks,
    clear_stage_advance,
):
    """Return the stage item and lease, or None when admission must stop."""
    dispatch_op = None
    dispatch_owner = None
    workflow_id = item["workflow_id"]
    store = get_store()
    workflow = store.get_workflow(workflow_id)

    if item.get("stage") in (None, "", "start") and not coordinator_intake_enabled():
        if workflow:
            from herdr.node_dispatch_store import operation_for_node

            reconcile_node_dispatches(workflow_id, discover=False)
            previous_dispatch = operation_for_node(
                store.db_path, workflow_id, target_node_id
            )
            if previous_dispatch and previous_dispatch["status"] not in (
                "resolved",
                "superseded",
            ):
                return None

    # Native workflows require durable responsibility for intake and downstream dispatch.
    if ((node or {}).get("depends_on") or coordinator_intake_enabled()) and workflow:
        from herdr.node_dispatch_store import claim, operation_for_node

        reconcile_node_dispatches(workflow_id)
        dispatch_op = operation_for_node(
            store.db_path, workflow_id, target_node_id
        )
        if not dispatch_op:
            return None

        if item.get("dispatch_operation_id") is not None:
            if (
                item["dispatch_operation_id"] != dispatch_op["id"]
                or item.get("dispatch_owner") != dispatch_op["owner"]
                or dispatch_op["status"] != "running"
                or dispatch_op["started"]
            ):
                return None
            dispatch_owner = item["dispatch_owner"]
        else:
            import uuid

            dispatch_op = claim(
                store.db_path, dispatch_op["id"], uuid.uuid4().hex
            )
            if dispatch_op is None:
                return None
            dispatch_owner = dispatch_op["owner"]

        item = dict(
            item,
            dispatch_operation_id=dispatch_op["id"],
            dispatch_owner=dispatch_owner,
        )
        pinned = workflow["config"]
        node = find_node(pinned, target_node_id)
        item = dict(item, node=node)
        tasks = load_tasks()
        execution_id = workflow.get("execution_id")
        if execution_id:
            tasks = [
                task for task in tasks
                if task.get("workflow_id") != workflow_id
                or not task.get("execution_id")
                or task.get("execution_id") == execution_id
            ]
        if not scheduler_join_gate_allows(workflow_id, node, tasks):
            from herdr.node_dispatch_store import defer

            defer(
                store.db_path,
                dispatch_op["id"],
                "join_gate_blocked",
                owner=dispatch_owner,
            )
            clear_stage_advance(workflow_id, target_node_id)
            return None

    return item, node, dispatch_op, dispatch_owner
