"""Acceptance Test Suite for Universal Workflow Shutdown Protocol (Review Round 14 / P1).

Covers the 3 required acceptance scenarios:
1. While workflow is closing/cancelling, Coordinator attempts to dispatch B -> B must be rejected.
2. When B has entered launch process but is not yet registered, shutdown coordinates cleanly -> B is not orphaned.
3. All agents must be confirmed stopped before workflow enters terminal state and releases workspace.
"""
import json
import os
import signal
import sqlite3
import shutil
import tempfile
import subprocess
import sys
import time
from pathlib import Path
import pytest

from herdr.inbox.store import InboxStore
from herdr.inbox.dispatcher import InboxDispatcher
from herdr.workflow_shutdown import shutdown_workflow
from herdr.state_db import save_workflow, get_workflow, save_task, get_task
from herdr import node_dispatch as core
from herdr import node_dispatch_store as nds
from herdr import task_resources


@pytest.fixture
def test_env(tmp_path: Path):
    db_file = tmp_path / "shutdown_test.db"
    store = InboxStore(db_file)
    disp = InboxDispatcher(store=store, db_path=db_file)
    return {
        "tmp_path": tmp_path,
        "db_file": db_file,
        "store": store,
        "disp": disp,
    }


def test_scenario_1_coordinator_dispatch_rejected_when_workflow_closing(test_env, monkeypatch):
    """Scenario 1: While A is being cancelled/closing, Coordinator concurrently attempts to dispatch B.

    B MUST be rejected:
    - node_dispatch.active(wf) returns False.
    - node_dispatch_store.claim returns None.
    - node_dispatch_store.start raises ValueError.
    - node_dispatch_store.validate_unbound_launch raises ValueError.
    - task_resources.begin_launch_intent raises ValueError.
    """
    db_file = test_env["db_file"]
    wf_id = "wf_scene_1"
    ws_id = "ws_scene_1"

    config = {
        "workflow_template": "software-development-v1",
        "nodes": [
            {"id": "node_A", "node_type": "agent", "roles": ["worker"]},
            {"id": "node_B", "node_type": "agent", "roles": ["worker"], "depends_on": ["node_A"]},
        ],
    }

    save_workflow(
        {
            "workflow_id": wf_id,
            "project_id": "proj_1",
            "workspace_id": ws_id,
            "title": "Shutdown Test WF",
            "status": "running",
            "execution_id": "exec_1",
            "config": config,
        },
        db_path=db_file,
    )
    save_task(
        {
            "task_id": "task_A",
            "workflow_id": wf_id,
            "node": "node_A",
            "status": "working",
            "pane_id": "%10",
        },
        db_path=db_file,
    )

    # Simulate workflow entering CLOSING state (Step 1 of shutdown protocol)
    from herdr.state_db import transition_workflow
    transition_workflow(wf_id, to_status="closing", reason="cancelling workflow", db_path=db_file, force=True)

    wf = get_workflow(wf_id, db_path=db_file)
    assert wf["status"] == "closing"

    # 1. Verification: core.active(workflow) is strictly FALSE for closing workflows
    assert core.active(wf) is False

    # 2. Verification: node_dispatch_store.claim refuses to claim operations for closing workflows
    # Seed an operation in workflow_recovery_operations
    now = time.time()
    conn = sqlite3.connect(str(db_file))
    cur = conn.execute(
        """
        INSERT INTO workflow_recovery_operations
        (workflow_id, identity_key, status, payload_json, detail_json, owner, attempts, next_due_at, lease_until, created_at, updated_at, version)
        VALUES (?, ?, 'pending', ?, '{}', NULL, 0, ?, ?, ?, ?, 1)
        """,
        (
            wf_id,
            "node_dispatch:test_b",
            json.dumps({"kind": "node_dispatch", "node_id": "node_B", "generation": ["exec_1", None, None]}),
            now - 10,
            now + 60,
            now,
            now,
        ),
    )
    op_id = cur.lastrowid
    conn.commit()
    conn.close()

    claimed_op = nds.claim(db_file, op_id, owner="coordinator_1", now=now)
    assert claimed_op is None, "Coordinator MUST NOT claim dispatch for a closing workflow"

    # 3. Verification: validate_unbound_launch rejects unbound launches on closing workflow
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    with pytest.raises(ValueError, match="is closing"):
        nds.validate_unbound_launch(conn, wf_id, "node_B")
    conn.close()

    # 4. Verification: task_resources.begin_launch_intent rejects launch intents on closing workflow
    from herdr.state_store import SQLiteStateStore
    store = SQLiteStateStore(str(db_file))
    with pytest.raises(ValueError, match="Cannot begin launch intent: workflow 'wf_scene_1' is closing"):
        task_resources.begin_launch_intent(
            store,
            workflow_id=wf_id,
            node_id="node_B",
            task_id="task_B_attempt",
        )


@pytest.fixture
def native_pane(tmp_path, monkeypatch):
    if not shutil.which("herdr"):
        pytest.skip("Herdr CLI required for the isolated native pane test")
    monkeypatch.delenv("HERDR_SESSION", raising=False)
    runtime = tempfile.TemporaryDirectory(dir="/tmp", prefix="hfstop-")
    monkeypatch.setenv("HERDR_HOME", runtime.name)
    monkeypatch.setenv("HERDR_SOCKET_PATH", runtime.name + "/herdr.sock")
    monkeypatch.setenv("HERDR_CLIENT_SOCKET_PATH", runtime.name + "/client.sock")
    server = subprocess.Popen(["herdr", "server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        def cli(*args):
            result = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=3, check=True)
            return json.loads(result.stdout)["result"] if result.stdout.strip() else None

        deadline = time.monotonic() + 5
        while True:
            try:
                cli("pane", "list")
                break
            except subprocess.CalledProcessError:
                if server.poll() is not None or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        created = cli("workspace", "create", "--cwd", str(tmp_path))
        pane = created["root_pane"]
        # Keep a second pane so closing the test pane is permitted by Herdr.
        cli("pane", "split", pane["pane_id"], "--direction", "right", "--cwd", runtime.name)
        yield pane, cli
    finally:
        server.terminate()
        server.communicate(timeout=5)
        runtime.cleanup()


def _start_workflow(test_env):
    db = test_env["db_file"]
    save_workflow({"workflow_id": "wf_stop", "workspace_id": "ws_stop", "project_id": "proj",
                   "status": "running", "config": {"nodes": [{"id": "node", "node_type": "agent"}]}},
                  db_path=db)
    store = test_env["store"]
    task = store.create_task(title="Stop", instruction="Stop", project_id="proj", workspace_id="ws_stop", auto_confirm=True)
    store.update_task_status(task["id"], new_status="CLAIMED")
    store.update_task_status(task["id"], new_status="RUNNING", workflow_id="wf_stop")
    return task["id"]


def _start_resistant_process(pane, cli, tmp_path):
    import shlex
    ready = tmp_path / "ready"
    code = ("import os, signal, subprocess, sys, time; from pathlib import Path; "
            "signal.signal(signal.SIGINT, signal.SIG_IGN); "
            "desc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
            "start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
            "stderr=subprocess.DEVNULL); "
            f"Path({str(ready)!r}).write_text(f'{{os.getpid()}} {{desc.pid}}'); "
            "time.sleep(30)")
    cli("pane", "run", pane, shlex.join([sys.executable, "-c", code]))
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.03)
    foreground, descendant = ready.read_text().split()
    return int(foreground), int(descendant)


def test_scenario_2_launcher_crash_preserves_unregistered_pane_and_occupancy(test_env, native_pane):
    from herdr.state_store import SQLiteStateStore

    _start_workflow(test_env)
    db = test_env["db_file"]
    pane, cli = native_pane
    clone = test_env["tmp_path"]
    store = SQLiteStateStore(str(db))
    with task_resources.workflow_launch_lock(db, "wf_stop"):
        intent = task_resources.begin_launch_intent(
            store, workflow_id="wf_stop", node_id="node", task_id="unregistered", run_id="run",
        )["intent"]
        intent = task_resources.record_launch_resources(store, intent, {"planned_clone_path": str(clone), "run_id": "run"})
        # A launcher process starts the pane command and dies without publishing
        # its runtime identity or registering the Task.
        launcher = subprocess.run([sys.executable, "-c", """
import os, subprocess, sys
subprocess.run(["herdr", "pane", "run", sys.argv[1], "sleep 30"], check=True)
os._exit(17)
""", pane["pane_id"]], capture_output=True, timeout=5)
        assert launcher.returncode == 17
    assert get_task("unregistered", db_path=db) is None
    deadline = time.monotonic() + 3
    while True:
        processes = cli("pane", "process-info", "--pane", pane["pane_id"])["process_info"]
        if any(proc["name"] == "sleep" for proc in processes["foreground_processes"]):
            break
        assert time.monotonic() < deadline
        time.sleep(0.03)
    assert shutdown_workflow("wf_stop", db_path=db) == "STOP_FAILED"
    assert task_resources._latest_intent(store, intent["key"])["phase"] == "allocating"
    assert cli("pane", "get", pane["pane_id"])["pane"]["terminal_id"] == pane["terminal_id"]
    assert get_workflow("wf_stop", db_path=db)["status"] == "closing"
    assert test_env["disp"].is_workspace_busy("ws_stop")[0]


def test_scenario_3_interrupt_ack_keeps_occupancy_until_process_stops(test_env, native_pane, monkeypatch):
    task_id = _start_workflow(test_env)
    db = test_env["db_file"]
    pane, cli = native_pane
    pane_id = pane["pane_id"]
    pid, descendant = _start_resistant_process(pane_id, cli, test_env["tmp_path"])
    save_task({"task_id": "worker", "workflow_id": "wf_stop", "run_id": "run", "status": "working",
               "pane_id": pane_id, "agent": "codex"}, db_path=db)
    monkeypatch.setattr(task_resources, "owned_live_pane", lambda task: (True, "identity_match"))

    from herdr import workflow_shutdown
    adapter = workflow_shutdown.get_agent_adapter("codex")
    real_interrupt = adapter.interrupt
    close = workflow_shutdown.close_pane
    sent = []

    def interrupt(*args, **kwargs):
        result = real_interrupt(*args, **kwargs)
        sent.append(result)
        return result

    monkeypatch.setattr(adapter, "interrupt", interrupt)
    monkeypatch.setattr(workflow_shutdown, "get_agent_adapter", lambda agent: adapter)
    try:
        monkeypatch.setattr(workflow_shutdown, "close_pane", lambda pane_id: True)
        with pytest.raises(RuntimeError, match="failed to stop"):
            test_env["store"].cancel_task(task_id)
        os.kill(pid, 0)
        assert sent == [True]
        assert get_workflow("wf_stop", db_path=db)["status"] == "closing"
        assert get_task("worker", db_path=db)["status"] == "working"
        assert test_env["disp"].is_workspace_busy("ws_stop")[0]

        # The real close removes the pane, but the observed detached child survives.
        monkeypatch.setattr(workflow_shutdown, "close_pane", close)
        with pytest.raises(RuntimeError, match="failed to stop"):
            test_env["store"].cancel_task(task_id)
        assert not any(p["pane_id"] == pane_id for p in cli("pane", "list")["panes"])
        os.kill(descendant, 0)
        assert test_env["disp"].is_workspace_busy("ws_stop")[0]
        # A separate lifecycle caller drops the database's pane reference.
        # The durable stop receipt still fences cancellation.
        from herdr.state_db import transition_task
        transition_task("worker", "failed", reason="test: concurrent lifecycle", db_path=db,
                        metadata={"pane_id": None}, force=True)
        with pytest.raises(RuntimeError, match="failed to stop"):
            test_env["store"].cancel_task(task_id)
    finally:
        try:
            os.kill(descendant, signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + 3
    while workflow_shutdown._terminal_processes(-1, [descendant]):
        assert time.monotonic() < deadline
        time.sleep(0.03)
    assert test_env["store"].cancel_task(task_id)["status"] == "CANCELLED"
    assert get_task("worker", db_path=db)["status"] == "failed"
    assert get_workflow("wf_stop", db_path=db)["status"] == "completed"
    assert not test_env["disp"].is_workspace_busy("ws_stop")[0]


def test_allocation_worker_keeps_workflow_fenced_after_launcher_exit(test_env):
    """The allocating child outlives its launcher and retains the actual flock."""
    _start_workflow(test_env)
    db = test_env["db_file"]
    worker_release = test_env["tmp_path"] / "worker-release"
    launcher_code = """
import os, subprocess, sys
from herdr.task_resources import workflow_launch_lock
with workflow_launch_lock(sys.argv[1], "wf_stop") as fd:
    subprocess.Popen([sys.executable, "-c", sys.argv[2], *sys.argv[3:]], pass_fds=(fd,),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os._exit(17)
"""
    worker_code = """
import sys, time
from pathlib import Path
deadline = time.monotonic() + 10
while not Path(sys.argv[1]).exists() and time.monotonic() < deadline:
    time.sleep(0.02)
"""
    result = subprocess.run([sys.executable, "-c", launcher_code, str(db), worker_code,
                             str(worker_release)], timeout=5)
    assert result.returncode == 17
    try:
        assert shutdown_workflow("wf_stop", db_path=db, timeout=0.1) == "STOP_FAILED"
        assert get_workflow("wf_stop", db_path=db)["status"] == "closing"
        assert test_env["disp"].is_workspace_busy("ws_stop")[0]
    finally:
        worker_release.touch()
    assert shutdown_workflow("wf_stop", db_path=db, timeout=3) == "STOPPED"


def test_owned_unregistered_pane_is_stopped_and_clone_is_retained(test_env, native_pane):
    from herdr.state_store import SQLiteStateStore

    task_id = _start_workflow(test_env)
    db = test_env["db_file"]
    pane, cli = native_pane
    pane_id = pane["pane_id"]
    clone = test_env["tmp_path"]
    store = SQLiteStateStore(str(db))
    intent = task_resources.begin_launch_intent(
        store, workflow_id="wf_stop", node_id="node", task_id="unregistered", run_id="run",
    )["intent"]
    intent = task_resources.record_launch_resources(store, intent, {"planned_clone_path": str(clone), "run_id": "run"})
    task_resources.write_worker_launch_identity(clone, {
        "intent_id": intent["intent_id"], "task_id": "unregistered", "run_id": "run",
        "phase": "pane_allocated", "pane_source": "dynamic", "pane_id": pane_id,
        "terminal_id": pane["terminal_id"],
    }, initial=True)

    assert test_env["store"].cancel_task(task_id)["status"] == "CANCELLED"
    assert get_task("unregistered", db_path=db) is None
    assert task_resources._latest_intent(store, intent["key"])["phase"] == "shutdown_stopped"
    assert clone.is_dir()
    assert not any(p["pane_id"] == pane_id for p in cli("pane", "list")["panes"])
    assert not test_env["disp"].is_workspace_busy("ws_stop")[0]
    assert shutdown_workflow("wf_stop", db_path=db) == "ALREADY_TERMINAL"


def test_unallocated_intent_is_settled_only_after_inventory_succeeds(test_env, native_pane, monkeypatch):
    from herdr.state_store import SQLiteStateStore

    inbox_id = _start_workflow(test_env)
    db = test_env["db_file"]
    store = SQLiteStateStore(str(db))
    intent = task_resources.begin_launch_intent(
        store, workflow_id="wf_stop", node_id="node", task_id="unallocated", run_id="run",
    )["intent"]
    intent = task_resources.record_launch_resources(store, intent, {
        "planned_clone_path": str(test_env["tmp_path"] / "not-allocated"), "run_id": "run",
    })
    inventory = task_resources.inventory_launch_resources

    def failed_inventory(*args, **kwargs):
        raise OSError("runtime inventory unavailable")

    monkeypatch.setattr(task_resources, "inventory_launch_resources", failed_inventory)
    assert shutdown_workflow("wf_stop", db_path=db) == "STOP_FAILED"
    assert get_workflow("wf_stop", db_path=db)["status"] == "closing"
    assert task_resources._latest_intent(store, intent["key"])["phase"] == "allocating"

    monkeypatch.setattr(task_resources, "inventory_launch_resources", inventory)
    assert test_env["store"].cancel_task(inbox_id)["status"] == "CANCELLED"
    assert task_resources._latest_intent(store, intent["key"])["phase"] == "resources_absent"
    assert not test_env["disp"].is_workspace_busy("ws_stop")[0]


@pytest.mark.parametrize("task_has_pane", [True, False])
def test_reused_pane_waits_for_original_process_then_releases(test_env, monkeypatch, task_has_pane):
    from herdr import workflow_shutdown
    from herdr.state_store import SQLiteStateStore

    inbox_id = _start_workflow(test_env)
    db = test_env["db_file"]
    pane_id = "w1:p1"
    if task_has_pane:
        save_task({"task_id": "old-task", "workflow_id": "wf_stop", "run_id": "old-run",
                   "status": "working", "pane_id": pane_id}, db_path=db)

    def unexpected_operation(*args, **kwargs):
        pytest.fail("shutdown must not operate on the replacement terminal")

    monkeypatch.setattr(workflow_shutdown, "_pane", lambda _: {"terminal_id": "new-terminal"})
    monkeypatch.setattr(workflow_shutdown, "close_pane", unexpected_operation)
    monkeypatch.setattr(workflow_shutdown, "get_agent_adapter", unexpected_operation)
    monkeypatch.setattr(task_resources, "owned_live_pane", unexpected_operation)
    store = SQLiteStateStore(str(db))
    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                          start_new_session=True) as old_process:
        try:
            store.record_event("shutdown_process", {
                "identity": {"pane_id": pane_id, "run_id": "old-run", "intent_id": None},
                "phase": "STOP_PENDING", "terminal_id": "old-terminal",
                "shell": old_process.pid, "pids": [old_process.pid],
            }, workflow_id="wf_stop", task_id="old-task", source="workflow_shutdown")

            with pytest.raises(RuntimeError, match="failed to stop"):
                test_env["store"].cancel_task(inbox_id)
            assert get_workflow("wf_stop", db_path=db)["status"] == "closing"
            assert test_env["disp"].is_workspace_busy("ws_stop")[0]
        finally:
            old_process.terminate()
            old_process.wait(timeout=3)

    # A malformed observation still cannot establish that the original pane is gone.
    with monkeypatch.context() as probe:
        probe.setattr(workflow_shutdown, "_pane", lambda _: {})
        with pytest.raises(RuntimeError, match="failed to stop"):
            test_env["store"].cancel_task(inbox_id)

    assert test_env["store"].cancel_task(inbox_id)["status"] == "CANCELLED"
    receipt = store.list_events(workflow_id="wf_stop", task_id="old-task",
                                event_type="shutdown_process", desc=True, limit=1)[0]["payload"]
    assert receipt["phase"] == "PROCESS_STOPPED"
    assert receipt["terminal_id"] == "old-terminal"
    assert not test_env["disp"].is_workspace_busy("ws_stop")[0]


def test_missing_unregistered_pane_without_process_receipt_preserves_occupancy(test_env, native_pane):
    from herdr.state_store import SQLiteStateStore

    inbox_id = _start_workflow(test_env)
    db = test_env["db_file"]
    pane, cli = native_pane
    pane_id = pane["pane_id"]
    store = SQLiteStateStore(str(db))
    intent = task_resources.begin_launch_intent(
        store, workflow_id="wf_stop", node_id="node", task_id="unregistered", run_id="run",
    )["intent"]
    # The launcher persists a real pane identity, then crashes before Task
    # registration or shutdown can capture the terminal's process identities.
    launcher = subprocess.run([sys.executable, "-c", """
import json, os, sys
from herdr.state_store import SQLiteStateStore
from herdr.task_resources import record_launch_resources, workflow_launch_lock
db, intent, resources = sys.argv[1:]
with workflow_launch_lock(db, "wf_stop"):
    record_launch_resources(SQLiteStateStore(db), json.loads(intent), json.loads(resources))
    os._exit(17)
""", str(db), json.dumps(intent), json.dumps({
        "planned_clone_path": str(test_env["tmp_path"] / "removed-clone"),
        "run_id": "run", "pane_id": pane_id, "terminal_id": pane["terminal_id"],
    })], timeout=5)
    assert launcher.returncode == 17
    intent = task_resources._latest_intent(store, intent["key"])
    _, descendant = _start_resistant_process(pane_id, cli, test_env["tmp_path"])
    try:
        cli("pane", "close", pane_id)
        assert task_resources.inventory_launch_resources(intent)["resource_status"] == "absent"
        assert get_task("unregistered", db_path=db) is None
        assert not store.list_events(workflow_id="wf_stop", event_type="shutdown_process")
        os.kill(descendant, 0)
        with task_resources.workflow_launch_lock(db, "wf_stop"):
            assert task_resources.recover_launch_resources(store, intent)["status"] == "recovery_required"

        with pytest.raises(RuntimeError, match="failed to stop"):
            test_env["store"].cancel_task(inbox_id)
        assert get_workflow("wf_stop", db_path=db)["status"] == "closing"
        assert task_resources._latest_intent(store, intent["key"])["phase"] == "allocating"
        assert test_env["disp"].is_workspace_busy("ws_stop")[0]
        assert not store.list_events(workflow_id="wf_stop", event_type="shutdown_process")
    finally:
        try:
            os.kill(descendant, signal.SIGTERM)
        except ProcessLookupError:
            pass


def test_reused_unregistered_pane_allows_inventory_to_settle_original_intent(test_env, monkeypatch):
    from herdr import workflow_shutdown
    from herdr.state_store import SQLiteStateStore

    inbox_id = _start_workflow(test_env)
    db = test_env["db_file"]
    store = SQLiteStateStore(str(db))
    pane_id = "w1:p1"
    intent = task_resources.begin_launch_intent(
        store, workflow_id="wf_stop", node_id="node", task_id="unregistered", run_id="old-run",
    )["intent"]
    planned = test_env["tmp_path"] / "old-clone"
    intent = task_resources.record_launch_resources(store, intent, {
        "planned_clone_path": str(planned), "run_id": "old-run", "pane_id": pane_id,
    })
    previous = subprocess.run([sys.executable, "-c", "print(__import__('os').getpid())"],
                              start_new_session=True, capture_output=True, text=True, check=True)
    pid = int(previous.stdout)
    store.record_event("shutdown_process", {
        "identity": {"pane_id": pane_id, "run_id": "old-run", "intent_id": intent["intent_id"]},
        "phase": "STOP_PENDING", "terminal_id": "old-terminal", "shell": pid, "pids": [pid],
    }, workflow_id="wf_stop", task_id=intent["key"], source="workflow_shutdown")

    replacement = {"pane_id": pane_id, "terminal_id": "new-terminal", "cwd": str(planned)}
    inventory = task_resources.inventory_launch_resources

    def run_inventory(argv, timeout):
        assert argv == ["herdr", "pane", "list"]
        return 0, json.dumps({"result": {"panes": [replacement]}}), ""

    monkeypatch.setattr(task_resources, "inventory_launch_resources",
                        lambda intent, **kw: inventory(intent, runner=run_inventory, **kw))
    monkeypatch.setattr(workflow_shutdown, "_pane", lambda _: replacement)
    monkeypatch.setattr(workflow_shutdown, "close_pane",
                        lambda _: pytest.fail("must not close the replacement terminal"))

    # Even a different terminal blocks settlement while it uses the old clone.
    with pytest.raises(RuntimeError, match="failed to stop"):
        test_env["store"].cancel_task(inbox_id)
    assert task_resources._latest_intent(store, intent["key"])["phase"] == "allocating"
    assert test_env["disp"].is_workspace_busy("ws_stop")[0]

    replacement["cwd"] = str(test_env["tmp_path"])
    assert test_env["store"].cancel_task(inbox_id)["status"] == "CANCELLED"
    assert task_resources._latest_intent(store, intent["key"])["phase"] == "resources_absent"


@pytest.mark.parametrize("start_requested", [False, True])
def test_worker_crash_recovers_absent_pane_only_before_agent_start(test_env, native_pane, start_requested):
    from herdr.state_store import SQLiteStateStore

    inbox_id = _start_workflow(test_env)
    db = test_env["db_file"]
    pane, cli = native_pane
    clone = test_env["tmp_path"] / "worker-clone"
    store = SQLiteStateStore(str(db))
    intent = task_resources.begin_launch_intent(
        store, workflow_id="wf_stop", node_id="node", task_id="unregistered",
    )["intent"]
    intent = task_resources.record_launch_resources(store, intent, {
        "planned_clone_path": str(clone), "run_id": "run",
    })
    script = """
import os, runpy, sys
from pathlib import Path
from herdr import task_resources

worker_path, clone, pane_id, terminal_id, crash_after_request, *args = sys.argv[1:]
main = runpy.run_path(worker_path)["main"]
worker = main.__globals__
clone = Path(clone)
def create_workspace(task):
    clone.mkdir()
    return clone
def allocate(parent, path, identity, task):
    identity["terminal_id"] = terminal_id
    return pane_id, "dynamic"
worker.update(create_context_task_workspace=create_workspace, allocate_task_pane=allocate,
              write_task_context=lambda *a, **k: ({}, {}), verify_request_preflight=lambda *a: {},
              start_agent=lambda *a: os._exit(18))
write_tag = task_resources.write_worker_launch_identity
def crash_before_request(path, identity, **kwargs):
    if crash_after_request == "False" and identity["phase"] == "agent_start_requested":
        os._exit(17)
    return write_tag(path, identity, **kwargs)
task_resources.write_worker_launch_identity = crash_before_request
sys.argv = [worker_path, *args]
main()
"""
    with task_resources.workflow_launch_lock(db, "wf_stop") as lock_fd:
        worker = subprocess.run([
            sys.executable, "-c", script, "services/herdr-worker.py", str(clone),
            pane["pane_id"], pane["terminal_id"], str(start_requested),
            "--source", str(test_env["tmp_path"]), "--execution-mode", "context",
            "--task-id", intent["task_id"], "--agent", "codex", "--task-type", "fix",
            "--parent-pane", pane["pane_id"], "--run-id", "run",
            "--launch-intent-id", intent["intent_id"], "--launch-intent-key", intent["key"],
            "--launch-state-db", str(db),
        ], pass_fds=(lock_fd,), capture_output=True, text=True, timeout=5)
    assert worker.returncode == (18 if start_requested else 17), worker.stderr
    current = task_resources._latest_intent(store, intent["key"])
    assert current["resources"]["pane_id"] == pane["pane_id"]
    assert current["resources"]["agent_start_requested"] is start_requested
    assert not store.list_events(workflow_id="wf_stop", event_type="shutdown_process")
    cli("pane", "close", pane["pane_id"])
    shutil.rmtree(clone)
    assert task_resources.inventory_launch_resources(current)["resource_status"] == "absent"

    if start_requested:
        with task_resources.workflow_launch_lock(db, "wf_stop"):
            assert task_resources.recover_launch_resources(store, current)["status"] == "recovery_required"
        with pytest.raises(RuntimeError, match="failed to stop"):
            test_env["store"].cancel_task(inbox_id)
        assert get_workflow("wf_stop", db_path=db)["status"] == "closing"
        assert test_env["disp"].is_workspace_busy("ws_stop")[0]
    else:
        assert test_env["store"].cancel_task(inbox_id)["status"] == "CANCELLED"
        assert task_resources._latest_intent(store, intent["key"])["phase"] == "resources_absent"
        assert not test_env["disp"].is_workspace_busy("ws_stop")[0]
        assert shutdown_workflow("wf_stop", db_path=db) == "ALREADY_TERMINAL"
    assert not store.list_events(workflow_id="wf_stop", event_type="shutdown_process")
