"""Regression tests for Codex initial prompt swallow retry in receipt-v1 delivery."""
import json
import subprocess
from unittest.mock import patch

from herdr import task_cli_execution


def _make_task(agent="codex"):
    return {
        "task_id": "wf-codex-test-r1",
        "workflow_id": "wf-codex",
        "run_id": "run-codex-1",
        "node": "cleanroom_test",
        "stage": "cleanroom_test",
        "agent": agent,
        "pane_id": "w1:p9",
        "status": "pending",
        "version": 1,
        "completion_protocol": "receipt-v1",
    }


def _ok_proc(payload):
    return subprocess.CompletedProcess(
        args=["herdr"], returncode=0, stdout=json.dumps(payload), stderr=""
    )


def _dummy_store(task):
    return type(
        "DummyStore",
        (),
        {
            "get_task": lambda self, tid: task,
            "get_workflow": lambda self, wid: None,
            "list_events": lambda self, **kw: [],
            "record_event": lambda self, *a, **kw: None,
        },
    )()


def test_codex_initial_prompt_retries_once_when_swallowed(monkeypatch):
    task = _make_task("codex")
    calls = []
    probes = iter([
        {"result": {"agent": {"agent": "codex", "agent_status": "idle", "revision": 1}}},
        {"result": {"agent": {"agent": "codex", "agent_status": "idle", "revision": 1}}},
    ])

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["herdr", "agent", "get"]:
            return _ok_proc(next(probes))
        if cmd[:3] == ["herdr", "agent", "prompt"]:
            return _ok_proc({"result": {"type": "agent_prompted"}})
        raise AssertionError(f"unexpected cmd: {cmd}")

    def fake_deliver(t, store, action, meta, prompt, send, prepare_task=None):
        return send(t["pane_id"], prompt)

    monkeypatch.setenv("HERDR_CODEX_PROMPT_VERIFY_DELAY", "0.01")
    with patch.object(task_cli_execution.subprocess, "run", side_effect=fake_run), \
         patch("herdr.supervisor_delivery.deliver", side_effect=fake_deliver), \
         patch("herdr.supervisor_delivery.current_delivery_task", return_value=task):
        task_cli_execution.handle_dispatch_task(
            task["task_id"],
            "run tests",
            prefetched_experience_suffix="",
            get_store=lambda: _dummy_store(task),
            set_status=lambda *a, **kw: None,
            compile_context_ref=lambda t: "",
        )

    prompt_calls = [c for c in calls if c[:3] == ["herdr", "agent", "prompt"]]
    assert len(prompt_calls) == 2


def test_codex_initial_prompt_does_not_retry_when_working(monkeypatch):
    task = _make_task("codex")
    calls = []
    probes = iter([
        {"result": {"agent": {"agent": "codex", "agent_status": "idle", "revision": 1}}},
        {"result": {"agent": {"agent": "codex", "agent_status": "working", "revision": 2}}},
    ])

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["herdr", "agent", "get"]:
            return _ok_proc(next(probes))
        if cmd[:3] == ["herdr", "agent", "prompt"]:
            return _ok_proc({"result": {"type": "agent_prompted"}})
        raise AssertionError(f"unexpected cmd: {cmd}")

    def fake_deliver(t, store, action, meta, prompt, send, prepare_task=None):
        return send(t["pane_id"], prompt)

    monkeypatch.setenv("HERDR_CODEX_PROMPT_VERIFY_DELAY", "0.01")
    with patch.object(task_cli_execution.subprocess, "run", side_effect=fake_run), \
         patch("herdr.supervisor_delivery.deliver", side_effect=fake_deliver), \
         patch("herdr.supervisor_delivery.current_delivery_task", return_value=task):
        task_cli_execution.handle_dispatch_task(
            task["task_id"],
            "run tests",
            prefetched_experience_suffix="",
            get_store=lambda: _dummy_store(task),
            set_status=lambda *a, **kw: None,
            compile_context_ref=lambda t: "",
        )

    prompt_calls = [c for c in calls if c[:3] == ["herdr", "agent", "prompt"]]
    assert len(prompt_calls) == 1


def test_codex_fast_done_with_bumped_revision_does_not_retry(monkeypatch):
    task = _make_task("codex")
    calls = []
    probes = iter([
        {"result": {"agent": {"agent": "codex", "agent_status": "idle", "revision": 1}}},
        {"result": {"agent": {"agent": "codex", "agent_status": "done", "revision": 2}}},
    ])

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["herdr", "agent", "get"]:
            return _ok_proc(next(probes))
        if cmd[:3] == ["herdr", "agent", "prompt"]:
            return _ok_proc({"result": {"type": "agent_prompted"}})
        raise AssertionError(f"unexpected cmd: {cmd}")

    def fake_deliver(t, store, action, meta, prompt, send, prepare_task=None):
        return send(t["pane_id"], prompt)

    monkeypatch.setenv("HERDR_CODEX_PROMPT_VERIFY_DELAY", "0.01")
    with patch.object(task_cli_execution.subprocess, "run", side_effect=fake_run), \
         patch("herdr.supervisor_delivery.deliver", side_effect=fake_deliver), \
         patch("herdr.supervisor_delivery.current_delivery_task", return_value=task):
        task_cli_execution.handle_dispatch_task(
            task["task_id"],
            "run tests",
            prefetched_experience_suffix="",
            get_store=lambda: _dummy_store(task),
            set_status=lambda *a, **kw: None,
            compile_context_ref=lambda t: "",
        )

    assert len([c for c in calls if c[:3] == ["herdr", "agent", "prompt"]]) == 1


def test_non_codex_agent_skips_probe_and_retry(monkeypatch):
    task = _make_task("agy")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["herdr", "agent", "prompt"]:
            return _ok_proc({"result": {"type": "agent_prompted"}})
        raise AssertionError(f"unexpected cmd: {cmd}")

    def fake_deliver(t, store, action, meta, prompt, send, prepare_task=None):
        return send(t["pane_id"], prompt)

    monkeypatch.setenv("HERDR_CODEX_PROMPT_VERIFY_DELAY", "0.01")
    with patch.object(task_cli_execution.subprocess, "run", side_effect=fake_run), \
         patch("herdr.supervisor_delivery.deliver", side_effect=fake_deliver), \
         patch("herdr.supervisor_delivery.current_delivery_task", return_value=task):
        task_cli_execution.handle_dispatch_task(
            task["task_id"],
            "run tests",
            prefetched_experience_suffix="",
            get_store=lambda: _dummy_store(task),
            set_status=lambda *a, **kw: None,
            compile_context_ref=lambda t: "",
        )

    assert len([c for c in calls if c[:3] == ["herdr", "agent", "prompt"]]) == 1
    assert len([c for c in calls if c[:3] == ["herdr", "agent", "get"]]) == 0


def test_codex_probe_failure_or_non_int_revision_does_not_crash(monkeypatch):
    task = _make_task("codex")
    calls = []
    probes = iter([
        subprocess.CompletedProcess(args=["herdr"], returncode=1, stdout="", stderr="err"),
    ])

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["herdr", "agent", "get"]:
            return next(probes)
        if cmd[:3] == ["herdr", "agent", "prompt"]:
            return _ok_proc({"result": {"type": "agent_prompted"}})
        raise AssertionError(f"unexpected cmd: {cmd}")

    def fake_deliver(t, store, action, meta, prompt, send, prepare_task=None):
        return send(t["pane_id"], prompt)

    monkeypatch.setenv("HERDR_CODEX_PROMPT_VERIFY_DELAY", "0.01")
    with patch.object(task_cli_execution.subprocess, "run", side_effect=fake_run), \
         patch("herdr.supervisor_delivery.deliver", side_effect=fake_deliver), \
         patch("herdr.supervisor_delivery.current_delivery_task", return_value=task):
        task_cli_execution.handle_dispatch_task(
            task["task_id"],
            "run tests",
            prefetched_experience_suffix="",
            get_store=lambda: _dummy_store(task),
            set_status=lambda *a, **kw: None,
            compile_context_ref=lambda t: "",
        )

    assert len([c for c in calls if c[:3] == ["herdr", "agent", "prompt"]]) == 1
