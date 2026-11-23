"""Regression tests for console inbox RUNNING+CLAIMED filter and intake stale workspace sync."""

from pathlib import Path
from unittest.mock import patch
import json
import shutil
import subprocess

import pytest

from console.inbox_api import api_inbox_tasks
from herdr.inbox.store import InboxStore


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_list_tasks_supports_comma_separated_running_and_claimed(tmp_path):
    """RUNNING,CLAIMED filter returns both CLAIMED and RUNNING tasks while preserving single-status filtering."""
    db_path = tmp_path / "inbox.db"
    store = InboxStore(db_path=db_path)

    t_claimed = store.create_task(
        title="Claimed task",
        instruction="Claimed instruction",
        project_id="nexusarchive-54433229",
        workspace_id="w1R",
        user_id="operator",
        auto_confirm=True,
    )
    t_running = store.create_task(
        title="Running task",
        instruction="Running instruction",
        project_id="nexusarchive-54433229",
        workspace_id="w1R",
        user_id="operator",
        auto_confirm=True,
    )
    t_done = store.create_task(
        title="Completed task",
        instruction="Done instruction",
        project_id="nexusarchive-54433229",
        workspace_id="w1R",
        user_id="operator",
        auto_confirm=True,
    )

    store.update_task_status(t_claimed["id"], "CLAIMED", workflow_id="wf_claimed")
    store.update_task_status(t_running["id"], "CLAIMED", workflow_id="wf_running")
    store.update_task_status(t_running["id"], "RUNNING", workflow_id="wf_running")
    store.update_task_status(t_done["id"], "CLAIMED", workflow_id="wf_done")
    store.update_task_status(t_done["id"], "RUNNING", workflow_id="wf_done")
    store.update_task_status(t_done["id"], "COMPLETED", workflow_id="wf_done")

    combined, combined_total = store.list_tasks(user_id="operator", status="RUNNING,CLAIMED")
    assert combined_total == 2
    assert {t["id"] for t in combined} == {t_claimed["id"], t_running["id"]}

    only_running, running_total = store.list_tasks(user_id="operator", status="RUNNING")
    assert running_total == 1
    assert [t["id"] for t in only_running] == [t_running["id"]]

    only_claimed, claimed_total = store.list_tasks(user_id="operator", status="CLAIMED")
    assert claimed_total == 1
    assert [t["id"] for t in only_claimed] == [t_claimed["id"]]


def test_api_inbox_tasks_running_claimed_filter(tmp_path, monkeypatch):
    """GET /api/inbox/tasks?status=RUNNING,CLAIMED returns both CLAIMED and RUNNING tasks."""
    db_path = tmp_path / "inbox_api.db"
    store = InboxStore(db_path=db_path)

    t_claimed = store.create_task(
        title="Claimed via API",
        instruction="Do work",
        project_id="nexusarchive-54433229",
        workspace_id="w1R",
        user_id="operator",
        auto_confirm=True,
    )
    store.update_task_status(t_claimed["id"], "CLAIMED", workflow_id="wf_ibx_test")

    monkeypatch.setenv("HAFLOW_USER", "operator")
    with patch("console.inbox_api.get_inbox_store", return_value=store):
        res = api_inbox_tasks({"status": ["RUNNING,CLAIMED"]})
        assert res["ok"] is True
        assert res["total"] == 1
        assert [t["id"] for t in res["tasks"]] == [t_claimed["id"]]


def test_inbox_js_running_filter_requests_running_and_claimed():
    """inbox.js maps filterStatus='RUNNING' to status=RUNNING%2CCLAIMED so CLAIMED tasks appear under 执行中."""
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node not installed")

    inbox_js = (REPO_ROOT / "console/static/console/scripts/inbox.js").read_text(encoding="utf-8")
    script = f"""
const urls = [];
const api = async (url) => {{
  urls.push(url);
  if (url.startsWith('/api/inbox/tasks')) return {{ ok: true, tasks: [], total: 0 }};
  return {{ ok: true, counts: {{}}, total: 0, busy_workspaces: {{}} }};
}};
const fetchIntakeActiveItems = async () => {{}};
const renderIntakeCards = () => '';
const renderIntakePagination = () => '';
const renderIntakeEmpty = () => '';
const getIntakeAuthButtonHtml = () => '';
const bindIntakeEvents = () => {{}};
const getIntakeItemsCount = () => 0;
const document = {{ getElementById: () => null }};
const state = {{ overview: {{ projects: [] }} }};
const esc = (s) => String(s ?? '');
const badge = () => '';
const formatElapsed = () => '';
const paintCrumb = () => {{}};
const setWorkspaceMode = () => {{}};
const openModal = () => {{}};
const closeModal = () => {{}};
const toast = () => {{}};
const showConfirmModal = () => {{}};

{inbox_js}

(async () => {{
  setInboxStatusFilter('RUNNING');
  await new Promise(r => setImmediate(r));
  console.log(JSON.stringify(urls));
}})();
"""
    proc = subprocess.run([node_bin, "-e", script], capture_output=True, text=True, check=True)
    urls = json.loads(proc.stdout.strip())
    task_urls = [u for u in urls if u.startswith("/api/inbox/tasks")]
    assert len(task_urls) == 1
    assert "status=RUNNING%2CCLAIMED" in task_urls[0]


def test_intake_js_syncs_stale_workspace_on_modal_open_and_guards_confirm():
    """intake.js updates readonly workspace input to project's current workspace_id on open and blocks un-saved confirm."""
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node not installed")

    intake_js = (REPO_ROOT / "console/static/console/scripts/intake.js").read_text(encoding="utf-8")
    script = f"""
const calls = [];
const elements = {{}};

class MockFormData {{
  constructor(form) {{ this._entries = [['title', form._title], ['description', form._desc], ['project_id', form._project.value], ['priority', form._priority]]; }}
  [Symbol.iterator]() {{ return this._entries[Symbol.iterator](); }}
  get(k) {{ const f = this._entries.find(([key]) => key === k); return f ? f[1] : null; }}
}}
globalThis.FormData = MockFormData;

const makeBtn = (action, type = 'button') => ({{
  type,
  dataset: action ? {{ action }} : {{}},
  disabled: false,
  focused: false,
  listeners: {{}},
  focus() {{ this.focused = true; }},
  addEventListener(ev, fn) {{ this.listeners[ev] = fn; }},
}});

const openModal = (_title, html) => {{
  const projectEl = {{
    value: 'nexusarchive-54433229',
    selectedOptions: [{{ dataset: {{ workspace: 'w1R' }} }}],
    listeners: {{}},
    addEventListener(ev, fn) {{ this.listeners[ev] = fn; }},
  }};
  const wsMatch = html.match(/id="intakeWorkspace" readonly value="([^"]*)"/);
  const workspaceEl = {{ value: wsMatch ? wsMatch[1] : '' }};
  const statusEl = {{ textContent: '', tabIndex: 0, focus() {{}} }};
  const submitBtn = makeBtn(null, 'submit');
  const confirmBtn = makeBtn('confirm', 'button');
  const formEl = {{
    _title: 'CO-667',
    _desc: 'Fix KEYS *',
    _project: projectEl,
    _priority: 'NORMAL',
    dataset: {{ linkId: 'link-1' }},
    listeners: {{}},
    addEventListener(ev, fn) {{ this.listeners[ev] = fn; }},
    querySelector(sel) {{
      if (sel === '[role=status]') return statusEl;
      if (sel === 'button[type=submit]') return submitBtn;
      return submitBtn;
    }},
    querySelectorAll(sel) {{
      if (sel === 'button') return [submitBtn, confirmBtn];
      if (sel === '[data-action]') return [confirmBtn];
      return [];
    }},
  }};
  elements.intakeDetail = formEl;
  elements.intakeProject = projectEl;
  elements.intakeWorkspace = workspaceEl;
  elements._status = statusEl;
  elements._confirmBtn = confirmBtn;
}};

const document = {{ getElementById: (id) => elements[id] || null }};
const state = {{ overview: {{ projects: [{{ project_id: 'nexusarchive-54433229', project_name: 'nexusarchive', workspace_id: 'w1R' }}] }} }};
const api = async (url, opts) => {{
  calls.push({{ url, body: opts && opts.body ? JSON.parse(opts.body) : null }});
  return {{ ok: true, items: [{{
    link_id: 'link-1',
    provider: 'linear',
    external_id: 'ext-1',
    disposition: 'LINKED',
    status: 'DRAFT',
    connection_id: 'linear',
    event_id: 'ev-1',
    title: 'CO-667',
    description: 'Fix KEYS *',
    source_title: 'CO-667',
    source_description: 'Fix KEYS *',
    project_id: 'nexusarchive-54433229',
    workspace_id: 'w1N',
    priority: 'NORMAL',
    revision: 7,
  }}] }};
}};
const esc = (s) => String(s ?? '').replace(/"/g, '&quot;');
const closeModal = () => {{}};
const toast = () => {{}};
const sessionStorage = {{ getItem: () => 'tok', setItem: () => {{}}, removeItem: () => {{}} }};
const localStorage = sessionStorage;

{intake_js}

(async () => {{
  await fetchIntakeActiveItems([], 'ALL', true);
  showIntakeDetail('link-1', async () => {{}});
  const syncedWorkspace = elements.intakeWorkspace.value;

  // Clicking confirm before saving should be blocked because workspace changed w1N -> w1R
  await elements._confirmBtn.listeners.click();
  const confirmGuardMsg = elements._status.textContent;
  const callsAfterBlockedConfirm = calls.length;

  // Submitting form should send updated workspace_id 'w1R'
  elements.intakeDetail.listeners.submit({{ preventDefault() {{}} }});
  await new Promise(r => setImmediate(r));
  console.log(JSON.stringify({{
    syncedWorkspace,
    confirmGuardMsg,
    callsAfterBlockedConfirm,
    calls,
  }}));
}})();
"""
    proc = subprocess.run([node_bin, "-e", script], capture_output=True, text=True, check=True)
    result = json.loads(proc.stdout.strip())
    assert result["syncedWorkspace"] == "w1R"
    assert "请先保存修改" in result["confirmGuardMsg"]
    assert result["callsAfterBlockedConfirm"] == 1  # Only initial fetch, no confirm POST
    edit_calls = [c for c in result["calls"] if c.get("body") and c["body"].get("action") == "edit"]
    assert len(edit_calls) == 1, f"unexpected calls: {result['calls']}"
    assert edit_calls[0]["body"]["changes"]["workspace_id"] == "w1R"
