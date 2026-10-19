"""CLI operations that inspect or renew existing task resources."""

import json
import subprocess
import sys


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
