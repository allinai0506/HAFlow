"""Persist a successfully allocated worker as a Task and close its launch intent."""

from dataclasses import dataclass
import json


@dataclass(frozen=True)
class LaunchRegistration:
    args: object
    now: float
    node: dict
    node_id: str
    run_id: str
    project: dict
    exec_fields: dict
    source_repo: str
    base_branch: str
    stage_config: dict
    workspace_id: str
    pane_id: str
    pane_source: str
    resolved_agent: str
    requested_agent: str
    clone_path: str
    branch: str
    baseline_commit: str | None
    onto_branch: str | None
    baseline_untracked: list
    baseline_fingerprint: dict
    recorded_onto: str | None
    launch_intent: dict
    worker_result: dict
    predecessor: dict | None
    committed_recovery: bool


def register_launched_task(context, store, *, apply_execution_identity,
                           init_status_history, is_gate_task,
                           trajectory_ledger_factory,
                           record_trajectory_event):
    """Build and persist the Task, intent completion, and start trajectory events."""
    args = context.args
    try:
        from herdr import runtime_state
    except ImportError:
        runtime_state = None

    runtime = None
    if runtime_state is not None:
        runtime = runtime_state.build_runtime_state(
            agent=context.resolved_agent,
            agent_session_id=context.worker_result.get("agent_session_id"),
            agent_name=context.worker_result.get("agent_name"),
            workspace_id=context.workspace_id,
            tab_id=context.stage_config.get("tab_id"),
            pane_id=context.pane_id,
            cwd=context.clone_path,
            status="running",
            now=context.now,
        )

    delivery_identities = []
    if context.source_repo:
        try:
            from herdr.review_closure import _path_delivery_identities
            delivery_identities = _path_delivery_identities(context.source_repo)
        except Exception:
            delivery_identities = []

    task = {
        "task_id": args.task_id,
        "workflow_id": args.workflow_id,
        "run_id": context.run_id,
        "project_id": context.project.get("project_id"),
        "project_name": context.project.get("project_name"),
        "source_repo": context.source_repo,
        "delivery_identities": delivery_identities,
        "base_branch": context.base_branch,
        "coordinator_pane_id": context.project.get("coordinator_pane_id"),
        "workflow_config": context.project.get("workflow_file"),
        "node": context.node_id,
        "node_label": context.stage_config.get("node_label") or context.stage_config.get("stage_label", ""),
        "stage": context.node_id,
        "stage_label": context.stage_config.get("stage_label") or context.stage_config.get("node_label", ""),
        "tab_id": context.stage_config["tab_id"],
        "workspace_id": context.workspace_id,
        "pane_id": context.pane_id,
        "pane_source": context.pane_source,
        "pane_persistent": True,
        "agent": context.resolved_agent,
        "requested_agent": context.requested_agent,
        "clone_path": context.clone_path,
        "branch": context.branch,
        "baseline_commit": context.baseline_commit,
        "onto_branch": context.onto_branch,
        "baseline_untracked": context.baseline_untracked,
        "baseline_fingerprint": context.baseline_fingerprint,
        "baseline_commit": context.baseline_commit,
        "onto_branch": context.recorded_onto,
        "integration_mode": args.integration_mode,
        "artifact_mode": args.artifact_mode,
        "dispatch_role": args.dispatch_role,
        "dispatch_round": args.dispatch_round,
        "launch_intent_id": context.launch_intent["intent_id"],
        "task_type": args.task_type,
        "status": "pending",
        "goal": args.goal,
        "acceptance_criteria": args.acceptance,
        "candidate_episode_id": context.project.get("candidate_episode_id"),
    }
    if getattr(args, "customer", None):
        task["customer"] = str(args.customer).strip()
    if getattr(args, "business_task_type", None):
        task["business_task_type"] = str(args.business_task_type).strip()
    if getattr(args, "mandatory_experience_rules", False):
        task["mandatory_experience_rules"] = True
    replay_of = getattr(args, "replay_of", None)
    if replay_of:
        task["replay_of"] = replay_of
    bound_sha = str(getattr(args, "candidate_sha", None) or "").strip()
    if bound_sha:
        task["candidate_sha"] = bound_sha
    apply_execution_identity(task, args)
    if context.launch_intent.get("dispatch_operation_id") is not None:
        task["dispatch_operation_id"] = context.launch_intent["dispatch_operation_id"]
        if context.launch_intent.get("supersedes"):
            task["supersedes"] = context.launch_intent["supersedes"]
    if getattr(args, "agent_policy", None):
        task["agent_policy"] = json.loads(args.agent_policy)
    if context.exec_fields["execution_mode"] == "context":
        task["execution_mode"] = "context"
        task["context"] = context.exec_fields.get("context") or {}
    if runtime is not None:
        task["runtime"] = runtime
    init_status_history(task, "pending", context.now)

    node = context.node
    if node:
        if node.get("gate") or node.get("node_type") == "gate":
            is_gate = True
        elif node.get("node_type") in ("agent", "implementation", "worker", "code"):
            is_gate = False
        else:
            is_gate = is_gate_task(task)
    else:
        is_gate = is_gate_task(task)
    if is_gate:
        task["gate"] = True
    if node.get("delivery_contract") is not None:
        from herdr.task_delivery import validate_contract
        task["delivery_contract"] = validate_contract(node["delivery_contract"])
    if context.committed_recovery:
        predecessor = context.predecessor
        task.update(
            recovery_predecessor=predecessor["task_id"],
            expected_predecessor_version=predecessor["version"],
            expected_predecessor_sha=predecessor["commit"],
        )

    store.save_task(task)
    from herdr.task_resources import finish_launch_intent
    finish_launch_intent(store, context.launch_intent)
    ledger = trajectory_ledger_factory(store.db_path)
    record_trajectory_event(task, "run_started", ledger=ledger, status="pending")
    record_trajectory_event(task, "task_started", ledger=ledger, status="pending")
    record_trajectory_event(
        task, "agent_started", ledger=ledger,
        status=(task.get("runtime") or {}).get("status") or "running",
    )
    return task
