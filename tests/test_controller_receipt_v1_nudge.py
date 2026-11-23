"""Regression tests for controller automatic missing-receipt nudge on receipt-v1 tasks."""

import sqlite3
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from herdr.completion_receipt import _connection as open_receipt_conn
from herdr.state_store import SQLiteStateStore
from services.controller import task_listener


@pytest.fixture(autouse=True)
def _reset_nudge_cache():
    task_listener._NUDGED_RECEIPT_EPOCHS.clear()
    yield
    task_listener._NUDGED_RECEIPT_EPOCHS.clear()


def _make_task(**overrides):
    base = {
        "task_id": "wf-1-craft-auto",
        "workflow_id": "wf-1",
        "run_id": "run_abc123",
        "node": "craft",
        "stage": "craft",
        "agent": "agy",
        "status": "working",
        "pane_id": "w1R:p5",
        "completion_protocol": "receipt-v1",
        "completion_epoch": "epoch-111",
        "completion_identity_path": "/tmp/receipt-id.json",
    }
    base.update(overrides)
    return base


def _make_ports(task, *, store=None, has_done_marker=True, run_result=None):
    mock_run = MagicMock(
        return_value=run_result
        or subprocess.CompletedProcess(
            ["herdr", "agent", "prompt", "w1R:p5", "prompt"], 0, "ok", ""
        )
    )
    ports = SimpleNamespace(
        lock=threading.Lock(),
        subprocess=SimpleNamespace(run=mock_run),
        get_task=MagicMock(return_value=task),
        _get_store=MagicMock(return_value=store),
        _completion_marker_snapshot=MagicMock(
            return_value=(has_done_marker, "HERDR_TASK_DONE" if has_done_marker else "working...")
        ),
        _record_completion_sample=MagicMock(),
        _set_observed_status=MagicMock(return_value=True),
        emit_done_if_allowed=MagicMock(),
        blocked_event_type=MagicMock(return_value=None),
        maybe_ack_on_working=MagicMock(),
        gate_verdict_ready=MagicMock(return_value=False),
        _gate_verdict_is_fresh=MagicMock(return_value=False),
        check_task_deliverables_ready=MagicMock(return_value=False),
    )
    return ports, mock_run


def test_nudges_once_when_receipt_v1_agent_outputs_marker_without_receipt(tmp_path):
    """When a receipt-v1 task prints HERDR_TASK_DONE and goes done/idle without a receipt,
    controller nudges the owned live pane once per epoch and records a durable audit event."""
    db_path = tmp_path / "state.db"
    store = SQLiteStateStore(db_path=db_path)
    open_receipt_conn(store).close()
    task = _make_task()
    ports, mock_run = _make_ports(task, store=store, has_done_marker=True)

    with patch("herdr.task_resources.owned_live_pane", return_value=(True, "identity_match")):
        task_listener.handle_event(ports, task["task_id"], "done")
        # Second event (e.g. idle right after done) must be deduplicated
        task_listener.handle_event(ports, task["task_id"], "idle")

    assert mock_run.call_count == 1
    cmd = mock_run.call_args[0][0]
    assert cmd[:4] == ["herdr", "agent", "prompt", "w1R:p5"]
    assert "herdr-task report-completion wf-1-craft-auto --identity-file /tmp/receipt-id.json" in cmd[4]
    # Must NEVER transition task status on marker alone
    ports._set_observed_status.assert_not_called()
    ports._record_completion_sample.assert_not_called()

    # Durable event persisted so restart will not re-nudge same epoch
    events = store.list_events(task_id=task["task_id"], event_type="receipt_v1_nudge_sent")
    assert len(events) == 1
    assert events[0]["payload"]["completion_epoch"] == "epoch-111"

    # Simulate controller restart (clear in-memory cache): still deduplicated via durable event
    task_listener._NUDGED_RECEIPT_EPOCHS.clear()
    with patch("herdr.task_resources.owned_live_pane", return_value=(True, "identity_match")):
        task_listener.handle_event(ports, task["task_id"], "idle")
    assert mock_run.call_count == 1


def test_does_not_nudge_when_no_done_marker_on_screen(tmp_path):
    """Idle without HERDR_TASK_DONE must NOT trigger a receipt nudge."""
    db_path = tmp_path / "state.db"
    store = SQLiteStateStore(db_path=db_path)
    open_receipt_conn(store).close()
    task = _make_task()
    ports, mock_run = _make_ports(task, store=store, has_done_marker=False)

    with patch("herdr.task_resources.owned_live_pane", return_value=(True, "identity_match")):
        task_listener.handle_event(ports, task["task_id"], "idle")

    mock_run.assert_not_called()


def test_does_not_nudge_when_receipt_already_submitted(tmp_path):
    """If completion_receipts already has a receipt for (task_id, epoch), do not nudge."""
    db_path = tmp_path / "state.db"
    store = SQLiteStateStore(db_path=db_path)
    open_receipt_conn(store).close()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO completion_receipts(receipt_id,task_id,run_id,epoch,payload,consumed) VALUES(?,?,?,?,?,0)",
        ("rcpt-1", "wf-1-craft-auto", "run_abc123", "epoch-111", "{}"),
    )
    conn.commit()
    conn.close()

    task = _make_task()
    ports, mock_run = _make_ports(task, store=store, has_done_marker=True)

    with patch("herdr.task_resources.owned_live_pane", return_value=(True, "identity_match")):
        task_listener.handle_event(ports, task["task_id"], "done")

    mock_run.assert_not_called()


def test_does_not_nudge_unowned_or_replaced_pane(tmp_path):
    """Never send prompt if owned_live_pane fails."""
    db_path = tmp_path / "state.db"
    store = SQLiteStateStore(db_path=db_path)
    open_receipt_conn(store).close()
    task = _make_task()
    ports, mock_run = _make_ports(task, store=store, has_done_marker=True)

    with patch("herdr.task_resources.owned_live_pane", return_value=(False, "identity_mismatch")):
        task_listener.handle_event(ports, task["task_id"], "done")

    mock_run.assert_not_called()


def test_failed_prompt_delivery_does_not_persist_dedup_and_allows_retry(tmp_path):
    """If herdr agent prompt fails, do not record durable nudge event and allow retry on next settled event."""
    db_path = tmp_path / "state.db"
    store = SQLiteStateStore(db_path=db_path)
    open_receipt_conn(store).close()
    task = _make_task()
    ports, mock_run = _make_ports(
        task,
        store=store,
        has_done_marker=True,
        run_result=subprocess.CompletedProcess([], 1, "", "pane busy"),
    )

    with patch("herdr.task_resources.owned_live_pane", return_value=(True, "identity_match")):
        task_listener.handle_event(ports, task["task_id"], "done")
        assert mock_run.call_count == 1
        assert store.list_events(task_id=task["task_id"], event_type="receipt_v1_nudge_sent") == []

        mock_run.return_value = subprocess.CompletedProcess([], 0, "ok", "")
        task_listener.handle_event(ports, task["task_id"], "idle")
        assert mock_run.call_count == 2
        assert len(store.list_events(task_id=task["task_id"], event_type="receipt_v1_nudge_sent")) == 1


def test_production_controller_binding_delivers_missing_receipt_nudge(tmp_path):
    """Real herdr-controller.py binding (via bindings.install_modules) must expose subprocess/lock to handle_event."""
    import importlib.machinery
    import importlib.util
    from pathlib import Path

    controller_path = Path(__file__).resolve().parent.parent / "services" / "herdr-controller.py"
    spec = importlib.util.spec_from_loader(
        "ctrl_receipt_nudge_binding_test",
        importlib.machinery.SourceFileLoader("ctrl_receipt_nudge_binding_test", str(controller_path)),
    )
    ctrl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ctrl)

    db_path = tmp_path / "prod_binding.db"
    store = SQLiteStateStore(db_path=db_path)
    open_receipt_conn(store).close()
    task = _make_task(task_id="wf-prod-craft-auto", completion_epoch="epoch-prod-1")

    with patch.object(ctrl, "get_task", return_value=task), \
         patch.object(ctrl, "_get_store", return_value=store), \
         patch.object(ctrl, "_completion_marker_snapshot", return_value=(True, "HERDR_TASK_DONE")), \
         patch.object(
             ctrl.subprocess,
             "run",
             return_value=subprocess.CompletedProcess(["herdr", "agent", "prompt"], 0, "ok", ""),
         ) as mock_run, \
         patch("herdr.task_resources.owned_live_pane", return_value=(True, "identity_match")):
        ctrl.handle_event(task["task_id"], "done")
        ctrl.handle_event(task["task_id"], "idle")

    assert mock_run.call_count == 1
    cmd = mock_run.call_args[0][0]
    assert cmd[:4] == ["herdr", "agent", "prompt", "w1R:p5"]
    assert "herdr-task report-completion wf-prod-craft-auto --identity-file /tmp/receipt-id.json" in cmd[4]
    events = store.list_events(task_id=task["task_id"], event_type="receipt_v1_nudge_sent")
    assert len(events) == 1
    assert events[0]["payload"]["completion_epoch"] == "epoch-prod-1"
