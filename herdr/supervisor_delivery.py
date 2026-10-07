"""Receipt-v1 prompt delivery phases; ambiguous native I/O requires attention."""
import json
import shlex
import time
from . import state_db
from .completion_receipt import issue_completion_contract, _expiry_error
from .workflow_docs import cli_path
from .task_resources import workflow_launch_lock

class DeliveryUnknown(RuntimeError):
    def __init__(self, action, intervention_id):
        super().__init__(f'{action} delivery outcome unknown; retained intent requires attention')
        self.intervention_error = {'code':'supervisor_delivery_unknown', 'action':action,
            'intervention_id':intervention_id,'side_effects':'unknown','delivery_pending':True}


def current_delivery_task(task, store, *, native=False):
    """Check current authority; native transports also require instance proof."""
    fresh = store.get_task(task['task_id']) or {}
    workflow = store.get_workflow(task.get('workflow_id')) or {}
    if (fresh.get('status') not in ('dispatched','working','rework')
            or not workflow or workflow.get('status') in {'closing','completed','failed','halted','abandoned'}
            or fresh.get('workflow_id') != task.get('workflow_id')
            or fresh.get('run_id') != task.get('run_id')
            or fresh.get('pane_id') != task.get('pane_id')):
        raise DeliveryUnknown('DELIVERY', task['task_id'])
    if native:
        from .task_resources import owned_live_pane
        owned, _ = owned_live_pane(fresh)
        if not owned:
            raise DeliveryUnknown('DELIVERY', task['task_id'])
        after_probe = current_delivery_task(task, store)
        if any(after_probe.get(key) != fresh.get(key) for key in
               ('run_id','pane_id','completion_epoch','completion_identity_path')) or _expiry_error(after_probe, time.time()):
            raise DeliveryUnknown('DELIVERY', task['task_id'])
        return after_probe
    return fresh


def deliver(task, store, action, payload, prompt, send, *, boundary=lambda phase: None, prepare_task=None):
    """Serialize one intervention, resume prepared work, never resend unknown I/O."""
    prefix = {'RETRY':'retry','VERIFY':'verification','INITIAL':'initial','REWORK':'rework','RENEW':'renewal'}[action]
    intervention_id = payload.get('intervention_id')
    if not intervention_id:
        raise ValueError('Receipt delivery requires intervention identity')
    identity = dict(workflow_id=task.get('workflow_id'), node_id=task.get('node') or task.get('stage'),
                    task_id=task['task_id'], agent_id=task.get('agent'), source=('herdr-task' if action in ('INITIAL','REWORK','RENEW') else 'supervisor'),run_id=task['run_id'])
    def record(event_type, body, conn=None):
        if conn is not None:
            return state_db.record_event(dict(identity,event_type=event_type,payload=body),conn=conn)
        return store.record_event(event_type,body,**identity)
    with workflow_launch_lock(store.db_path, f"supervisor-delivery:{task['task_id']}:{task['run_id']}"):
        conn=state_db.get_db_connection(store.db_path)
        try:
            rows=conn.execute("SELECT event_type,payload_json FROM events WHERE task_id=? AND run_id=? AND source=? AND json_extract(payload_json,'$.intervention_id')=? AND event_type IN (?,?,?,?) ORDER BY id DESC LIMIT 4",
                (task['task_id'],task['run_id'],identity['source'],intervention_id,prefix+'_dispatch_prepared',prefix+'_transport_started',prefix+'_dispatched',prefix+'_dispatch_intent')).fetchall()
        finally:
            conn.close()
        phases={row['event_type']:json.loads(row['payload_json']) for row in rows}
        if prefix+'_dispatched' in phases:
            fresh = store.get_task(task['task_id']) or {}
            if fresh.get('completion_identity_path') != phases[prefix+'_dispatched'].get('identity_path'):
                raise DeliveryUnknown(action,intervention_id)
            return {prefix+'_dispatched':True,'already_applied':True}
        if prefix+'_transport_started' in phases:
            raise DeliveryUnknown(action,intervention_id)
        prepared=phases.get(prefix+'_dispatch_prepared')
        if prepared is None and prefix+'_dispatch_intent' in phases:
            raise DeliveryUnknown(action,intervention_id)
        if prepared is None and ((action == 'INITIAL' and task.get('status') == 'dispatched')
                or (action == 'REWORK' and task.get('rework_delivery') == 'pending')):
            raise DeliveryUnknown(action,intervention_id)
        if prepared is None:
            boundary('before_prepared')
            def prepare(conn,contract):
                nonlocal prepared
                if prepare_task is not None:
                    prepare_task(conn)
                from .task_delivery import instruction_block
                extra = instruction_block(task)
                if action in ('INITIAL', 'REWORK'):
                    from .task_checkpoint import checkpoint_instruction_block
                    extra += checkpoint_instruction_block(dict(task, completion_epoch=contract['epoch']), contract['epoch'])
                body=dict(payload,completion_epoch=contract['epoch'],identity_path=contract['path'],
                    prompt=prompt+extra+'\nWhen finished, submit the structured completion declaration:\n'+shlex.quote(str(cli_path()))+' report-completion '+shlex.quote(task['task_id'])+' --identity-file '+shlex.quote(contract['path'])+'\nThe server credential expires after 24 hours; ask the controller to renew and deliver a new contract if expired.\nThis declaration does not certify acceptance, merge, or deployment.\n')
                record(prefix+'_dispatch_prepared',body,conn)
                prepared=body
            issue_completion_contract(task['task_id'],store,prepare=prepare,renew=(action == 'RENEW'))
            boundary('after_prepared')
        fresh=store.get_task(task['task_id']) or {}
        if (fresh.get('run_id') != task['run_id'] or fresh.get('completion_epoch') != prepared['completion_epoch']
                or fresh.get('completion_identity_path') != prepared['identity_path']
                or _expiry_error(fresh, time.time())):
            raise DeliveryUnknown(action,intervention_id)
        from .workflow_close import workflow_lifecycle_lock
        with workflow_lifecycle_lock(store, task.get('workflow_id')) as acquired:
            if not acquired:
                raise DeliveryUnknown(action, intervention_id)
            current_delivery_task(task, store)
            record(prefix+'_transport_started',dict(prepared,delivery_phase='transport_started'))
            boundary('before_prompt')
            try:
                current = current_delivery_task(task, store)
                if (current.get('completion_epoch') != prepared['completion_epoch']
                        or current.get('completion_identity_path') != prepared['identity_path']
                        or _expiry_error(current, time.time())):
                    raise DeliveryUnknown(action, intervention_id)
                send(current['pane_id'],prepared['prompt'])
            except Exception as exc:
                raise DeliveryUnknown(action,intervention_id) from exc
            boundary('after_prompt')
            record(prefix+'_dispatched',dict(prepared,delivery_phase='dispatched'))
            return {prefix+'_dispatched':True}
