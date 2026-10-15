"""PR #211 R19: version-guarded success; quarantined exit."""

import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _schema(conn):
    conn.execute("""CREATE TABLE local_collector_records (task_id TEXT NOT NULL, run_id TEXT NOT NULL,
      outcome_id TEXT, experience_id TEXT, status TEXT NOT NULL, candidate_json TEXT, outcome_json TEXT,
      last_error TEXT, retry_count INTEGER NOT NULL DEFAULT 0, next_retry_at REAL NOT NULL DEFAULT 0,
      created_at REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY (task_id, run_id))""")


def _exp(eid, content="rule content long enough xxxxx"):
    return {"experience_id": eid, "type": "business_rule", "title": "t", "content": content,
            "contributor": "u", "applies_to": {"customer": "acme", "task_type": "quote"},
            "source": {"task_id": "t", "run_id": "r"}}


def test_success_guarded_by_experience_id(tmp_path):
    from herdr.experience.collector import ExperienceCollector, _get_collector_db

    cdb = tmp_path / "c.db"
    conn = sqlite3.connect(str(cdb))
    _schema(conn)
    now = time.time()
    conn.execute("INSERT INTO local_collector_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("t", "r", None, "exp-A", "pending_upload", json.dumps(_exp("exp-A")),
                  None, "x", 0, 0, now, now))
    conn.commit()
    conn.close()

    class SwapHub:
        def __init__(self, cdb):
            self.cdb = cdb
        def submit_candidate(self, exp):
            c = _get_collector_db(self.cdb)
            with c:
                c.execute("UPDATE local_collector_records SET experience_id=?, status='pending_upload', "
                          "candidate_json=?, updated_at=? WHERE task_id='t'",
                          ("exp-B", json.dumps(_exp("exp-B", "different rule content yyyy")), time.time()))
            c.close()
            return {"ok": True, "experience_id": "exp-A"}

    col = ExperienceCollector(db_path=cdb, hub_client=SwapHub(cdb), contributor="u")
    res = col.retry_pending_uploads(force=True)
    assert res["succeeded"] == 0
    conn = _get_collector_db(cdb)
    try:
        row = conn.execute("SELECT status, experience_id FROM local_collector_records WHERE task_id='t'").fetchone()
    finally:
        conn.close()
    assert (row["status"], row["experience_id"]) == ("pending_upload", "exp-B")


def test_quarantined_collection_fails_sync(tmp_path):
    import os
    cdb = tmp_path / "c.db"
    conn = sqlite3.connect(str(cdb))
    _schema(conn)
    now = time.time()
    conn.execute("INSERT INTO local_collector_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("ghost", "r", None, None, "pending_collection", None,
                  '{"run_id":"r"}', None, 0, 0, now, now))
    conn.commit()
    conn.close()
    env = dict(os.environ, HERDR_COLLECTOR_DB_PATH=str(cdb), HERDR_STATE_DB=str(tmp_path / "s.db"))
    first = subprocess.run([sys.executable, "bin/herdr-task", "sync-experience", "--force", "--json"],
                           capture_output=True, text=True, env=env, timeout=120)
    assert first.returncode != 0
    second = subprocess.run([sys.executable, "bin/herdr-task", "sync-experience", "--force", "--json"],
                            capture_output=True, text=True, env=env, timeout=120)
    assert second.returncode != 0
    assert json.loads(second.stdout)["pending_collection_error"] >= 1
