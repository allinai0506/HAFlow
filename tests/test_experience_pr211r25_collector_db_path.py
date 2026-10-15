"""R25: queue source identity survives process/cwd/env changes."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from herdr import state_db
from herdr.experience.collector import ExperienceCollector


def seed(db, run="A", rule="报价金额超过10000必须由区域经理审批"):
    state_db.init_db(db)
    state_db.save_task(
        {
            "task_id": "shared",
            "run_id": run,
            "status": "completed",
            "customer": "acme",
            "task_type": "quote",
        },
        db_path=db,
    )
    state_db.save_steer(
        {
            "steer_id": "s",
            "task_id": "shared",
            "operator": "human",
            "instruction": rule,
            "status": "dispatched",
        },
        db_path=db,
    )


def outcome(run="A"):
    return {
        "outcome_id": "o-" + run,
        "run_id": run,
        "qualified_success": True,
        "verification_passed": True,
        "requirements_satisfied": True,
    }


def rows(db):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return {
            r["run_id"]: dict(r)
            for r in conn.execute("SELECT * FROM local_collector_records")
        }


class Hub:
    def __init__(self):
        self.calls = 0
        self.entities = {}
        self.fail = False

    def submit_candidate(self, exp):
        self.calls += 1
        if self.fail:
            raise ConnectionError("controlled offline")
        self.entities[exp.experience_id] = exp.to_dict()
        return {"ok": True}


def recover(queue, cwd):
    code = """
import json, sys
from pathlib import Path
from herdr.experience.collector import ExperienceCollector
class Hub:
    def submit_candidate(self, exp):
        print('CANDIDATE:'+json.dumps(exp.to_dict()))
        return {'ok': True}
print('STATS:'+json.dumps(ExperienceCollector(Path(sys.argv[1]), Hub()).process_pending()))
"""
    env = dict(
        os.environ,
        PYTHONPATH=str(Path(__file__).resolve().parents[1]),
        HERDR_STATE_DB="decoy.db",
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(queue)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize("default", [False, True])
def test_relative_source_survives_independent_recovery(tmp_path, monkeypatch, default):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    seed(a / "source.db")
    seed(b / "source.db", "DECOY", "诱饵报价不允许被提炼，必须拒绝")
    seed(b / "decoy.db", "DECOY")
    monkeypatch.chdir(a)
    monkeypatch.setenv("HERDR_STATE_DB", "source.db")
    monkeypatch.setenv("CHECKPOINTS_DIR", str(b / "checkpoints"))
    q = tmp_path / "queue.db"
    col = ExperienceCollector(q, Hub())
    assert col.enqueue_task_settlement(
        "shared", outcome(), state_db_path=None if default else Path("source.db")
    )["ok"]
    assert rows(q)["A"]["state_db_path"] == str(a / "source.db")
    output = recover(q, b)
    assert "DECOY" not in output
    assert "task_not_found" not in output
    assert (
        json.loads(output.split("CANDIDATE:")[1].splitlines()[0])["source"]["run_id"]
        == "A"
    )
    assert rows(q)["A"]["status"] == "uploaded"


def test_tilde_reads_and_persists_same_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    seed(home / "source.db")
    q = tmp_path / "queue.db"
    col = ExperienceCollector(q, Hub())
    assert col.enqueue_task_settlement("shared", outcome(), Path("~/source.db"))["ok"]
    assert rows(q)["A"]["state_db_path"] == str(home / "source.db")
    assert col.process_pending()["processed"] == 1
    assert rows(q)["A"]["status"] == "uploaded"


def test_mixed_databases_keep_rule_and_other_row_isolated(tmp_path):
    a, b, q = tmp_path / "a.db", tmp_path / "b.db", tmp_path / "q.db"
    seed(a, "A", "甲库规则：报价超过10000必须审批")
    seed(b, "B", "乙库规则：折扣超过百分之十必须审批")
    hub = Hub()
    col = ExperienceCollector(q, hub)
    for db, run in [(a, "A"), (b, "B")]:
        assert col.enqueue_task_settlement("shared", outcome(run), db)["ok"]
    before = rows(q)["B"]
    assert col.process_pending(state_db_path=b, limit=1)["processed"] == 1
    assert rows(q)["B"] == before
    after_a = rows(q)["A"]
    assert col.process_pending(state_db_path=a)["processed"] == 1
    assert rows(q)["A"] == after_a
    candidates = {e["source"]["run_id"]: e["content"] for e in hub.entities.values()}
    assert "甲库" in candidates["A"] and "乙库" not in candidates["A"]
    assert "乙库" in candidates["B"] and "甲库" not in candidates["B"]


@pytest.mark.parametrize("stored", [None, ""])
def test_legacy_empty_source_uses_caller(tmp_path, stored):
    db, q = tmp_path / "source.db", tmp_path / "q.db"
    seed(db)
    col = ExperienceCollector(q, Hub())
    col.enqueue_task_settlement("shared", outcome(), db)
    with sqlite3.connect(q) as conn:
        conn.execute("UPDATE local_collector_records SET state_db_path=?", (stored,))
    assert col.process_pending(state_db_path=db)["processed"] == 1
    assert rows(q)["A"]["status"] == "uploaded"


def test_missing_source_retries_then_recovers(tmp_path):
    db, q = tmp_path / "source.db", tmp_path / "q.db"
    seed(db)
    col = ExperienceCollector(q, Hub())
    col.enqueue_task_settlement("shared", outcome(), db)
    saved = tmp_path / "saved.db"
    db.rename(saved)
    assert col.process_pending()["failed"] == 1
    row = rows(q)["A"]
    assert row["status"] == "pending_collection"
    assert row["retry_count"] == 1 and row["next_retry_at"] > 0
    assert "state_db_missing" in row["last_error"]
    assert not db.exists()
    saved.rename(db)
    with sqlite3.connect(q) as conn:
        conn.execute("UPDATE local_collector_records SET next_retry_at=0")
    assert col.process_pending()["processed"] == 1
    assert rows(q)["A"]["status"] == "uploaded"
