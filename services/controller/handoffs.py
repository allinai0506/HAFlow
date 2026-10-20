"""Dispatch verified node-result handoffs to dependent tasks.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def maybe_dispatch_node_handoffs(ports, *, workflow_id, ready_id, dep_ids, launched,
                                 tasks_by_id=None, prompt_sender=None, db_path=None):
    """Accelerator hook for deterministic node advance.

    For each newly launched task on a known edge (implementation→review,
    review→test), creates one HANDOFF from the latest completed upstream task
    and dispatches it through Herdr. Unknown edges return ``skipped`` so the
    existing Coordinator path stays authoritative. Never raises: a handoff
    failure must not break the already-launched downstream task.
    """
    from herdr import collaboration as _collab
    from herdr import state_db as _sdb

    launched = launched or []
    if not ports.collaboration_enabled():
        return [{"task_id": t, "skipped": True, "reason": "disabled"} for t in launched]
    try:
        if tasks_by_id is None:
            tasks_by_id = {t.get("task_id"): t for t in (ports.load_tasks() or [])
                           if isinstance(t, dict) and t.get("task_id")}
        authoritative_tasks = {}
        for task_id in (tasks_by_id or {}):
            current_task = _sdb.get_task(str(task_id), db_path=db_path)
            if current_task is not None:
                authoritative_tasks[task_id] = current_task
        tasks = authoritative_tasks
    except Exception as exc:
        return [{"task_id": t, "status": "failed", "error": type(exc).__name__}
                for t in launched]

    results = []
    for to_id in launched:
        try:
            target = tasks.get(to_id)
            if target is None:
                results.append({"task_id": to_id, "status": "failed",
                                "reason": "target_task_missing"})
                continue
            dep_hit = None
            trigger = None
            for dep in (dep_ids or []):
                inferred = _collab.infer_handoff_trigger(dep, ready_id)
                if inferred is not None:
                    dep_hit = dep
                    trigger = inferred
                    break
            if trigger is None:
                results.append({"task_id": to_id, "skipped": True,
                                "reason": "no_deterministic_route"})
                continue
            route = _collab.route_deterministic_handoff(trigger=trigger)
            target_scope = _collab.collab_scope_for_task(target)
            target_has_explicit_scope = bool(
                target.get("workflow_run_id") or target.get("execution_id")
            )

            def legacy_pair_is_proven(candidate):
                if target_has_explicit_scope or candidate.get("workflow_run_id") or candidate.get("execution_id"):
                    return _collab.collab_scope_for_task(candidate) == target_scope
                if not target.get("run_id") or not candidate.get("run_id"):
                    return False
                return str(target.get("run_id")) == str(candidate.get("run_id"))

            upstream = [t for t in tasks.values()
                        if isinstance(t, dict)
                        and t.get("workflow_id") == workflow_id
                        and (t.get("node") == dep_hit or t.get("stage") == dep_hit)
                        and t.get("status") in ports._NODE_DONE_STATUSES
                        and (not target_scope or _collab.collab_scope_for_task(t) == target_scope)
                        and legacy_pair_is_proven(t)]
            if not upstream:
                results.append({"task_id": to_id, "skipped": True,
                                "reason": "no_completed_upstream"})
                continue
            upstream.sort(key=lambda t: float(t.get("updated_at") or 0))
            from_task = upstream[-1]
            run_id = _collab.collab_scope_for_task(from_task)
            branch = from_task.get("branch")
            event = _sdb.create_collaboration_event({
                "run_id": run_id,
                "workflow_id": workflow_id,
                "from_task_id": from_task.get("task_id"),
                "from_agent": from_task.get("agent") or "",
                "from_pane_id": ports._collab_task_pane(from_task),
                "to_task_id": to_id,
                "to_agent": target.get("agent") or (route or {}).get("to_agent") or "",
                "type": (route or {}).get("type") or "HANDOFF",
                "summary": str(from_task.get("goal") or f"{dep_hit} completed."),
                "artifact_refs": ([f"branch:{branch}"] if branch else []),
                "evidence_refs": [],
                "context_refs": [],
                "source_fact_id": f"{workflow_id}:{dep_hit}:completed",
            }, db_path=db_path)
            try:
                from herdr.context_compiler import compile_working_context, infer_agent_role
                target_context = compile_working_context(
                    workflow_id=workflow_id,
                    task_id=to_id,
                    agent_role=infer_agent_role(target),
                    planned_links=[{
                        "from_task_id": from_task.get("task_id"),
                        "to_task_id": to_id,
                    }],
                    db_path=db_path,
                )
                event = _sdb.attach_working_context_ref(
                    event["event_id"], target_context.context_id, db_path=db_path,
                )
            except Exception as exc:
                print(f"[WORKING_CONTEXT SKIPPED] task={to_id}: {type(exc).__name__}")
            dispatched = ports.dispatch_collaboration_event(
                event["event_id"], tasks, prompt_sender, db_path=db_path)
            results.append({"task_id": to_id, **dispatched})
        except Exception as exc:
            results.append({"task_id": to_id, "status": "failed",
                            "error": type(exc).__name__})
    return results


DEPENDENCIES = {'maybe_dispatch_node_handoffs': ('_NODE_DONE_STATUSES', '_collab_task_pane',
                                  'collaboration_enabled', 'dispatch_collaboration_event',
                                  'load_tasks')}

__all__ = tuple(DEPENDENCIES)
