"""PR #211 R11: no rewind on concurrent success; loud failing sync."""

import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


def test_failed_upload_does_not_rewind_success(tmp_path):
    from herdr import state_db
    from herdr.experience.collector import ExperienceCollector, _get_collector_db

    sdb = tmp_path / "s.db"
    cdb = tmp_path / "c.db"
    state_db.init_db(sdb)
    state_db.save_task({"task_id": "t-r11", "run_id": "r-r11", "workflow_id": "wf",
                        "customer": "acme", "task_type": "quote", "status": "completed"}, db_path=sdb)
    state_db.save_steer({"steer_id": "s-r11", "task_id": "t-r11",
                         "instruction": "有效报价口径请长期遵循执行下去",
                         "operator": "human", "urgent": True, "status": "dispatched"}, db_path=sdb)
    release = threading.Event()
    mode = {"fail": True}

    class FlapHub:
        def submit_candidate(self, exp):
            if mode["fail"]:
                release.wait(timeout=20)
                return {"ok": False, "error": "offline", "message": "down"}
            return {"ok": True, "experience_id": exp.experience_id}

    col = ExperienceCollector(db_path=cdb, hub_client=FlapHub(), contributor="u")
    out = {"outcome_id": "o-r11", "run_id": "r-r11", "qualified_success": True,
           "human_intervention_count": 1, "requirements_satisfied": True,
           "verification_passed": True, "final_status": "completed"}
    t = threading.Thread(target=col.process_task_settlement,
                         args=("t-r11",), kwargs={"outcome": out, "state_db_path": sdb}, daemon=True)
    t.start()
    for _ in range(100):
        conn = _get_collector_db(cdb)
        try:
            row = conn.execute("SELECT status FROM local_collector_records WHERE task_id='t-r11'").fetchone()
        finally:
            conn.close()
        if row and row["status"] == "pending_upload":
            break
        time.sleep(0.1)
    mode["fail"] = False
    assert col.retry_pending_uploads(force=True)["succeeded"] == 1
    release.set()
    t.join(timeout=20)
    conn = _get_collector_db(cdb)
    try:
        final = conn.execute("SELECT status FROM local_collector_records WHERE task_id='t-r11'").fetchone()
    finally:
        conn.close()
    assert final["status"] == "uploaded"


def test_sync_failing_hub_exits_nonzero(tmp_path):
    import os
    cdb = tmp_path / "c.db"
    conn = sqlite3.connect(str(cdb))
    conn.execute("""CREATE TABLE local_collector_records (task_id TEXT NOT NULL, run_id TEXT NOT NULL,
      outcome_id TEXT, experience_id TEXT, status TEXT NOT NULL, candidate_json TEXT, outcome_json TEXT,
      last_error TEXT, retry_count INTEGER NOT NULL DEFAULT 0, next_retry_at REAL NOT NULL DEFAULT 0,
      created_at REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY (task_id, run_id))""")
    exp = {"experience_id": "e-r11", "type": "business_rule", "title": "t", "content": "x",
           "contributor": "u", "applies_to": {"customer": "acme"}, "source": {"task_id": "t", "run_id": "r"}}
    now = time.time()
    conn.execute("INSERT INTO local_collector_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("t", "r", "o", "e-r11", "pending_upload", json.dumps(exp),
                  None, "x", 0, 0, now, now))
    conn.commit()
    conn.close()
    env = dict(os.environ, HERDR_COLLECTOR_DB_PATH=str(cdb), HERDR_STATE_DB=str(tmp_path / "s.db"),
               HERDR_HUB_URL="http://127.0.0.1:1", HERDR_HUB_TOKEN="t")
    p = subprocess.run([sys.executable, "bin/herdr-task", "sync-experience", "--force", "--json"],
                       capture_output=True, text=True, env=env, timeout=120)
    assert p.returncode != 0
    body = json.loads(p.stdout)
    assert body["failed"] >= 1 and body["pending_upload_remaining"] >= 1


def test_sync_quarantined_collection_exits_nonzero(tmp_path):
    import os
    cdb = tmp_path / "c.db"
    conn = sqlite3.connect(str(cdb))
    conn.execute("""CREATE TABLE local_collector_records (task_id TEXT NOT NULL, run_id TEXT NOT NULL,
      outcome_id TEXT, experience_id TEXT, status TEXT NOT NULL, candidate_json TEXT, outcome_json TEXT,
      last_error TEXT, retry_count INTEGER NOT NULL DEFAULT 0, next_retry_at REAL NOT NULL DEFAULT 0,
      created_at REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY (task_id, run_id))""")
    now = time.time()
    conn.execute("INSERT INTO local_collector_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("ghost", "r", None, None, "pending_collection", None,
                  '{"run_id":"r"}', None, 0, 0, now, now))
    conn.commit()
    conn.close()
    env = dict(os.environ, HERDR_COLLECTOR_DB_PATH=str(cdb), HERDR_STATE_DB=str(tmp_path / "s.db"))
    p = subprocess.run([sys.executable, "bin/herdr-task", "sync-experience", "--force", "--json"],
                       capture_output=True, text=True, env=env, timeout=120)
    assert p.returncode != 0
    body = json.loads(p.stdout)
    assert body["pending_collection_failed"] >= 1
