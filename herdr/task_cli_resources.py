"""CLI operations that inspect or renew existing task resources."""

import json
import shlex
import subprocess
import sys
from pathlib import Path


def capture_pane_transcript(pane, *, herdr):
    """Read bounded recent Pane scrollback; failures leave archival optional."""
    try:
        result = herdr(
            "pane", "read", pane,
            "--source", "recent-unwrapped",
            "--lines", "20000",
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode == 0 else ""


def handle_reap(args, *, get_store, herdr, evidence_root, capture=None):
    from herdr.task_resources import reap_task_pane
    from herdr.archive import ARCHIVED_STATUSES
    store = get_store()
    if not store.get_workflow(args.workflow_id):
        print(f"Workflow not found: {args.workflow_id}", file=sys.stderr)
        sys.exit(2)
    capture = capture or (lambda pane: capture_pane_transcript(pane, herdr=herdr))
    rows = []
    for task in store.list_tasks(workflow_id=args.workflow_id):
        if task.get("status") in ARCHIVED_STATUSES:
            rows.append(reap_task_pane(
                store, task["task_id"], apply=args.apply,
                close=lambda pane: herdr("pane", "close", pane).returncode == 0,
                capture=capture,
                transcript_root=evidence_root,
            ))
    print(json.dumps({"workflow_id": args.workflow_id, "apply": args.apply,
                      "tasks": rows}, ensure_ascii=False))
    if args.apply and any(r["action"] == "retained" for r in rows):
        sys.exit(2)


def handle_renew_completion(args, *, get_store, herdr):
    from herdr.supervisor_delivery import deliver
    from herdr.task_resources import owned_live_pane
    operation_id = args.operation_id
    if not operation_id or len(operation_id.encode()) > 128 or '\x00' in operation_id:
        raise ValueError('Renewal operation identity exceeds budget')
    store = get_store()
    task = store.get_task(args.task_id)
    if not task or task.get('completion_protocol') != 'receipt-v1' or task.get('status') not in ('working','dispatched','rework'):
        raise ValueError('Renewal requires an executing receipt-v1 task')
    workflow = store.get_workflow(task.get('workflow_id')) or {}
    if not workflow or workflow.get('status') in {'closing','completed','failed','halted','abandoned'}:
        raise ValueError('Renewal requires an open workflow')
    owned, reason = owned_live_pane(task)
    if not owned:
        raise ValueError('Renewal refused: pane ownership ' + reason)
    def send(pane, text):
        from herdr.supervisor_delivery import current_delivery_task
        pane = current_delivery_task(task, store, native=True)['pane_id']
        sent = herdr('agent','prompt',str(pane),text)
        if sent.returncode != 0:
            raise RuntimeError('Renewal native prompt transport failed')
    result = deliver(task, store, 'RENEW', {'intervention_id':operation_id,'action':'RENEW'},
        'The controller renewed the completion bearer for the current execution. Preserve the current work and checkpoint progress.', send)
    print(json.dumps(result, ensure_ascii=False))
    return result


def handle_launch_reconcile(args, *, get_store, release_agent_reservation):
    from herdr.task_resources import (
        _latest_intent,
        authorize_launch_recovery,
        dispatch_identity,
        inventory_launch_resources,
        recover_launch_resources,
        registered_launch_task,
        workflow_launch_lock,
    )

    try:
        store = get_store()
        key = dispatch_identity(args.workflow_id, args.node, args.dispatch_role,
                                args.candidate_sha or '', args.dispatch_round)
        with workflow_launch_lock(store.db_path, args.workflow_id):
            intent = _latest_intent(store, key)
            if not intent:
                alternatives = store.list_events(
                    workflow_id=args.workflow_id,
                    node_id=args.node,
                    event_type='launch_intent',
                    source='launch',
                    desc=True,
                    limit=100,
                )
                roles = sorted({
                    event['payload'].get('role', 'worker')
                    for event in alternatives
                    if event['payload'].get('candidate_sha', '') == (args.candidate_sha or '')
                    and event['payload'].get('dispatch_round', 1) == args.dispatch_round
                })
                if roles:
                    raise ValueError('launch intent role mismatch: requested=' + args.dispatch_role
                                     + '; available=' + ','.join(roles))
                raise ValueError('launch intent not found')

            registered = registered_launch_task(store, intent)
            if registered:
                report = {'resource_status': 'registered', 'task_id': registered['task_id'], 'applied': False}
            elif store.get_task(intent['task_id']):
                report = {
                    'resource_status': 'foreign',
                    'task_id': intent['task_id'],
                    'applied': False,
                    'reason': 'task_id_reused_by_another_identity',
                }
            else:
                if getattr(args, 'authorize_terminal_id', None):
                    if args.apply or not args.confirm_agent_never_started or not args.reason:
                        raise ValueError(
                            'Authorization requires --confirm-agent-never-started --reason '
                            'and a separate subsequent --apply'
                        )
                    report = authorize_launch_recovery(
                        store, intent, args.authorize_terminal_id, args.reason
                    )
                    print(json.dumps(report, ensure_ascii=False))
                    return

                report = inventory_launch_resources(intent)
                report.update(intent_id=intent['intent_id'], task_id=intent['task_id'], applied=False)
                report['recovery_command'] = (
                    'herdr-task launch-reconcile --workflow-id ' + shlex.quote(args.workflow_id)
                    + ' --node ' + shlex.quote(args.node)
                    + ' --dispatch-role ' + shlex.quote(args.dispatch_role)
                    + ' --candidate-sha ' + shlex.quote(args.candidate_sha or '')
                    + ' --dispatch-round ' + str(args.dispatch_round) + ' --apply'
                )
                if report.get('resource_status') != 'absent':
                    report['authentication_help'] = (
                        'Inspect herdr pane get <pane_id>; only with evidence no Agent start occurred, '
                        'run this command without --apply plus --authorize-terminal-id <terminal_id> '
                        '--confirm-agent-never-started --reason <evidence>; then repeat --apply. '
                        'Started/foreign instances require task recovery rather than reclamation.'
                    )
                if args.apply:
                    recovered = recover_launch_resources(store, intent)
                    report.update(recovered)
                    if recovered['status'] == 'resources_absent':
                        release_agent_reservation(intent['task_id'])
                        report['applied'] = True

            print(json.dumps(report, ensure_ascii=False))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'[LAUNCH RECONCILE REJECTED] {exc}', file=sys.stderr)
        sys.exit(2)


def capacity_definition(project):
    from herdr.workflow import normalize_workflow

    config = project.get("config")
    path = project.get("workflow_file")
    if not isinstance(config, dict) or not config.get("nodes"):
        config = json.loads(Path(path).expanduser().read_text(encoding="utf-8")) if path else project
    return normalize_workflow(config)


def check_launch_capacity(args, project, tasks, node_id, *, get_store,
                          load_capacity_definition=capacity_definition):
    from herdr.node_capacity import launch_capacity_error, node_usage

    try:
        definition = load_capacity_definition(project)
        node = next((item for item in definition.get("nodes", [])
                     if (item.get("id") or item.get("key")) == node_id), {"id": node_id})
        usage = node_usage(node, tasks, args.workflow_id)
        old_id = getattr(args, "supersedes", None)
        old = next((task for task in tasks if task.get("task_id") == old_id), None) if old_id else None
        if old_id and (not old or old.get("workflow_id") != args.workflow_id
                       or (old.get("node") or old.get("stage")) != node_id):
            raise ValueError("--supersedes must name an existing task in the same workflow/node")
        if old_id and not str(getattr(args, "supersede_reason", "") or "").strip():
            raise ValueError("Replacement requires --supersede-reason; prefer herdr-task rework")
        error = launch_capacity_error(usage)
        if error:
            raise ValueError(error)
        warning = usage["confirmation_threshold"]
        overflow = warning is not None and usage["task_count"] + 1 > warning
        if usage["legacy_max_agents"] is not None:
            print("[DEPRECATED] max_agents is a legacy dispatch switch; migrate to "
                  "max_concurrency and max_tasks_per_node")
        if overflow:
            print(f"[NODE OVERFLOW] node={node_id} cumulative tasks={usage['task_count']} "
                  f"pane_refs={usage['pane_count']} legacy max_agents={warning}")
            print("Existing task_ids: " + ", ".join(usage["task_ids"]))
            if not getattr(args, "ack_overflow", False):
                raise ValueError("Legacy cumulative threshold exceeded; use explicit --ack-overflow "
                                 "(audited), or rework the existing task")
            get_store().record_event(
                "node_overflow_acknowledged",
                {**usage, "proposed_task_count": usage["task_count"] + 1},
                workflow_id=args.workflow_id,
                node_id=node_id,
                task_id=args.task_id,
                run_id=args.run_id,
                source="herdr-task",
            )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        print(f"[NODE CAPACITY REJECTED] {exc}; existing task_ids: "
              + ", ".join(str(task.get("task_id")) for task in tasks
                          if task.get("workflow_id") == args.workflow_id
                          and (task.get("node") or task.get("stage")) == node_id))
        sys.exit(2)
