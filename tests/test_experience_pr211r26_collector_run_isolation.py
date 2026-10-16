"""R26: failures never account against another Run or unknown identity."""

import sqlite3

import pytest

from herdr import state_db
from herdr.experience import collector as module
from herdr.experience.collector import ExperienceCollector
from tests.test_experience_pr211r25_collector_db_path import Hub, outcome, rows, seed


@pytest.fixture
def queued(tmp_path):
    a, b, q = tmp_path / "a.db", tmp_path / "b.db", tmp_path / "q.db"
    seed(a, "A")
    seed(b, "B")
    hub = Hub()
    col = ExperienceCollector(q, hub)
    col.enqueue_task_settlement("shared", outcome("A"), a)
    col.enqueue_task_settlement("shared", outcome("B"), b)
    return col, hub, a, b, q


def extraction_fault(monkeypatch):
    real = module.extract_candidate_from_facts

    def extract(**kwargs):
        if kwargs["task"]["run_id"] == "A":
            raise RuntimeError("controlled extraction fault")
        return real(**kwargs)

    monkeypatch.setattr(module, "extract_candidate_from_facts", extract)


def test_ordinary_failure_updates_only_queued_run_then_b_uploads(queued, monkeypatch):
    col, hub, _a, _b, q = queued
    before = rows(q)
    extraction_fault(monkeypatch)
    stats = col.process_pending(limit=1)
    assert stats["failed"] == 1
    after = rows(q)
    assert after["B"] == before["B"]  # independent SQLite connections; every column
    for key in ["retry_count", "next_retry_at", "last_error", "updated_at"]:
        assert after["A"][key] != before["A"][key]
    assert col.process_pending()["processed"] == 1
    assert rows(q)["B"]["status"] == "uploaded"
    assert len(hub.entities) == 1
    assert next(iter(hub.entities.values()))["source"]["run_id"] == "B"


def test_direct_failure_uses_derived_run(queued, monkeypatch):
    col, _hub, a, _b, q = queued
    before = rows(q)
    extraction_fault(monkeypatch)
    assert not col.process_task_settlement("shared", state_db_path=a)["ok"]
    after = rows(q)
    assert after["B"] == before["B"]
    assert after["A"]["retry_count"] == 1


@pytest.mark.parametrize("bound", [None, "A"])
def test_failure_before_task_read_is_unknown_or_queue_scoped(
    queued, monkeypatch, caplog, bound
):
    col, _hub, a, _b, q = queued
    before = rows(q)

    def broken(*args, **kwargs):
        raise RuntimeError("controlled read fault")

    monkeypatch.setattr(state_db, "get_task", broken)
    assert not col.process_task_settlement(
        "shared", state_db_path=a, queued_run_id=bound
    )["ok"]
    after = rows(q)
    assert after["B"] == before["B"]
    if bound:
        assert after["A"]["retry_count"] == 1
    else:
        assert after == before
        assert "unknown" in caplog.text and "shared" in caplog.text
        assert "collection failure note without run identity" in caplog.text


def test_missing_task_without_queue_identity_never_quarantines_other_runs(
    queued, tmp_path, caplog
):
    col, _hub, _a, _b, q = queued
    missing = tmp_path / "empty.db"
    state_db.init_db(missing)
    before = rows(q)
    result = col.process_task_settlement("shared", state_db_path=missing)
    assert not result["ok"] and result["reason"] == "task_not_found"
    assert rows(q) == before
    assert "without run identity" in caplog.text


@pytest.mark.parametrize("terminal", ["uploaded", "no_candidate"])
def test_terminal_other_run_all_columns_unchanged(queued, monkeypatch, terminal):
    col, _hub, a, _b, q = queued
    with sqlite3.connect(q) as conn:
        conn.execute(
            "UPDATE local_collector_records SET status=? WHERE run_id='B'", (terminal,)
        )
    before = rows(q)["B"]
    extraction_fault(monkeypatch)
    assert not col.process_task_settlement(
        "shared", state_db_path=a, queued_run_id="A"
    )["ok"]
    assert rows(q)["B"] == before


def test_task_run_conflict_refused_without_cross_run_settlement(queued):
    """Queued run and the task's current run disagree: refuse, never settle.

    The queued identity no longer merely scopes exception accounting while
    extraction still runs against the task's current run (Codex review P1:
    that allowed a cross-run upload and stranded the queued row). The mismatch
    now fails closed before any extraction or write, recorded terminally
    against the queued row only.
    """
    col, _hub, a, _b, q = queued
    before = rows(q)
    result = col.process_task_settlement("shared", state_db_path=a, queued_run_id="B")
    assert not result["ok"]
    assert result["reason"] == "run_identity_mismatch"
    after = rows(q)
    assert after["A"] == before["A"]
    assert after["B"]["status"] == "error"
    assert "run_identity_mismatch" in after["B"]["last_error"]


def test_task_run_reuse_refuses_cross_run_upload(tmp_path):
    """Task id reused for a new run while the old run's row is pending.

    Reproduces the Codex review scenario without fault injection: enqueue run
    A, overwrite the source task with run B, then recover. The queued row must
    not be uploaded under B, no phantom B row may appear, and run A's row must
    not linger in pending_collection.
    """
    a, q = tmp_path / "a.db", tmp_path / "q.db"
    seed(a, "A")
    hub = Hub()
    col = ExperienceCollector(q, hub)
    assert col.enqueue_task_settlement("shared", outcome("A"), a)["ok"]

    seed(a, "B")  # the task id is reused for a new run before recovery

    stats = col.process_pending()
    assert stats["processed"] == 0
    assert stats["failed"] == 1
    assert hub.entities == {}
    stored = rows(q)
    assert set(stored) == {"A"}
    assert stored["A"]["status"] == "error"
    assert "run_identity_mismatch" in stored["A"]["last_error"]


def test_upload_backoff_and_reentry_keep_one_entity(queued):
    col, hub, a, _b, q = queued
    hub.fail = True
    assert (
        col.process_task_settlement("shared", outcome(), a)["status"]
        == "pending_upload"
    )
    before = rows(q)["A"]
    for _ in range(3):
        assert (
            col.process_task_settlement("shared", outcome(), a)["reason"] == "backoff"
        )
    assert rows(q)["A"] == before
    assert hub.calls == 1
    hub.fail = False
    col.retry_pending_uploads(force=True)
    assert hub.calls == 2
    assert len(hub.entities) == 1
    for _ in range(2):
        assert (
            col.process_task_settlement("shared", outcome(), a)["status"] == "uploaded"
        )
    assert hub.calls == 2 and len(hub.entities) == 1
