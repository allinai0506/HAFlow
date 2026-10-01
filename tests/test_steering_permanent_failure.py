"""Permanent steering capability rejection must not look like delivery."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock
import pytest
from herdr import steering
from herdr.state_store import get_state_store
from tests.test_steering_mesh import steering_env, _seed_task
from tests.test_impl_fix6_regression import load_script

ROOT = Path(__file__).resolve().parent.parent


def seed_unsupported(env, agent="opencode"):
    task = _seed_task(env, "unsupported", status="working")
    store = get_state_store(db_path=env["tasks_file"].parent / "state.db")
    task["agent"] = agent
    store.save_task(task)
    return store


@pytest.mark.parametrize("agent", ["opencode", "qodercli", "not-registered"])
def test_new_unsupported_instruction_is_retained_as_blocked(steering_env, agent):
    store = seed_unsupported(steering_env, agent)
    before = store.get_task("unsupported")
    result = steering.queue_steer("unsupported", "keep this directive", urgent=False)
    assert result["ok"] is False
    assert result["status"] == "blocked"
    item = store.list_steers(task_id="unsupported")[0]
    assert item["status"] == "blocked"
    assert item["instruction"] == "keep this directive"
    assert item["last_delivery_error"]
    for _ in range(3):
        assert steering.dispatch_pending_steer("unsupported") is None
    assert store.get_task("unsupported")["status"] == before["status"]


def seed_legacy_pending(store):
    store.save_steer({"steer_id": "legacy", "task_id": "unsupported",
                      "instruction": "legacy retained", "operator": "human",
                      "urgent": False, "status": "pending", "created_at": 1})


def test_legacy_unsupported_item_attempts_once_and_remains_recoverable(steering_env, monkeypatch):
    store = seed_unsupported(steering_env)
    seed_legacy_pending(store)
    result = steering.dispatch_pending_steer("unsupported")
    assert result["status"] == "blocked"
    assert result["ok"] is False
    for _ in range(3):
        assert steering.dispatch_pending_steer("unsupported") is None
    item = store.list_steers(task_id="unsupported")[0]
    assert item["delivery_attempt_count"] == 1
    assert len(store.list_steering_history()) == 1
    # Only an explicit urgent call changes delivery mode; replace transport.
    adapter = steering.get_agent_adapter("opencode")
    urgent = Mock(return_value={"ok": True, "injected": True, "interrupted": True})
    monkeypatch.setattr(adapter, "steer_urgent", urgent)
    monkeypatch.setattr(steering, "get_agent_adapter", lambda _: adapter)
    recovered = steering.dispatch_steer_now("unsupported", "legacy")
    assert recovered["ok"] is True
    assert store.list_steers(task_id="unsupported")[0]["status"] == "dispatched"
    urgent.assert_called_once()


def test_native_cli_reports_permanent_refusal_as_nonzero(steering_env):
    store = seed_unsupported(steering_env)
    env = dict(os.environ, HERDR_STATE_DB=str(store.db_path), TASKS_FILE=str(steering_env["tasks_file"]),
               STEERING_FILE=str(steering_env["steering_file"]), WORKFLOWS_FILE=str(steering_env["workflows_file"]))
    result = subprocess.run([sys.executable, str(ROOT / "bin/herdr-task"), "steer", "unsupported", "native directive"],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 1
    assert "STEER_FAILED" in result.stdout
    assert "STEER_QUEUED" not in result.stdout
    assert store.list_steers(task_id="unsupported")[0]["status"] == "blocked"


def test_sentinel_does_not_claim_failed_injection(steering_env, monkeypatch, capsys):
    store = seed_unsupported(steering_env)
    task = store.get_task("unsupported")
    task["status"] = "blocked"
    store.save_task(task)
    seed_legacy_pending(store)
    sentinel = load_script("sentinel_permanent_steer_test", "services/herdr-sentinel.py")
    monkeypatch.setattr(sentinel, "_get_store", lambda: store)
    monkeypatch.setattr(sentinel, "STATE_FILE", steering_env["tasks_file"].parent / "sentinel.json")
    monkeypatch.setattr(sentinel, "pane_visible", lambda _: "")
    monkeypatch.setattr(sentinel, "agent_status", lambda _: "idle")
    monkeypatch.setattr(sentinel, "check_dispatch_fuse", lambda *args: False)
    monkeypatch.setattr(sentinel, "check_task_stalls", lambda *args: None)
    class StopLoop(BaseException):
        pass
    sleeps = []
    def stop_after_two(_seconds):
        sleeps.append(1)
        if len(sleeps) == 2:
            raise StopLoop
    monkeypatch.setattr(sentinel.time, "sleep", stop_after_two)
    with pytest.raises(StopLoop):
        sentinel.main()
    output = capsys.readouterr().out
    assert "Injected pending steer" not in output
    assert store.list_steers(task_id="unsupported")[0]["status"] == "blocked"
    assert len(store.list_steering_history()) == 1


@pytest.mark.parametrize("pane,reason", [(None, "no_pane_id"), ("pane-101", "inject_prompt_failed")])
def test_transient_failures_remain_retryable(steering_env, monkeypatch, pane, reason):
    task = _seed_task(steering_env, "transient", pane_id=pane)
    adapter = steering.get_agent_adapter("codex")
    soft = Mock(return_value={"ok": False, "reason": reason})
    monkeypatch.setattr(adapter, "steer_soft", soft)
    monkeypatch.setattr(steering, "get_agent_adapter", lambda _: adapter)
    result = steering.queue_steer("transient", "retry me")
    assert result["ok"] is True
    for _ in range(2):
        delivered = steering.dispatch_pending_steer("transient")
        assert delivered["status"] == "pending"
        assert delivered["reason"] == reason
    assert soft.call_count == (2 if pane else 0)


def test_native_supported_cli_queues_without_physical_delivery(steering_env):
    _seed_task(steering_env, "supported")
    db = steering_env["tasks_file"].parent / "state.db"
    env = dict(os.environ, HERDR_STATE_DB=str(db), TASKS_FILE=str(steering_env["tasks_file"]),
               STEERING_FILE=str(steering_env["steering_file"]), WORKFLOWS_FILE=str(steering_env["workflows_file"]))
    result = subprocess.run([sys.executable, str(ROOT / "bin/herdr-task"), "steer", "supported", "queued directive"],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 0
    assert "STEER_QUEUED" in result.stdout
    item = get_state_store(db_path=db).list_steers(task_id="supported")[0]
    assert item["status"] == "pending"
    assert item.get("dispatched_at") is None


@pytest.mark.parametrize("result,urgent,expected_error", [
    ({"ok": False, "status": "blocked", "reason": "soft_steer_not_supported"}, False, True),
    ({"ok": False, "status": "blocked", "reason": "unknown_agent_no_adapter_registered"}, False, True),
    ({"ok": False, "status": "pending", "reason": "inject_prompt_failed"}, True, True),
    ({"ok": True, "status": "queued"}, False, False),
    ({"ok": True, "status": "dispatched"}, True, False),
])
def test_console_executes_real_steer_feedback(result, urgent, expected_error):
    from console.herdr_factory_console import HTML
    function = next(line for line in HTML.splitlines() if line.startswith("async function submitSteer("))
    script = r"""
const vm=require('node:vm');const assert=require('node:assert/strict');
const input=JSON.parse(process.argv[1]);const messages=[];const requests=[];
const ctx={document:{getElementById:(id)=>id==='steerInput'?{value:'keep directive'}:{checked:input.urgent}},
closeModal:()=>{},toast:(message,error)=>messages.push({message,error:!!error}),
state:{workflowId:'wf'},loadWorkflow:async()=>{},
api:async(path,args)=>{requests.push(JSON.parse(args.body));return input.result}};
vm.runInNewContext(input.function,ctx);
(async()=>{await ctx.submitSteer('task');assert.equal(requests[0].urgent,input.urgent);
assert.equal(messages.at(-1).error,input.expected_error);
if(input.expected_error)assert.ok(!messages.at(-1).message.includes('已放进留言队列'));
console.log(JSON.stringify(messages));})().catch(e=>{console.error(e);process.exit(1)});
"""
    completed = subprocess.run(["node", "-e", script, json.dumps({"function": function, "result": result,
        "urgent": urgent, "expected_error": expected_error})], text=True, capture_output=True)
    assert completed.returncode == 0, completed.stderr
