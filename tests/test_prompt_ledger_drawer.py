"""Task drawer reads the latest prompt snapshot without taking the full text."""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from herdr.kernel import state_db_path
from herdr.prompt_ledger_view import project_task_prompt
from herdr.state_store import get_state_store
from herdr.supervisor_delivery import deliver


def _task(task_id="t-drawer", workflow_id="wf-drawer", **extra):
    task = {
        "task_id": task_id,
        "workflow_id": workflow_id,
        "node": "implementation",
        "run_id": "run-drawer",
        "pane_id": "pane-drawer",
        "status": "working",
        "agent": "codex",
        "agent_kind": "codex",
        "goal": "show the sent instruction",
        "candidate_sha": "b" * 40,
        "completion_protocol": "receipt-v1",
    }
    task.update(extra)
    return task


def _send(store, task, prompt, intervention):
    store.save_task(task)
    return deliver(
        task, store, "RETRY",
        {"intervention_id": intervention, "action": "RETRY"},
        prompt, lambda pane, text: None,
    )


def _blob(card):
    return json.dumps(card, ensure_ascii=False)


def test_real_delivery_projects_a_redacted_card_for_that_task_only(tmp_path):
    store = get_state_store(tmp_path / "state.db")
    secret = "xoxb-" + ("B" * 16)
    tail = "TAILSECRET-drawer"
    prompt = secret + " " + ("n" * 400) + " " + tail
    _send(store, _task(), prompt, "round-1")
    _send(store, _task(task_id="other-task", run_id="run-other"), "UNIQUE_OTHER_TASK_MARKER", "other-1")

    card = project_task_prompt(store, "t-drawer", workflow_id="wf-drawer")
    blob = _blob(card)
    latest = card["latest"]
    assert card["available"] is True
    assert card["rounds"] == 1
    assert card["rounds_capped"] is False
    assert latest["dispatch_status"] == "DISPATCHED"
    assert latest["status_label"] == "传输命令已接受"
    assert latest["model_receipt"] == "UNOBSERVABLE"
    assert latest["prompt_sha256"]
    assert "[redacted]" in latest["prompt_excerpt"]
    assert secret not in blob
    assert tail not in blob
    assert "UNIQUE_OTHER_TASK_MARKER" not in blob
    assert "prompt_content" not in blob
    assert "delivery_evidence" not in blob
    assert "composition" not in blob

    foreign = project_task_prompt(store, "t-drawer", workflow_id="wf-other")
    assert foreign["rounds"] == 0
    assert foreign["latest"] is None
    assert foreign["compare"] is None


def test_two_rounds_compare_without_returning_the_diff_text(tmp_path):
    store = get_state_store(tmp_path / "state.db")
    _send(store, _task(), "round-one-body", "round-1")
    _send(store, _task(), "round-two-body", "round-2")
    card = project_task_prompt(store, "t-drawer", workflow_id="wf-drawer")
    assert card["rounds"] == 2
    assert card["compare"]["equal"] is False
    assert card["compare"]["access"] == "reveal_required"
    assert "diff" not in card["compare"]
    assert "round-one-body" not in card["latest"]["prompt_excerpt"]
    assert card["latest"]["prompt_excerpt"].startswith("round-two-body")
    assert card["previous_prompt_id"] == card["compare"]["left_id"]
    assert card["latest"]["prompt_id"] == card["compare"]["right_id"]


def test_card_read_does_not_open_a_writable_connection(tmp_path, monkeypatch):
    store = get_state_store(tmp_path / "state.db")
    _send(store, _task(), "visible instruction", "only")

    def refuse(*_args, **_kwargs):
        raise AssertionError("drawer read opened a writable state path")

    monkeypatch.setattr("herdr.state_db.init_db", refuse)
    monkeypatch.setattr("herdr.state_db.get_db_connection", refuse)
    monkeypatch.setattr("herdr.state_store.get_state_store", refuse)
    monkeypatch.setattr("herdr.prompt_ledger._connect", refuse)

    card = project_task_prompt(store, "t-drawer", workflow_id="wf-drawer")
    assert card["latest"]["dispatch_status"] == "DISPATCHED"
    assert card["latest"]["prompt_excerpt"].startswith("visible instruction")


def test_missing_snapshot_is_an_empty_card(tmp_path):
    store = get_state_store(tmp_path / "state.db")
    card = project_task_prompt(store, "nobody", workflow_id="wf-drawer")
    assert card == {
        "task_id": "nobody",
        "workflow_id": "wf-drawer",
        "available": True,
        "rounds": 0,
        "rounds_capped": False,
        "latest": None,
        "previous_prompt_id": None,
        "compare": None,
    }


def test_projection_api_reads_the_same_store_and_survives_a_ledger_failure(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks.json"
    workflows = tmp_path / "workflows.json"
    tasks.write_text(json.dumps({"tasks": []}), encoding="utf-8")
    workflows.write_text(json.dumps({"workflows": {}}), encoding="utf-8")
    monkeypatch.setenv("TASKS_FILE", str(tasks))
    monkeypatch.setenv("WORKFLOWS_FILE", str(workflows))
    monkeypatch.delenv("HERDR_STATE_DB", raising=False)
    assert state_db_path() == tmp_path / "state.db"

    from console import herdr_factory_console as console

    store = get_state_store(state_db_path())
    task = _task()
    store.save_task(task)
    _send(store, task, "visible instruction", "only")

    monkeypatch.setattr("herdr.projection._read_pane_content", lambda pane_id: "")

    def refuse(*_args, **_kwargs):
        raise AssertionError("projection opened a writable state path")

    monkeypatch.setattr("herdr.state_db.init_db", refuse)
    monkeypatch.setattr("herdr.state_db.get_db_connection", refuse)
    monkeypatch.setattr("herdr.state_store.get_state_store", refuse)
    projected = console.api_task_projection(task["task_id"])
    assert projected["prompt_ledger"]["latest"]["prompt_excerpt"].startswith("visible instruction")
    assert "prompt_content" not in _blob(projected["prompt_ledger"])
    assert projected["task_id"] == task["task_id"]

    def explode(*args, **kwargs):
        raise RuntimeError("ledger down")

    monkeypatch.setattr("console.read_api.project_task_prompt", explode)
    failed = console.api_task_projection(task["task_id"])
    assert failed["task_id"] == task["task_id"]
    assert failed["prompt_ledger"]["available"] is False
    assert failed["prompt_ledger"]["latest"] is None
    assert "artifacts" in failed


def _extract_fn(html, name):
    start = html.find(f"function {name}(")
    opener = html.find("{", start)
    depth = 0
    for index in range(opener, len(html)):
        if html[index] == "{":
            depth += 1
        elif html[index] == "}":
            depth -= 1
            if depth == 0:
                return html[start:index + 1]
    raise AssertionError(name)


def test_overview_renders_the_card_and_leaves_old_payloads_alone():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not found")
    from console.page import HTML_TEMPLATE

    overview = _extract_fn(HTML_TEMPLATE, "renderTaskOverview")
    ledger = _extract_fn(HTML_TEMPLATE, "renderPromptLedger")
    assert "renderPromptLedger" in overview
    assert "发出的指令" in ledger
    harness = """
function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function humanReason(s){return s==null?'':String(s);}
function badge(s){return s||'';}
""" + ledger + "\n" + overview + """
const filled = renderTaskOverview({}, {prompt_ledger:{
  available:true, rounds:2, rounds_capped:false,
  latest:{dispatch_status:'DISPATCHED', status_label:'传输命令已接受', agent_name:'worker-a',
    prompt_sha256:'0123456789abcdef', prompt_excerpt:'<script>alert(1)</script>', model_receipt:'UNOBSERVABLE',
    content_purged:false, prompt_id:'ps_new'},
  previous_prompt_id:'ps_old',
  compare:{left_id:'ps_old', right_id:'ps_new', equal:false, access:'reveal_required'}
}});
const empty = renderTaskOverview({}, {prompt_ledger:{available:true, rounds:0, latest:null, compare:null}});
const broken = renderTaskOverview({}, {prompt_ledger:{available:false, error:'指令记录暂时无法读取', latest:null}});
const absent = renderTaskOverview({goal:'only goal'}, {});
console.log(JSON.stringify({filled, empty, broken, absent}));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write(harness)
        path = handle.name
    try:
        result = subprocess.run([node, path], capture_output=True, text=True, timeout=15, check=False)
    finally:
        Path(path).unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    assert "传输命令已接受" in rendered["filled"]
    assert "worker-a" in rendered["filled"]
    assert "0123456789ab" in rendered["filled"]
    assert "&lt;script&gt;" in rendered["filled"]
    assert "<script>" not in rendered["filled"]
    assert "与上一轮不同" in rendered["filled"]
    assert "prompt-ledger diff --left ps_old --right ps_new --reveal" in rendered["filled"]
    assert "不表示模型已经收到或理解" in rendered["filled"]
    assert "这次任务还没有投递记录" in rendered["empty"]
    assert "指令记录暂时无法读取" in rendered["broken"]
    assert "发出的指令" not in rendered["absent"]
    assert "only goal" in rendered["absent"]
