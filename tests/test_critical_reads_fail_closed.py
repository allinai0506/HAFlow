import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from herdr import agent_router, projects
from herdr.state_store import StateStore


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    db_file = tmp_path / "state.db"
    tasks_file = tmp_path / "tasks.json"
    workflows_file = tmp_path / "workflows.json"

    # Seed stale JSON files that would cause incorrect decisions if read
    workflows_file.write_text(
        json.dumps({
            "version": 1,
            "workflows": {
                "wf-test-01": {
                    "workflow_id": "wf-test-01",
                    "project_id": "proj-alpha",
                    "status": "running",
                    "agent_override": "stale-claude",
                }
            }
        }, ensure_ascii=False),
        encoding="utf-8"
    )

    tasks_file.write_text(
        json.dumps({
            "tasks": [
                {
                    "task_id": "task-stale-01",
                    "workflow_id": "wf-test-01",
                    "project_id": "proj-alpha",
                    "status": "working",
                    "agent": "stale-agent",
                }
            ]
        }, ensure_ascii=False),
        encoding="utf-8"
    )

    monkeypatch.setenv("HERDR_STATE_DB", str(db_file))
    monkeypatch.setenv("TASKS_FILE", str(tasks_file))
    monkeypatch.setenv("WORKFLOWS_FILE", str(workflows_file))
    monkeypatch.setattr(agent_router, "TASKS_FILE", tasks_file)
    monkeypatch.setattr(agent_router, "WORKFLOWS_FILE", workflows_file)
    monkeypatch.setattr(projects, "WORKFLOWS_FILE", workflows_file)

    return tmp_path


def test_router_workflow_record_fails_closed_on_store_error(isolated_env):
    mock_store = MagicMock(spec=StateStore)
    mock_store.get_workflow.side_effect = sqlite3.OperationalError("database disk image is malformed")

    with patch.object(agent_router, "_get_store", return_value=mock_store):
        with pytest.raises(sqlite3.OperationalError):
            agent_router.workflow_record("wf-test-01")


def test_router_active_agent_loads_fails_closed_on_store_error(isolated_env):
    mock_store = MagicMock(spec=StateStore)
    mock_store.list_tasks.side_effect = sqlite3.OperationalError("database is locked")

    with patch.object(agent_router, "_get_store", return_value=mock_store):
        with pytest.raises(sqlite3.OperationalError):
            agent_router._active_agent_loads("proj-alpha")


def test_router_clean_reservations_fails_closed_on_store_error(isolated_env):
    mock_store = MagicMock(spec=StateStore)
    mock_store.list_tasks.side_effect = sqlite3.OperationalError("disk I/O error")

    with patch.object(agent_router, "_get_store", return_value=mock_store):
        with pytest.raises(sqlite3.OperationalError):
            agent_router._clean_reservations({"reservations": {}})


def test_projects_active_workflows_fails_closed_on_store_error(isolated_env):
    mock_store = MagicMock(spec=StateStore)
    mock_store.list_workflows.side_effect = sqlite3.OperationalError("no such table: workflows")

    with patch.object(projects, "_get_store", return_value=mock_store):
        with pytest.raises(sqlite3.OperationalError):
            projects.active_workflows_for_project("proj-alpha")


def test_projects_non_terminal_workflows_fails_closed_on_store_error(isolated_env):
    mock_store = MagicMock(spec=StateStore)
    mock_store.list_workflows.side_effect = sqlite3.OperationalError("database is locked")

    with patch.object(projects, "_get_store", return_value=mock_store):
        with pytest.raises(sqlite3.OperationalError):
            projects.non_terminal_workflow_ids()


def test_projects_project_for_workflow_fails_closed_on_store_error(isolated_env):
    mock_store = MagicMock(spec=StateStore)
    mock_store.get_workflow.side_effect = sqlite3.OperationalError("disk I/O error")

    with patch.object(projects, "_get_store", return_value=mock_store):
        with pytest.raises(sqlite3.OperationalError):
            projects.project_for_workflow("wf-test-01")


def test_projects_load_workflows_fails_closed_on_store_error(isolated_env):
    mock_store = MagicMock(spec=StateStore)
    mock_store.export_workflows_json.side_effect = sqlite3.OperationalError("corrupted database")

    with patch.object(projects, "_get_store", return_value=mock_store):
        with pytest.raises(sqlite3.OperationalError):
            projects.load_workflows()


def test_controller_workflow_entry_fails_closed_on_store_error(isolated_env, monkeypatch):
    import importlib
    controller = importlib.import_module("services.herdr-controller")

    mock_store = MagicMock(spec=StateStore)
    mock_store.get_workflow.side_effect = sqlite3.OperationalError("disk I/O error")

    with patch.object(controller, "_get_store", return_value=mock_store):
        with pytest.raises(sqlite3.OperationalError):
            controller._workflow_entry("wf-test-01")


def test_controller_active_registered_workflows_fails_closed_on_store_error(isolated_env, monkeypatch):
    import importlib
    controller = importlib.import_module("services.herdr-controller")

    mock_store = MagicMock(spec=StateStore)
    mock_store.list_workflows.side_effect = sqlite3.OperationalError("disk I/O error")

    with patch.object(controller, "_get_store", return_value=mock_store):
        with pytest.raises(sqlite3.OperationalError):
            controller.active_registered_workflows()


def test_stale_json_never_resurrects_deleted_or_missing_workflow(isolated_env):
    """Test A: Verify stale JSON never resurrects deleted or missing workflows into SQLite."""
    import importlib
    controller = importlib.import_module("services.herdr-controller")
    from herdr.state_store import get_state_store
    store = get_state_store()

    # The isolated_env fixture creates workflows.json with "wf-test-01".
    # During the initial DB init in this test, wf-test-01 was bootstrapped.
    # Now we explicitly delete it from the authoritative SQLite database.
    store.delete_workflow("wf-test-01")
    assert store.get_workflow("wf-test-01") is None

    # Verify stale workflows.json still contains wf-test-01 on disk
    wf_file = Path(isolated_env) / "workflows.json"
    disk_data = json.loads(wf_file.read_text(encoding="utf-8"))
    assert "wf-test-01" in disk_data.get("workflows", {})

    # Read through all critical control paths
    assert projects.project_for_workflow("wf-test-01") is None
    assert len(projects.active_workflows_for_project("proj-alpha")) == 0
    assert "wf-test-01" not in projects.non_terminal_workflow_ids()
    assert agent_router.workflow_record("wf-test-01") == {}
    assert controller._workflow_entry("wf-test-01") == {}
    assert "wf-test-01" not in controller.active_registered_workflows()

    # Crucial assertion: SQLite was NEVER resurrected from stale JSON
    assert store.get_workflow("wf-test-01") is None


def test_choose_agent_fails_closed_on_unknown_workflow(isolated_env):
    """Test B: Verify choose_agent raises RuntimeError when workflow does not exist in StateStore."""
    # 1. Unknown workflow_id must fail-closed with RuntimeError
    with pytest.raises(RuntimeError) as exc_info:
        agent_router.choose_agent("wf-non-existent-999", stage="implementation", task_type="dev")
    assert "Workflow not found in authoritative StateStore: wf-non-existent-999" in str(exc_info.value)

    # 2. FR-6.3 breaking change: standalone task without workflow_id and
    # auto selection must fail-closed (no silent 'opencode' default).
    with pytest.raises(RuntimeError) as exc_none:
        agent_router.choose_agent(None, stage="implementation", task_type="dev", requested="auto")
    assert "explicit" in str(exc_none.value).lower()

    res_req = agent_router.choose_agent(None, stage="implementation", task_type="dev", requested="claude")
    assert res_req == "claude"


def test_controller_active_registered_workflows_ignores_projects_json_ghosts(isolated_env, monkeypatch):
    """Test C: Verify controller.active_registered_workflows() 100% ignores projects.json ghost workflows."""
    import importlib
    controller = importlib.import_module("services.herdr-controller")

    # Inject ghost workflow into projects.json
    ghost_dir = isolated_env / ".herdr-controller"
    ghost_dir.mkdir(parents=True, exist_ok=True)
    ghost_proj_file = ghost_dir / "projects.json"
    ghost_proj_file.write_text(json.dumps({
        "projects": {
            "p1": {
                "project_id": "proj-ghost",
                "workflow_id": "ghost-wf-999"
            }
        }
    }), encoding="utf-8")
    monkeypatch.setenv("HOME", str(isolated_env))

    # Controller's active workflows must NOT contain ghost-wf-999
    active = controller.active_registered_workflows()
    assert "ghost-wf-999" not in active


def test_one_time_bootstrap_migration_then_strict_isolation(tmp_path, monkeypatch):
    """Test D: Verify legacy JSON is bootstrapped once on empty DB, but subsequent JSON edits are ignored."""
    from herdr.state_store import get_state_store
    new_db = tmp_path / "bootstrap_test" / "state.db"
    new_dir = new_db.parent
    new_dir.mkdir(parents=True, exist_ok=True)

    legacy_wf = new_dir / "workflows.json"
    legacy_tasks = new_dir / "tasks.json"

    wid = "wf-legacy-boot"
    tid = "task-legacy-boot"

    legacy_wf.write_text(json.dumps({
        "version": 1,
        "workflows": {
            wid: {
                "workflow_id": wid,
                "project_id": "proj-boot",
                "status": "running",
            }
        }
    }), encoding="utf-8")

    legacy_tasks.write_text(json.dumps({
        "tasks": [
            {
                "task_id": tid,
                "workflow_id": wid,
                "status": "pending",
            }
        ]
    }), encoding="utf-8")

    # Initialize store for the first time
    store = get_state_store(db_path=new_db)

    # Bootstrapped into SQLite
    assert store.get_workflow(wid) is not None
    assert store.get_workflow(wid)["status"] == "running"
    assert store.get_task(tid) is not None

    # Now add a new task and workflow to the JSON files after initialization
    legacy_tasks.write_text(json.dumps({
        "tasks": [
            {"task_id": "task-after-boot", "workflow_id": wid, "status": "pending"}
        ]
    }), encoding="utf-8")

    # The store was already initialized (v1_migration_done); subsequent reads MUST NOT import it
    assert store.get_task("task-after-boot") is None


def test_atomic_bootstrap_rollback_on_corrupt_legacy_json(tmp_path):
    """Test E: Verify corrupt legacy JSON prevents v1_migration_done, raises to block startup, rolls back data, and retrying after fix succeeds."""
    from herdr.state_store import get_state_store, reset_state_store

    test_db = tmp_path / "atomic_boot_test" / "state.db"
    test_dir = test_db.parent
    test_dir.mkdir(parents=True, exist_ok=True)

    legacy_wf = test_dir / "workflows.json"
    legacy_tasks = test_dir / "tasks.json"

    wid = "wf-atomic-01"
    tid = "task-atomic-01"

    legacy_wf.write_text(json.dumps({
        "version": 1,
        "workflows": {
            wid: {
                "workflow_id": wid,
                "project_id": "proj-atomic",
                "status": "running",
            }
        }
    }), encoding="utf-8")

    # Intentionally corrupt tasks.json
    legacy_tasks.write_text("{invalid json corrupt content...", encoding="utf-8")

    # First init attempt: MUST raise and fail closed (cannot start on corrupt data)
    reset_state_store()
    with pytest.raises(Exception):
        get_state_store(db_path=test_db)

    # Verify migration failed atomically:
    # 1. v1_migration_done was NOT marked
    conn = sqlite3.connect(str(test_db))
    cur = conn.execute("SELECT value FROM schema_meta WHERE key = 'v1_migration_done';")
    assert cur.fetchone() is None
    # 2. wf-atomic-01 was rolled back and is NOT present
    cur_wf = conn.execute("SELECT 1 FROM workflows WHERE workflow_id = ?;", (wid,))
    assert cur_wf.fetchone() is None
    conn.close()

    # Now repair tasks.json
    legacy_tasks.write_text(json.dumps({
        "tasks": [
            {
                "task_id": tid,
                "workflow_id": wid,
                "status": "pending",
            }
        ]
    }), encoding="utf-8")

    # Retry initialization: reset cached state store & trigger new connection
    reset_state_store()
    store2 = get_state_store(db_path=test_db)

    # Verify migration now succeeded completely:
    conn = sqlite3.connect(str(test_db))
    cur = conn.execute("SELECT value FROM schema_meta WHERE key = 'v1_migration_done';")
    row = cur.fetchone()
    assert row is not None and row[0] == "1"
    conn.close()

    wf = store2.get_workflow(wid)
    assert wf is not None
    assert wf["status"] == "running"
    task = store2.get_task(tid)
    assert task is not None
    assert task["status"] == "pending"


def test_checkpoint_read_and_fork_never_resurrects_sqlite(tmp_path, monkeypatch):
    """Test F: Verify Checkpoint reading, listing, or forking never resurrects deleted state into SQLite."""
    from herdr import kernel
    from herdr.state_store import get_state_store, reset_state_store

    db_file = tmp_path / "cp_test" / "state.db"
    cp_dir = tmp_path / "cp_test" / "checkpoints"
    db_file.parent.mkdir(parents=True, exist_ok=True)
    cp_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HERDR_STATE_DB", str(db_file))
    monkeypatch.setenv("CHECKPOINTS_DIR", str(cp_dir))

    wid = "wf-cp-01"
    cpid = "cp-snapshot-001"
    tid = "task-cp-001"

    # Write legacy checkpoint JSON on disk
    wf_cp_dir = cp_dir / wid
    wf_cp_dir.mkdir(parents=True, exist_ok=True)
    cp_file = wf_cp_dir / f"{cpid}.json"
    cp_file.write_text(json.dumps({
        "checkpoint_id": cpid,
        "workflow_id": wid,
        "tag": "golden_backup",
        "created_at": 1773480000,
        "workflow": {
            "workflow_id": wid,
            "title": "Legacy Checkpoint WF",
            "status": "running"
        },
        "tasks": [
            {
                "task_id": tid,
                "workflow_id": wid,
                "node": "dev",
                "status": "completed"
            }
        ]
    }), encoding="utf-8")

    # Initialize store
    reset_state_store()
    store = get_state_store(db_path=db_file)

    # Explicitly ensure SQLite does NOT have this checkpoint, workflow, or task
    store.delete_workflow(wid)
    assert store.get_workflow(wid) is None
    assert store.get_task(tid) is None
    with pytest.raises(FileNotFoundError):
        store.get_checkpoint(wid, cpid)

    # 1. kernel.list_checkpoints(wid) must return empty list and NOT resurrect into SQLite
    cps = kernel.list_checkpoints(wid)
    assert cps == []
    assert store.get_workflow(wid) is None
    assert store.get_task(tid) is None

    # 2. kernel.get_checkpoint(wid, cpid) must fail closed (raise FileNotFoundError) and NOT resurrect
    with pytest.raises(FileNotFoundError):
        kernel.get_checkpoint(wid, cpid)
    assert store.get_workflow(wid) is None
    assert store.get_task(tid) is None

    # 3. kernel.fork_workflow_from_checkpoint must fail closed (raise FileNotFoundError) and NOT resurrect
    with pytest.raises(FileNotFoundError):
        kernel.fork_workflow_from_checkpoint(cpid, "wf-forked-999")
    assert store.get_workflow(wid) is None
    assert store.get_workflow("wf-forked-999") is None
    assert store.get_task(tid) is None


def test_bootstrap_failure_blocks_startup_fail_closed(tmp_path):
    """Test G: Verify bootstrap failure propagates exception to block system startup and leaves no partial state."""
    from herdr.state_store import get_state_store, reset_state_store

    test_db = tmp_path / "boot_fail_closed" / "state.db"
    test_dir = test_db.parent
    test_dir.mkdir(parents=True, exist_ok=True)

    legacy_wf = test_dir / "workflows.json"
    legacy_tasks = test_dir / "tasks.json"

    wid = "wf-boot-fail-01"

    legacy_wf.write_text(json.dumps({
        "version": 1,
        "workflows": {
            wid: {
                "workflow_id": wid,
                "project_id": "proj-fail-closed",
                "status": "running",
            }
        }
    }), encoding="utf-8")

    # Corrupt tasks.json
    legacy_tasks.write_text("<<<malformed json content>>>", encoding="utf-8")

    # Attempting to start/open store MUST fail closed by raising an exception
    reset_state_store()
    with pytest.raises(Exception):
        get_state_store(db_path=test_db)

    # Ensure DB is completely clean of partial state
    conn = sqlite3.connect(str(test_db))
    cur = conn.execute("SELECT value FROM schema_meta WHERE key = 'v1_migration_done';")
    assert cur.fetchone() is None
    cur_wf = conn.execute("SELECT 1 FROM workflows WHERE workflow_id = ?;", (wid,))
    assert cur_wf.fetchone() is None
    conn.close()

    # Repair legacy_tasks
    legacy_tasks.write_text(json.dumps({
        "tasks": [{"task_id": "task-boot-fixed-01", "workflow_id": wid, "status": "pending"}]
    }), encoding="utf-8")

    # Restarting store now succeeds cleanly
    reset_state_store()
    store = get_state_store(db_path=test_db)
    assert store.get_workflow(wid) is not None
    assert store.get_task("task-boot-fixed-01") is not None


def test_existing_sqlite_upgrade_protects_against_stale_json_overwrite(tmp_path, monkeypatch):
    """Test H: Verify existing SQLite database without migration marker is protected from stale JSON overwrite on upgrade."""
    from herdr.state_store import get_state_store, reset_state_store

    test_dir = tmp_path / "upgrade_protect"
    test_dir.mkdir(parents=True, exist_ok=True)
    test_db = test_dir / "state.db"
    cp_dir = test_dir / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CHECKPOINTS_DIR", str(cp_dir))

    # 1. Create an existing SQLite database (simulate PR #21 database with runtime data, but no schema_meta)
    from herdr import state_db
    state_db._INITIALIZED_DBS.discard(str(test_db))
    conn = state_db.get_db_connection(test_db)
    wid = "wf-upgrade-01"
    tid = "task-upgrade-01"
    state_db.save_workflow({
        "workflow_id": wid,
        "project_id": "proj-upgrade",
        "title": "Upgrade Test WF",
        "status": "completed",
    }, conn=conn)
    state_db.save_task({
        "task_id": tid,
        "workflow_id": wid,
        "node": "dev",
        "status": "completed",
        "agent": "codex",
    }, conn=conn)

    # Authentically simulate PR #21 by completely dropping schema_meta table
    conn.execute("DROP TABLE schema_meta;")
    conn.commit()
    conn.close()
    state_db._INITIALIZED_DBS.discard(str(test_db))

    # 2. Plant stale legacy JSON files in test_dir that conflict with SQLite
    legacy_wf = test_dir / "workflows.json"
    legacy_tasks = test_dir / "tasks.json"
    legacy_st = test_dir / "steering.json"
    legacy_cp = cp_dir / wid / "cp-stale-01.json"
    legacy_cp.parent.mkdir(parents=True, exist_ok=True)

    legacy_wf.write_text(json.dumps({
        "version": 1,
        "workflows": {
            wid: {
                "workflow_id": wid,
                "project_id": "proj-upgrade",
                "title": "Stale WF Title",
                "status": "running"  # Stale state!
            },
            "wf-stale-extra": {
                "workflow_id": "wf-stale-extra",
                "status": "pending"
            }
        }
    }), encoding="utf-8")

    legacy_tasks.write_text(json.dumps({
        "tasks": [
            {
                "task_id": tid,
                "workflow_id": wid,
                "status": "working"  # Stale state!
            },
            {
                "task_id": "task-stale-extra",
                "workflow_id": wid,
                "status": "pending"
            }
        ]
    }), encoding="utf-8")

    legacy_st.write_text(json.dumps({
        "steering_queues": {
            tid: [{"steer_id": "steer-stale-01", "instruction": "stale instruction"}]
        }
    }), encoding="utf-8")

    legacy_cp.write_text(json.dumps({
        "checkpoint_id": "cp-stale-01",
        "workflow_id": wid,
        "tag": "stale_cp"
    }), encoding="utf-8")

    # 3. Initialize StateStore (upgrading to PR #23)
    reset_state_store()
    store = get_state_store(db_path=test_db)

    # 4. Assertions: SQLite authoritative state was 100% PRESERVED
    wf = store.get_workflow(wid)
    assert wf is not None
    assert wf["status"] == "completed"  # NOT overwritten to "running"
    assert wf["title"] == "Upgrade Test WF"

    task = store.get_task(tid)
    assert task is not None
    assert task["status"] == "completed"  # NOT overwritten to "working"

    # Extra entities from stale JSON were NOT imported
    assert store.get_workflow("wf-stale-extra") is None
    assert store.get_task("task-stale-extra") is None
    assert store.list_steers(tid) == []
    with pytest.raises(FileNotFoundError):
        store.get_checkpoint(wid, "cp-stale-01")

    # Migration marker was written
    conn2 = sqlite3.connect(str(test_db))
    cur = conn2.execute("SELECT value FROM schema_meta WHERE key = 'v1_migration_done';")
    row = cur.fetchone()
    assert row is not None
    assert row[0] == "1"
    conn2.close()
