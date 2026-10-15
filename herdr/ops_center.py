"""Read-only operations projection using one authoritative SQLite snapshot.

The CLI owns the established aggregation helpers. Loading it once as a Python
module preserves those semantics and its backwards-compatible patchable test
surface without subprocess startup, StateStore initialization, or shared request
context. All inputs passed to the aggregation are request-local.
"""
import importlib.machinery
import importlib.util
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from herdr.console_queries import read_inventory
from herdr.projects import validate_required_task_scope, requirement_subject
from herdr.workflow import normalize_workflow

_aggregation = None
_aggregation_lock = threading.Lock()
MAX_RUNTIME_PROBES = 8
RUNTIME_WORKERS = 8
_runtime_pool = ThreadPoolExecutor(max_workers=RUNTIME_WORKERS, thread_name_prefix="ops-runtime")
_runtime_admission = threading.BoundedSemaphore(RUNTIME_WORKERS)


def _helpers():
    global _aggregation
    with _aggregation_lock:
        if _aggregation is None:
            path = Path(__file__).resolve().parent.parent / "bin" / "herdr-task"
            loader = importlib.machinery.SourceFileLoader("_herdr_ops_aggregation", str(path))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            _aggregation = module
    return _aggregation


def _default_runtime_probe(pane_id):
    return _helpers()._agent_runtime_status(pane_id)


def build_ops_center_payload(workflow_id=None, include_tasks=False, *, db_path=None,
                             now=None, runtime_probe=None):
    """Project operations without migrations, writes, or per-workflow DB reads.

    Runtime probing has at most 8 distinct panes and 8 concurrent workers; the
    remaining panes are explicitly unknown, never inferred to be alive. Each
    default external probe has its existing three-second timeout.
    """
    tasks, workflows = read_inventory(db_path)
    helpers = _helpers()
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
            # Retain the CLI's legacy external-definition behavior, resolving
            # required task identities against this same snapshot.
            if record.get("workflow_file"):
                try:
                    config = json.loads(Path(record["workflow_file"]).expanduser().read_text())
                except OSError:
                    pass
            config = validate_required_task_scope(normalize_workflow(config), wid, store=SnapshotTasks())
            return config, label or config.get("name") or wid
        except Exception:  # One malformed workflow must not break the entire inventory.
            return {"configuration_error": "workflow_config_invalid"}, label or wid
    relevant = [t for t in tasks if not workflow_id or t.get("workflow_id") == workflow_id]
    panes = sorted({t.get("pane_id") for t in relevant
                    if t.get("pane_id") and t.get("status") not in helpers.TERMINAL_STATUSES})
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
    payload = helpers._ops_center_payload(workflow_id, now, include_tasks,
        snapshot_tasks=tasks, workflow_lookup=lookup, runtime_probe=runtimes.get)
    payload["runtime_sampling"] = {"sampled": len(runtimes), "omitted": len(panes) - len(runtimes)}
    return payload
