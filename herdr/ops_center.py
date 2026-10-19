"""Read-only operations projection using one authoritative SQLite snapshot."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from herdr.console_queries import read_inventory
from herdr.ops_center_helpers import TERMINAL_STATUSES, _agent_runtime_status
from herdr.ops_center_projection import _ops_center_payload
from herdr.projects import validate_required_task_scope, requirement_subject
from herdr.workflow import normalize_workflow

MAX_RUNTIME_PROBES = 8
RUNTIME_WORKERS = 8
_runtime_pool = ThreadPoolExecutor(max_workers=RUNTIME_WORKERS, thread_name_prefix="ops-runtime")
_runtime_admission = threading.BoundedSemaphore(RUNTIME_WORKERS)


def _default_runtime_probe(pane_id):
    return _agent_runtime_status(pane_id)


def build_ops_center_payload(workflow_id=None, include_tasks=False, *, db_path=None,
                             now=None, runtime_probe=None):
    """Project one SQLite snapshot with bounded, shared runtime probes."""
    tasks, workflows = read_inventory(db_path)
    inventory = {t["task_id"]: t for t in tasks}

    class SnapshotTasks:
        def get_task(self, task_id):
            return inventory.get(task_id)

    def lookup(wid):
        record = workflows.get(wid)
        if record is None:
            return {"configuration_error": "workflow_config_invalid"}, wid
        label = (record.get("title") or record.get("requirement_subject")
            or requirement_subject(record.get("requirement", "")))
        try:
            config = record.get("config") or record
            if record.get("workflow_file"):
                try:
                    config = json.loads(Path(record["workflow_file"]).expanduser().read_text())
                except OSError:
                    pass
            config = validate_required_task_scope(normalize_workflow(config), wid, store=SnapshotTasks())
            return config, label or config.get("name") or wid
        except Exception:
            return {"configuration_error": "workflow_config_invalid"}, label or wid

    relevant = [t for t in tasks if not workflow_id or t.get("workflow_id") == workflow_id]
    panes = sorted({t.get("pane_id") for t in relevant
                    if t.get("pane_id") and t.get("status") not in TERMINAL_STATUSES})
    probe = runtime_probe or _default_runtime_probe

    def safe_probe(pane):
        try:
            return probe(pane)
        except Exception:
            return None

    def admitted_probe(pane):
        try:
            return safe_probe(pane)
        finally:
            _runtime_admission.release()

    futures = {}
    for pane in panes[:MAX_RUNTIME_PROBES]:
        if not _runtime_admission.acquire(blocking=False):
            continue
        try:
            futures[pane] = _runtime_pool.submit(admitted_probe, pane)
        except Exception:
            _runtime_admission.release()
            raise
    runtimes = {pane: future.result() for pane, future in futures.items()}
    payload = _ops_center_payload(
        workflow_id, now, include_tasks, snapshot_tasks=tasks,
        workflow_lookup=lookup, runtime_probe=runtimes.get)
    payload["runtime_sampling"] = {"sampled": len(runtimes), "omitted": len(panes) - len(runtimes)}
    return payload


def handle_cli(args, *, db_path=None):
    payload = build_ops_center_payload(
        workflow_id=getattr(args, "workflow_id", None),
        include_tasks=getattr(args, "include_tasks", False), db_path=db_path)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
