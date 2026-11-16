"""Integration tests for Console Inbox APIs."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from console.inbox_api import (
    api_inbox_cancel_task,
    api_inbox_confirm_task,
    api_inbox_create_task,
    api_inbox_detect,
    api_inbox_reorder_task,
    api_inbox_retry_task,
    api_inbox_set_priority,
    api_inbox_stats,
    api_inbox_sweep,
    api_inbox_task,
    api_inbox_tasks,
    get_inbox_store,
)
from herdr.inbox.store import InboxStore

MOCK_PROJECTS = {
    "version": 1,
    "projects": {
        "/Users/user/HAFlow": {
            "project_id": "haflow-1de29d2d",
            "project_name": "HAFlow",
            "project_root": "/Users/user/HAFlow",
            "workspace_id": "w1K",
        },
    },
}


@pytest.fixture(autouse=True)
def setup_inbox_test_db(tmp_path: Path):
    db_file = tmp_path / "test_api_inbox.db"
    store = InboxStore(db_file)
    with patch("console.inbox_api.get_inbox_store", return_value=store):
        yield store


def test_api_inbox_workflow():
    with patch("herdr.inbox.detector.load_projects", return_value=MOCK_PROJECTS):
        # 1. Detect scope
        detect_res = api_inbox_detect({
            "title": "Bug in HAFlow UI",
            "instruction": "Fix layout bug",
        })
        assert detect_res["ok"] is True
        assert detect_res["recommended_project_id"] == "haflow-1de29d2d"
        assert detect_res["recommended_workspace_id"] == "w1K"

        # 2. Create as draft
        draft_res = api_inbox_create_task({
            "title": "Draft task",
            "instruction": "Need review first",
            "priority": "LOW",
        })
        assert draft_res["ok"] is True
        assert draft_res["status"] == "DRAFT"
        task_id = draft_res["task_id"]

        # 3. Confirm scope
        confirm_res = api_inbox_confirm_task({
            "task_id": task_id,
            "project_id": "haflow-1de29d2d",
            "workspace_id": "w1K",
        })
        assert confirm_res["ok"] is True
        assert confirm_res["task"]["status"] == "PENDING"

        # 4. Set priority
        prio_res = api_inbox_set_priority({
            "task_id": task_id,
            "priority": "HIGH",
        })
        assert prio_res["ok"] is True
        assert prio_res["task"]["priority"] == "HIGH"

        # 5. Create another task directly to PENDING with auto_confirm by explicit user
        auto_res = api_inbox_create_task({
            "title": "Urgent Hotfix",
            "instruction": "Fix critical bug",
            "project_id": "haflow-1de29d2d",
            "workspace_id": "w1K",
            "priority": "URGENT",
            "auto_confirm": True,
            "confirmation_source": "user",
        })
        assert auto_res["status"] == "PENDING"

        # 6. List tasks
        list_res = api_inbox_tasks({"status": "PENDING", "workspace_id": "w1K"})
        assert list_res["ok"] is True
        assert list_res["total"] == 2
        # Urgent Hotfix should be #1, Draft task (now HIGH) should be #2
        assert list_res["tasks"][0]["title"] == "Urgent Hotfix"
        assert list_res["tasks"][0]["queue_position"] == 1
        assert list_res["tasks"][1]["title"] == "Draft task"
        assert list_res["tasks"][1]["queue_position"] == 2

        # 7. Cancel a task
        cancel_res = api_inbox_cancel_task({
            "task_id": auto_res["task_id"],
            "reason": "Test cancel",
        })
        assert cancel_res["ok"] is True
        assert cancel_res["task"]["status"] == "CANCELLED"

        # 8. Stats
        stats_res = api_inbox_stats()
        assert stats_res["ok"] is True
        assert stats_res["total"] == 2
        assert stats_res["counts"]["PENDING"] == 1
        assert stats_res["counts"]["CANCELLED"] == 1


def test_auto_confirm_source_validation_and_user_isolation(monkeypatch, setup_inbox_test_db):
    store = setup_inbox_test_db
    with patch("herdr.inbox.detector.load_projects", return_value=MOCK_PROJECTS):
        # 1. Automated confirm with low confidence must be forced to DRAFT
        low_res = api_inbox_create_task({
            "title": "Random pasta task",
            "instruction": "Cook pasta without any project references",
            "project_id": "haflow-1de29d2d",
            "workspace_id": "w1K",
            "auto_confirm": True,
            "confirmation_source": "auto",  # automated source!
        })
        assert low_res["ok"] is True
        assert low_res["status"] == "DRAFT"  # Forced to DRAFT!

        # 2. User identity scoping
        monkeypatch.setenv("HAFLOW_USER", "alice")
        alice_task = api_inbox_create_task({
            "title": "Alice task",
            "instruction": "For HAFlow project",
            "project_id": "haflow-1de29d2d",
            "workspace_id": "w1K",
            "auto_confirm": True,
            "confirmation_source": "user",
        })
        assert alice_task["task"]["user_id"] == "alice"

        monkeypatch.setenv("HAFLOW_USER", "bob")
        bob_task = api_inbox_create_task({
            "title": "Bob task",
            "instruction": "For HAFlow project",
            "project_id": "haflow-1de29d2d",
            "workspace_id": "w1K",
            "auto_confirm": True,
            "confirmation_source": "user",
        })
        assert bob_task["task"]["user_id"] == "bob"

        # Bob default listing only sees Bob's tasks
        bob_tasks = api_inbox_tasks({})
        bob_ids = [t["id"] for t in bob_tasks["tasks"]]
        assert bob_task["task_id"] in bob_ids
        assert alice_task["task_id"] not in bob_ids

        # P2-01: Non-admin caller passing user_id='all' or user_id='alice' CANNOT bypass isolation
        bob_all = api_inbox_tasks({"user_id": "all"})
        bob_all_ids = [t["id"] for t in bob_all["tasks"]]
        assert alice_task["task_id"] not in bob_all_ids

        bob_spoof = api_inbox_tasks({"user_id": "alice"})
        bob_spoof_ids = [t["id"] for t in bob_spoof["tasks"]]
        assert alice_task["task_id"] not in bob_spoof_ids

        # P2-01: Busy workspace notice masking across user boundaries
        store.update_task_status(alice_task["task_id"], "CLAIMED")
        store.update_task_status(alice_task["task_id"], "RUNNING", workflow_id="wf_secret_alice")

        bob_stats = api_inbox_stats()
        assert bob_stats["ok"] is True
        busy_w1 = bob_stats["busy_workspaces"].get("w1K")
        assert busy_w1 is not None
        assert busy_w1["status"] == "RUNNING"
        # Must be sanitized! No task_id or sensitive title leaked
        assert busy_w1["task_id"] is None
        assert busy_w1["title"] == "任务执行中 (其他用户)"


def test_high_confidence_auto_detection_cannot_bypass_user_confirmation(monkeypatch):
    """P1-01: Even if detection has 100% confidence, confirmation_source != 'user' MUST NOT set status to PENDING."""
    with patch("herdr.inbox.detector.load_projects", return_value=MOCK_PROJECTS):
        res = api_inbox_create_task({
            "title": "Fix HAFlow UI layout",
            "instruction": "Fix layout in /Users/user/HAFlow project",
            "project_id": "haflow-1de29d2d",
            "workspace_id": "w1K",
            "auto_confirm": True,
            "confirmation_source": "auto",  # automated detector
        })
        assert res["ok"] is True
        assert res["status"] == "DRAFT"  # Forced to DRAFT!
        assert res["task"]["status"] == "DRAFT"


def test_automated_sources_and_missing_confirmation_default_to_draft():
    """P1-03: Webhooks, timers, automated sources, or omitted confirmation sources cannot bypass user confirmation."""
    with patch("herdr.inbox.detector.load_projects", return_value=MOCK_PROJECTS):
        # Case 1: auto_confirm=True but NO confirmation_source or confirmed_by provided
        res1 = api_inbox_create_task({
            "title": "Unconfirmed caller",
            "instruction": "Some task",
            "project_id": "haflow-1de29d2d",
            "workspace_id": "w1K",
            "auto_confirm": True,
        })
        assert res1["status"] == "DRAFT"

        # Case 2: source_type='webhook' even if claiming confirmed_by='user'
        res2 = api_inbox_create_task({
            "title": "Webhook event",
            "instruction": "Deploy webhook",
            "project_id": "haflow-1de29d2d",
            "workspace_id": "w1K",
            "source_type": "webhook",
            "auto_confirm": True,
            "confirmed_by": "user",
        })
        assert res2["status"] == "DRAFT"

        # Case 3: source_type='timer'
        res3 = api_inbox_create_task({
            "title": "Cron timer",
            "instruction": "Hourly check",
            "project_id": "haflow-1de29d2d",
            "workspace_id": "w1K",
            "source_type": "timer",
            "auto_confirm": True,
            "confirmed_by": "user",
        })
        assert res3["status"] == "DRAFT"


def test_sweep_filters_and_sanitizes_for_regular_user(monkeypatch):
    """P2-01: Regular user calling /api/inbox/sweep cannot see other users' task instructions or titles."""
    mock_sweep_report = {
        "recovered": [
            {"id": "t_alice", "user_id": "alice", "title": "Alice Secret", "instruction": "Top secret instruction"},
            {"id": "t_bob", "user_id": "bob", "title": "Bob Public", "instruction": "Normal instruction"},
        ],
        "reconciled": [
            {"id": "t_alice2", "user_id": "alice", "title": "Alice Secret 2", "instruction": "Secret instruction 2"},
        ],
        "launched": [],
        "busy_workspaces": [
            {"workspace_id": "w1", "reason": "Task 't_alice' (Alice Secret) is RUNNING: Top secret instruction"},
        ],
    }

    class MockDispatcher:
        def dispatch_sweep(self):
            return mock_sweep_report

    with patch("console.inbox_api.get_inbox_dispatcher", return_value=MockDispatcher()):
        # 1. Bob (regular user) calls sweep
        monkeypatch.setenv("HAFLOW_USER", "bob")
        bob_sweep = api_inbox_sweep()
        assert bob_sweep["ok"] is True
        sweep = bob_sweep["sweep"]

        # Bob only sees his own tasks in recovered/reconciled/launched
        assert len(sweep["recovered"]) == 1
        assert sweep["recovered"][0]["id"] == "t_bob"
        assert len(sweep["reconciled"]) == 0

        # Busy workspaces must be sanitized
        assert len(sweep["busy_workspaces"]) == 1
        assert sweep["busy_workspaces"][0]["workspace_id"] == "w1"
        assert sweep["busy_workspaces"][0]["reason"] == "Workspace is currently busy"
        assert "Alice Secret" not in sweep["busy_workspaces"][0]["reason"]

        # 2. Admin user sees full report
        monkeypatch.setenv("HAFLOW_USER", "admin")
        admin_sweep = api_inbox_sweep()
        assert admin_sweep["ok"] is True
        assert len(admin_sweep["sweep"]["recovered"]) == 2
        assert len(admin_sweep["sweep"]["reconciled"]) == 1
        assert "Alice Secret" in admin_sweep["sweep"]["busy_workspaces"][0]["reason"]


def test_inbox_url_cold_start_bootstrap():
    """P2-03: Cold start with ?view=inbox correctly sets shellView to 'inbox' and workflowId to '__inbox__'."""
    import json
    import subprocess
    import shutil
    from pathlib import Path

    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node.js not available")

    bootstrap_path = Path("console/static/console/scripts/bootstrap.js").resolve()
    assert bootstrap_path.exists()
    bootstrap_code = bootstrap_path.read_text()

    script = f"""
    const state = {{
      shellView: 'workbench',
      opsMode: false,
      dashMode: false,
      workflowId: null,
      openWorkflowTabIds: []
    }};
    const window = {{
      location: {{
        search: '?view=inbox'
      }},
      addEventListener: () => {{}}
    }};
    const document = {{
      addEventListener: () => {{}},
      hidden: false
    }};
    function loadViewState() {{ return null; }}
    function saveViewState() {{}}
    function syncOpsUi() {{}}
    function paintCrumb() {{}}
    function setWorkspaceMode() {{}}
    function stopDashTimer() {{}}
    function stopInboxTimer() {{}}
    function renderWorkflowTabs() {{}}
    function loadInboxData() {{}}
    function showInbox() {{
      state.shellView = 'inbox';
      state.workflowId = '__inbox__';
    }}
    function showShellView(v) {{
      state.shellView = v;
    }}
    async function refreshAll() {{}}
    async function showDashboard() {{}}
    async function showOpsCenter() {{}}
    async function showTemplateLibrary() {{}}
    function toast() {{}}
    function api() {{ return Promise.resolve({{}}); }}

    {bootstrap_code}

    (async () => {{
      await initFromUrlOrState();
      console.log(JSON.stringify({{ shellView: state.shellView, workflowId: state.workflowId }}));
      process.exit(0);
    }})();
    """
    res = subprocess.run([node_bin, "-e", script], capture_output=True, text=True, timeout=5)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip())
    assert out["shellView"] == "inbox"
    assert out["workflowId"] == "__inbox__"


def test_inbox_js_user_selected_project_protection():
    """Verify that user manually selecting a project is protected from being overwritten by auto-detection."""
    import subprocess
    import shutil
    from pathlib import Path

    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node.js not available")

    inbox_js_path = Path("console/static/console/scripts/inbox.js").resolve()
    assert inbox_js_path.exists()
    inbox_code = inbox_js_path.read_text()

    script = f"""
    let currentDetectedProj = 'proj_detected_1';
    let currentConfidence = 0.95;

    const mockElements = {{
      'inboxInstruction': {{ value: 'Fix bug in path' }},
      'inboxTitle': {{ value: 'My Task' }},
      'inboxDetectBanner': {{ style: {{ display: 'none' }}, className: '', innerHTML: '' }},
      'inboxProject': {{
        value: '',
        selectedOptions: [{{ dataset: {{ ws: 'ws_user' }} }}]
      }},
      'inboxWorkspace': {{ value: '' }},
      'inboxAutoConfirm': {{ checked: false }}
    }};

    const document = {{
      getElementById: (id) => mockElements[id] || null,
      hidden: false
    }};
    const state = {{}};
    function esc(s) {{ return s || ''; }}
    function badge(t, s) {{ return ''; }}
    function formatElapsed(ts) {{ return ''; }}
    function showConfirmModal() {{}}
    function openModal() {{}}
    function api(url, opts) {{
      return Promise.resolve({{
        is_confident: true,
        confidence: currentConfidence,
        recommended_project_id: currentDetectedProj,
        recommended_workspace_id: 'ws_detected',
        reason: 'matched path'
      }});
    }}
    function toast() {{}}
    function closeModal() {{}}
    function setWorkspaceMode() {{}}
    function paintCrumb() {{}}
    function saveViewState() {{}}
    function syncOpsUi() {{}}

    // Evaluate inbox.js module factory
    {inbox_code}

    (async () => {{
      const sel = mockElements['inboxProject'];
      const wsInput = mockElements['inboxWorkspace'];

      // Scenario 1: User manually selects 'proj_manual' via dropdown
      sel.value = 'proj_manual';
      sel.selectedOptions[0] = {{ dataset: {{ ws: 'ws_manual' }} }};
      onInboxProjectSelectChange(true);
      const pass1_initial = (wsInput.value === 'ws_manual');

      // Instruction blur triggers auto-detection returning 'proj_detected_1'
      currentDetectedProj = 'proj_detected_1';
      await onInboxInstructionBlur();

      // Project must NOT be overwritten!
      const pass1 = pass1_initial && (sel.value === 'proj_manual');

      // Scenario 2: Reset / unselected by user
      sel.value = '';
      sel.selectedOptions[0] = {{ dataset: {{ ws: '' }} }};
      onInboxProjectSelectChange(true);

      currentDetectedProj = 'proj_detected_2';
      await onInboxInstructionBlur();

      // Programmatic detection fills 'proj_detected_2'
      const pass2 = (sel.value === 'proj_detected_2');

      // Now instruction changes again with 'proj_detected_3'; should update since user didn't manually pick
      currentDetectedProj = 'proj_detected_3';
      await onInboxInstructionBlur();
      const pass3 = (sel.value === 'proj_detected_3');

      // Scenario 3: User now manually selects 'proj_user_final'
      sel.value = 'proj_user_final';
      sel.selectedOptions[0] = {{ dataset: {{ ws: 'ws_user_final' }} }};
      onInboxProjectSelectChange(true);

      // Subsequent blur cannot override
      currentDetectedProj = 'proj_detected_4';
      await onInboxInstructionBlur();
      const pass4 = (sel.value === 'proj_user_final');

      console.log(JSON.stringify({{ pass1, pass2, pass3, pass4 }}));
      process.exit(0);
    }})();
    """
    res = subprocess.run([node_bin, "-e", script], capture_output=True, text=True, timeout=5)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip())
    assert out["pass1"] is True
    assert out["pass2"] is True
    assert out["pass3"] is True
    assert out["pass4"] is True
