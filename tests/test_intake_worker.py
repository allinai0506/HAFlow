import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from tests.test_intake_store import clock, delivery, store


def test_worker_service_consumes_and_stops_without_dispatch(store, clock):
    clock[0] = time.time_ns() // 1_000_000
    receipt = store.accept_event(delivery())
    command = [sys.executable, "services/herdr-intake.py", "--tenant", "tenant",
               "--connection", "one"]
    with subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1],
                          env={**os.environ, "HERDR_STATE_DB": str(store.path)},
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and process.poll() is None:
                if store.get_event(receipt.event_id)["status"] == "PROCESSED":
                    break
                time.sleep(0.02)
            assert store.get_event(receipt.event_id)["status"] == "PROCESSED"
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
            _, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr.decode()
    assert store.list_attempts(receipt.event_id)[0]["outcome"] == "PROCESSED"


def test_worker_records_transient_lock_and_permanent_processing_failure(store, monkeypatch, clock):
    import json
    import sqlite3

    from herdr.intake import worker

    accepted = store.accept_event(delivery())

    def locked(_):
        exc = sqlite3.OperationalError("injected lock")
        exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise exc

    monkeypatch.setattr(worker, "_process", locked)
    assert worker.run_once(store)
    event = store.get_event(accepted.event_id)
    assert event["status"] == "RETRY_WAIT"
    assert event["last_error"] == "SQLITE_CONTENTION"

    def corrupted(_):
        raise json.JSONDecodeError("injected damaged JSON", "", 0)

    monkeypatch.setattr(worker, "_process", corrupted)
    clock[0] = event["next_retry_at"]
    assert worker.run_once(store)
    attempts = store.list_attempts(accepted.event_id)
    assert [attempt["outcome"] for attempt in attempts] == ["RETRY_WAIT", "DEAD_LETTER"]
    assert attempts[-1]["error"] == "CANONICAL_DATA_CORRUPT"
