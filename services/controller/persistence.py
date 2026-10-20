"""Read authoritative tasks and workflow records from the existing StateStore.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def _get_store(ports):
    if ports.os.environ.get("HERDR_STATE_DB"):
        return ports.get_state_store(ports.Path(ports.os.environ["HERDR_STATE_DB"]))
    t_file = getattr(ports, 'TASKS_FILE', None) or ports.os.environ.get("TASKS_FILE")
    if t_file:
        p = ports.Path(t_file).parent / "state.db"
        if p.parent.exists():
            return ports.get_state_store(db_path=p)
    w_file = getattr(ports, 'WORKFLOWS_FILE', None) or ports.os.environ.get("WORKFLOWS_FILE")
    if w_file:
        p = ports.Path(w_file).parent / "state.db"
        if p.parent.exists():
            return ports.get_state_store(db_path=p)
    return ports.get_state_store()

def _workflow_entry(ports, workflow_id):
    store = ports._get_store()
    wf = store.get_workflow(workflow_id)
    if wf:
        return wf
    return {}

def workflow_closed(ports, workflow_id):
    """Registry entry reached a terminal (completed) status."""
    return ports._workflow_entry(workflow_id).get("status") == "completed"


# Registry
def load_tasks(ports):
    store = ports._get_store()
    return store.list_tasks()

def get_task(ports, task_id):
    store = ports._get_store()
    return store.get_task(task_id)

def active_registered_workflows(ports):
    workflows = set()
    store = ports._get_store()
    for wf in store.list_workflows():
        wid = wf.get("workflow_id")
        if not wid or wf.get("status") == "completed":
            continue

        # 夹具/临时残留(pytest tmp、已删除的 workflow_file)绝不允许进入调度 sweep,
        # 否则会对不存在的 pane 无限重试并淹没日志。
        if ports.liveness.workflow_is_foreign(wf):
            if wid not in ports._foreign_workflows_logged:
                ports._foreign_workflows_logged.add(wid)
                print(
                    f"[WORKFLOW FOREIGN SKIPPED] "
                    f"workflow={wid} "
                    f"file={wf.get('workflow_file')} "
                    f"(fixture/temp residue; excluded from scheduling)"
                )
            continue

        workflows.add(wid)
    return workflows


DEPENDENCIES = {'_get_store': ('Path', 'TASKS_FILE', 'WORKFLOWS_FILE', 'get_state_store', 'os'),
 '_workflow_entry': ('_get_store',),
 'workflow_closed': ('_workflow_entry',),
 'load_tasks': ('_get_store',),
 'get_task': ('_get_store',),
 'active_registered_workflows': ('_foreign_workflows_logged', '_get_store', 'liveness')}

__all__ = tuple(DEPENDENCIES)
