"""PR #211 R15: same-pass exclusion; force drains fully."""

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


def test_same_pass_retry_skips_newly_collected(tmp_path):
    from herdr import state_db
    from herdr.experience.collector import ExperienceCollector

    sdb, cdb = tmp_path / "s.db", tmp_path / "c.db"
    state_db.init_db(sdb)
    state_db.save_task({"task_id": "t-r15", "run_id": "r-r15", "workflow_id": "wf",
                        "customer": "acme", "task_type": "quote", "status": "completed"}, db_path=sdb)
    state_db.save_steer({"steer_id": "s-r15", "task_id": "t-r15",
                         "instruction": "有效报价口径请长期遵循执行下去",
                         "operator": "human", "urgent": True, "status": "dispatched"}, db_path=sdb)
    calls = []

    class FailHub:
        def submit_candidate(self, exp):
            calls.append(1)
            return {"ok": False, "error": "network_error", "message": "down"}

    col = ExperienceCollector(db_path=cdb, hub_client=FailHub(), contributor="u")
    out = {"outcome_id": "o-r15", "run_id": "r-r15", "qualified_success": True,
           "human_intervention_count": 1, "requirements_satisfied": True,
           "verification_passed": True, "final_status": "completed"}
    col.enqueue_task_settlement("t-r15", out, state_db_path=sdb)
    col.recover_and_sync(state_db_path=sdb)
    assert len(calls) == 1


def test_force_drains_beyond_batch_limit(tmp_path):
    import os
    cdb = tmp_path / "c.db"
    conn = sqlite3.connect(str(cdb))
    _schema(conn)
    exp = {"experience_id": "e", "type": "business_rule", "title": "t", "content": "x",
           "contributor": "u", "applies_to": {"customer": "acme"}, "source": {"task_id": "t", "run_id": "r"}}
    now = time.time()
    for i in range(51):
        conn.execute("INSERT INTO local_collector_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (f"t{i}", f"r{i}", None, "e", "pending_upload", json.dumps(exp),
                      None, "x", 0, 0, now, now))
    conn.commit()
    conn.close()
    with tempfile.TemporaryDirectory() as hub_td:
        from herdr.experience.hub_server import ExperienceHubServer
        srv = ExperienceHubServer(host="127.0.0.1", port=0, db_path=Path(hub_td) / "h.db")
        srv.storage.register_token("tok", user_id="u", role="contributor", allowed_customers=["acme"])
        srv.start(block=False)
        time.sleep(0.05)
        try:
            env = dict(os.environ, HERDR_COLLECTOR_DB_PATH=str(cdb),
                       HERDR_STATE_DB=str(tmp_path / "s.db"),
                       HERDR_HUB_URL=f"http://127.0.0.1:{srv.port}", HERDR_HUB_TOKEN="tok")
            p = subprocess.run([sys.executable, "bin/herdr-task", "sync-experience", "--force", "--json"],
                               capture_output=True, text=True, env=env, timeout=180)
            assert p.returncode == 0, p.stderr
            body = json.loads(p.stdout)
            assert body["succeeded"] == 51 and body["pending_upload_remaining"] == 0
        finally:
            srv.stop()
