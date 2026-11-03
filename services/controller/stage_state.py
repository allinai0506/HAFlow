"""Persist and reconcile stage queued/notified latches and node completion status.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""


# Workflow stage state
def load_stage_state(ports):
    if not ports.os.path.exists(ports.STAGE_STATE_FILE):
        return {}

    try:
        with open(
            ports.STAGE_STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            return ports.json.load(f)
    except Exception:
        return {}

def save_stage_state(ports, data):
    tmp = ports.STAGE_STATE_FILE + ".tmp"

    with open(
        tmp,
        "w",
        encoding="utf-8"
    ) as f:
        ports.json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

    ports.os.replace(
        tmp,
        ports.STAGE_STATE_FILE
    )

def stage_advance_key(ports,
    workflow_id,
    stage
):
    return f"{workflow_id}:{stage}"

def mark_stage_advance_queued(ports,
    workflow_id,
    stage
):
    key = ports.stage_advance_key(
        workflow_id,
        stage
    )

    with ports.lock:
        state = ports.load_stage_state()

        if state.get(key) in (
            "queued",
            "notified"
        ):
            return False

        state[key] = "queued"
        ports.save_stage_state(state)

    return True

def clear_stage_advance(ports,
    workflow_id,
    stage
):
    key = ports.stage_advance_key(
        workflow_id,
        stage
    )

    with ports.lock:
        state = ports.load_stage_state()
        state.pop(key, None)
        ports.save_stage_state(state)

def clear_workflow_stage_state(ports,
    workflow_id
):
    """Clear every queued/notified latch of one workflow (close-workflow semantics)."""
    prefix = f"{workflow_id}:"

    with ports.lock:
        state = ports.load_stage_state()
        removed = [key for key in state if key.startswith(prefix)]
        for key in removed:
            del state[key]
        if removed:
            ports.save_stage_state(state)

    return removed

def reset_queued_stage_states(ports):
    with ports.lock:
        state = ports.load_stage_state()
        changed = False
        for k in list(state.keys()):
            if state[k] == "queued":
                del state[k]
                changed = True
        if changed:
            ports.save_stage_state(state)


# Coordinator queue
def get_stage_status(ports,
    workflow_id,
    stage
):
    try:
        output = ports.subprocess.check_output(
            [
                ports.TASK_MANAGER,
                "stage-status",
                workflow_id,
                stage
            ],
            text=True
        )

        return ports.json.loads(output)

    except Exception as e:
        print(
            f"[STAGE STATUS ERROR] "
            f"workflow={workflow_id} "
            f"stage={stage}: {e}"
        )
        return None

def is_node_complete(ports, workflow_id, node_id):
    tasks = [
        t for t in ports.load_tasks()
        if t.get("workflow_id") == workflow_id
        and (t.get("node") == node_id or t.get("stage") == node_id)
    ]
    # Superseded tasks are excluded from completion calculation — they were
    # replaced by another task whose outcome is the authoritative result.
    active = [
        t for t in tasks
        if t.get("status") != "superseded" and not t.get("superseded_by")
    ]

    # No task, or only superseded ones: a reused verifier looks exactly like
    # this, because reuse never creates a Task (PR #108). Its verification is
    # satisfied by an immutable reuse fact bound to the current candidate, so
    # it is complete *for that candidate* — without this the workflow would
    # wait forever on a verifier that is not going to run again, and the join
    # gate would never see a satisfied branch.
    #
    # The binding is re-resolved against the latest frozen candidate on every
    # call, so a later rotation (A -> B -> C) drops the A -> B fact
    # automatically and the node stops counting as complete.
    cfg = ports.workflow_config_for(workflow_id) or {}
    node = next((n for n in cfg.get("nodes", []) if n.get("id") == node_id), {})
    required_ids = node.get("required_task_ids")
    if not active and required_ids is None:
        # Current-candidate verifier reuse is a real replacement for execution;
        # missing/stale evidence remains false, including pending obligations.
        return ports._reverification_satisfies_node(workflow_id, node_id)

    if ports.scheduler_core is not None:
        try:
            complete = ports.scheduler_core.node_is_complete(tasks, required_ids, node=node)
        except TypeError:
            complete = ports.scheduler_core.node_is_complete(tasks, required_ids)
    else:
        if required_ids is not None or any(t.get("replacement_pending") for t in tasks):
            return False
        complete = all(
            t.get("status") in (
                "completed", "committed", "integrated", "cleanup_ready", "cleaned"
            )
            and (t.get("integration_mode") != "git"
                 or t.get("status") in ("integrated", "cleanup_ready", "cleaned"))
            for t in active
        )
    if not complete:
        return False

    if node_id == "plan":
        has_challenger_task = False
        for t in active:
            explicit_role = str(t.get("dispatch_role") or t.get("role") or "").strip().lower()
            if explicit_role:
                if explicit_role == "challenger":
                    has_challenger_task = True
                    break
            elif "challenger" in str(t.get("task_id") or "").lower():
                has_challenger_task = True
                break
        if has_challenger_task:
            from herdr.plan_parser import verify_plan_and_challenger
            all_wf_tasks = ports.load_tasks()
            verif_ok, _, _ = verify_plan_and_challenger(workflow_id, tasks=all_wf_tasks, require_challenger=True)
            if not verif_ok:
                return False

    return True


def dispatch_awaiting_human(ports, workflow_id, node_id):
    """True when this node's durable dispatch operation has escalated to a human.

    waiting_human is the recovery contract's terminal parking state: the operation
    stays there until a person decides. Re-queueing the stage underneath it does
    not produce a different launch, it produces the same refusal again.
    """
    from herdr.node_dispatch_store import operation_for_node

    operation = operation_for_node(ports._get_store().db_path, workflow_id, node_id)
    return bool(operation and operation["status"] == "waiting_human")


def reconcile_stage_advance_states(ports, workflow_id, workflow_cfg):
    """Revoke 'notified' stage-state entries when the node they guard has
    regressed — either because a predecessor regressed, or because the node's
    own task lineage died with no live member left to re-dispatch.

    Without this, a stage that regresses after the coordinator was already
    notified would never be re-triggered — because mark_stage_advance_queued
    returns False for 'notified' entries and the node never appears in
    get_ready_nodes again.

    Both regression shapes deadlock the sweep silently:

    * predecessor regressed (upstream check below);
    * the node's own tasks were all superseded while its predecessors stayed
      complete — a fix-loop backflow whose promised replacements were never
      created (zombie obligations). Real deadlock: wf-nexusarchive-1005-01,
      where `test`/`review` kept their 'notified' lock forever and the
      controller skipped both every sweep with no log line at all.
    """
    with ports.lock:
        state = ports.load_stage_state()
        changed = False
        wf_tasks = None

        nodes_by_id = {
            n["id"]: n
            for n in workflow_cfg.get("nodes", [])
        }

        for key in list(state.keys()):
            if not key.startswith(f"{workflow_id}:"):
                continue
            if state[key] != "notified":
                continue

            node_id = key.split(":", 1)[1]
            node = nodes_by_id.get(node_id)
            if not node:
                continue

            # Check whether all predecessors are still complete.
            deps = node.get("depends_on", [])
            predecessors_complete = all(
                ports.is_node_complete(workflow_id, dep)
                for dep in deps
            )
            if predecessors_complete:
                # Upstream is intact, so the node can still regress on its own:
                # fix-loop may have superseded everything it dispatched without
                # the replacements ever landing. Tasks load at most once per call.
                if wf_tasks is None:
                    wf_tasks = ports.load_tasks()
                if ports.direct_dispatch_planner is not None and (
                    ports.direct_dispatch_planner.lineage_redispatch_candidates(
                        ports.direct_dispatch_planner.node_tasks_for_latch(wf_tasks, workflow_id, node_id)
                    )
                ):
                    # The durable operation owns this decision once it escalates:
                    # revoking here would re-queue a dispatch that can only be
                    # refused again, spinning with no backoff and no terminal state.
                    if ports.dispatch_awaiting_human(workflow_id, node_id):
                        continue
                    reason = "own lineage fully superseded with no live task"
                elif not ports.is_node_complete(workflow_id, node_id):
                    node_t = [t for t in (wf_tasks or []) if t.get("workflow_id") == workflow_id and (t.get("node") == node_id or t.get("stage") == node_id)]
                    # Must have known registered tasks: absence of tasks alone must NOT authorize revocation!
                    if not node_t:
                        continue
                    from herdr.transitions import ACTIVE_TASK_STATUSES, COMPLETED_TASK_STATUSES
                    has_running = any(str(t.get("status") or "") in ACTIVE_TASK_STATUSES for t in node_t)
                    if has_running or ports.dispatch_awaiting_human(workflow_id, node_id):
                        continue

                    # Check if the node defines staged roles
                    roles = (node.get("agent_policy") or {}).get("roles") or node.get("roles") or []
                    if not (isinstance(roles, list) and len(roles) > 1):
                        continue

                    # Verify known prior delivery: at least one prerequisite role is completed
                    completed_roles = set()
                    for t in node_t:
                        if str(t.get("status") or "") in COMPLETED_TASK_STATUSES and not t.get("superseded_by"):
                            explicit_role = str(t.get("dispatch_role") or t.get("role") or "").strip().lower()
                            if explicit_role:
                                completed_roles.add(explicit_role)
                            else:
                                tid = str(t.get("task_id") or "").lower()
                                for r in roles:
                                    r_n = str(r.get("name") or "").strip().lower()
                                    if r_n and r_n in tid:
                                        completed_roles.add(r_n)

                    if not completed_roles:
                        continue

                    # Check if plan review is outdated or invalid (reopening review)
                    needs_redispatch = False
                    if node_id == "plan" and "challenger" in completed_roles:
                        from herdr.plan_parser import verify_plan_and_challenger
                        verif_ok, verif_reason, details = verify_plan_and_challenger(
                            workflow_id, tasks=wf_tasks, require_challenger=True
                        )
                        if not verif_ok:
                            refusal_code = verif_reason.split(":")[0].strip()
                            if refusal_code in (
                                "challenger_outdated",
                                "challenger_plan_ref_missing",
                                "challenger_plan_version_mismatch",
                                "challenger_invalid",
                                "challenger_verdict_missing",
                            ) and (details.get("plan_id") or details.get("valid_plan_ids")):
                                completed_roles.discard("challenger")
                                needs_redispatch = True

                    # Verify there is an un-dispatched (or needs redispatch) role whose prerequisites are met
                    pending_ready = False
                    for r in roles:
                        r_name = str(r.get("name") or "").strip().lower()
                        if r_name in completed_roles:
                            continue
                        dep_role = str(r.get("depends_on_role") or "").strip().lower()
                        has_executor = any(str(x.get("name") or "").strip().lower() == "executor" for x in roles)
                        if not dep_role and node_id == "plan" and r_name in ("challenger", "review", "reviewer", "adversarial") and has_executor:
                            dep_role = "executor"
                        if not dep_role or dep_role in completed_roles:
                            if dep_role == "executor" or (node_id == "plan" and r_name == "challenger"):
                                from herdr.plan_parser import find_plan_deliverables, validate_plan_deliverable_content
                                deliv = find_plan_deliverables(workflow_id, wf_tasks)
                                p_text = deliv[0] if deliv else None
                                p_meta = getattr(deliv, "meta", {})
                                p_ref = str(p_meta.get("plan_id") or "").strip()
                                if not p_text or not p_ref:
                                    continue
                                p_ok, _ = validate_plan_deliverable_content(p_text, role="executor")
                                if not p_ok:
                                    continue
                            pending_ready = True
                            break

                    if pending_ready:
                        reason = "staged role pending redispatch" if needs_redispatch else "node not complete with pending roles to dispatch"
                    else:
                        continue
                else:
                    continue
            else:
                reason = "predecessors no longer complete"

            del state[key]
            changed = True
            print(
                f"[STAGE REVOKE] "
                f"workflow={workflow_id} node={node_id}: "
                f"{reason}, revoking 'notified' lock"
            )

        if changed:
            ports.save_stage_state(state)


DEPENDENCIES = {'load_stage_state': ('STAGE_STATE_FILE', 'json', 'os'),
 'save_stage_state': ('STAGE_STATE_FILE', 'json', 'os'),
 'stage_advance_key': (),
 'mark_stage_advance_queued': ('load_stage_state', 'lock', 'save_stage_state', 'stage_advance_key'),
 'clear_stage_advance': ('load_stage_state', 'lock', 'save_stage_state', 'stage_advance_key'),
 'clear_workflow_stage_state': ('load_stage_state', 'lock', 'save_stage_state'),
 'reset_queued_stage_states': ('load_stage_state', 'lock', 'save_stage_state'),
 'get_stage_status': ('TASK_MANAGER', 'json', 'subprocess'),
 'is_node_complete': ('_reverification_satisfies_node', 'load_tasks', 'scheduler_core',
                      'workflow_config_for'),
 'dispatch_awaiting_human': ('_get_store',),
 'reconcile_stage_advance_states': ('direct_dispatch_planner', 'dispatch_awaiting_human',
                                    'is_node_complete', 'load_stage_state', 'load_tasks',
                                    'lock', 'save_stage_state')}

__all__ = tuple(DEPENDENCIES)
