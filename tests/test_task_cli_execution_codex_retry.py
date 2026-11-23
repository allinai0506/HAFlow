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


def _codex_agent_payload(
    *,
    agent="codex",
    name="owned-agent",
    terminal_id="term-1",
    session="sess-owned",
    status="idle",
    revision=1,
):
    return {
        "result": {
            "agent": {
                "agent": agent,
                "name": name,
                "terminal_id": terminal_id,
                "agent_session": {"value": session},
                "agent_status": status,
                "revision": revision,
            }
        }
    }


def test_codex_initial_prompt_retries_once_when_swallowed(monkeypatch):
    task = _make_task("codex")
    calls = []
    ownership_checks = []
    probes = iter([
        _codex_agent_payload(status="idle", revision=1),
        _codex_agent_payload(status="idle", revision=1),
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

    def fake_current_delivery_task(t, store, native=True):
        ownership_checks.append(native)
        return task

    monkeypatch.setenv("HERDR_CODEX_PROMPT_VERIFY_DELAY", "0.01")
    with patch.object(task_cli_execution.subprocess, "run", side_effect=fake_run), \
         patch("herdr.supervisor_delivery.deliver", side_effect=fake_deliver), \
         patch("herdr.supervisor_delivery.current_delivery_task", side_effect=fake_current_delivery_task):
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
    assert ownership_checks == [True, True]


def test_codex_initial_prompt_does_not_retry_when_working(monkeypatch):
    task = _make_task("codex")
    calls = []
    probes = iter([
        _codex_agent_payload(status="idle", revision=1),
        _codex_agent_payload(status="working", revision=2),
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
        _codex_agent_payload(status="idle", revision=1),
        _codex_agent_payload(status="done", revision=2),
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


def test_codex_replaced_instance_or_lower_revision_does_not_retry(monkeypatch):
    """Codex P1: foreign replacement instance (changed session/name or reset revision) must never receive retry."""
    task = _make_task("codex")
    calls = []
    probes = iter([
        _codex_agent_payload(name="owned-agent", session="sess-owned", status="idle", revision=8),
        _codex_agent_payload(name="foreign-agent", session="sess-foreign", status="idle", revision=1),
    ])
    ownership_checks = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["herdr", "agent", "get"]:
            return _ok_proc(next(probes))
        if cmd[:3] == ["herdr", "agent", "prompt"]:
            return _ok_proc({"result": {"type": "agent_prompted"}})
        raise AssertionError(f"unexpected cmd: {cmd}")

    def fake_deliver(t, store, action, meta, prompt, send, prepare_task=None):
        return send(t["pane_id"], prompt)

    def fake_current_delivery_task(t, store, native=True):
        ownership_checks.append(native)
        return task

    monkeypatch.setenv("HERDR_CODEX_PROMPT_VERIFY_DELAY", "0.01")
    with patch.object(task_cli_execution.subprocess, "run", side_effect=fake_run), \
         patch("herdr.supervisor_delivery.deliver", side_effect=fake_deliver), \
         patch("herdr.supervisor_delivery.current_delivery_task", side_effect=fake_current_delivery_task):
        task_cli_execution.handle_dispatch_task(
            task["task_id"],
            "run tests",
            prefetched_experience_suffix="",
            get_store=lambda: _dummy_store(task),
            set_status=lambda *a, **kw: None,
            compile_context_ref=lambda t: "",
        )

    assert len([c for c in calls if c[:3] == ["herdr", "agent", "prompt"]]) == 1
    assert ownership_checks == [True]


def test_codex_same_revision_changed_session_does_not_retry(monkeypatch):
    """Codex P1: equal revision with changed session identity must not trigger retry."""
    task = _make_task("codex")
    calls = []
    probes = iter([
        _codex_agent_payload(session="sess-owned", status="idle", revision=1),
        _codex_agent_payload(session="sess-other", status="idle", revision=1),
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


def test_codex_retry_revalidates_native_ownership_and_aborts_if_lost(monkeypatch):
    """Codex P1: second send must re-call current_delivery_task(..., native=True) and skip if ownership is lost."""
    task = _make_task("codex")
    calls = []
    same_agent = _codex_agent_payload(status="idle", revision=1)
    probes = iter([same_agent, same_agent])
    ownership_calls = 0

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["herdr", "agent", "get"]:
            return _ok_proc(next(probes))
        if cmd[:3] == ["herdr", "agent", "prompt"]:
            return _ok_proc({"result": {"type": "agent_prompted"}})
        raise AssertionError(f"unexpected cmd: {cmd}")

    def fake_deliver(t, store, action, meta, prompt, send, prepare_task=None):
        return send(t["pane_id"], prompt)

    def fake_current_delivery_task(t, store, native=True):
        nonlocal ownership_calls
        ownership_calls += 1
        if ownership_calls == 1:
            return task
        raise RuntimeError("pane ownership lost")

    monkeypatch.setenv("HERDR_CODEX_PROMPT_VERIFY_DELAY", "0.01")
    with patch.object(task_cli_execution.subprocess, "run", side_effect=fake_run), \
         patch("herdr.supervisor_delivery.deliver", side_effect=fake_deliver), \
         patch("herdr.supervisor_delivery.current_delivery_task", side_effect=fake_current_delivery_task):
        task_cli_execution.handle_dispatch_task(
            task["task_id"],
            "run tests",
            prefetched_experience_suffix="",
            get_store=lambda: _dummy_store(task),
            set_status=lambda *a, **kw: None,
            compile_context_ref=lambda t: "",
        )

    assert ownership_calls == 2
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

    assert [c[:3] for c in calls] == [["herdr", "agent", "prompt"]]


def test_codex_probe_failure_or_non_int_revision_does_not_crash(monkeypatch):
    task = _make_task("codex")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["herdr", "agent", "get"]:
            return _ok_proc({"result": {"agent": {"agent": "codex", "agent_status": "idle", "revision": "1"}}})
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
