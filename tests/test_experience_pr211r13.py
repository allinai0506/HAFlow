"""PR #211 R13: trusted gate event, collection failure propagation, upsert guard."""

import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch


def test_gate_runs_on_target_event():
    import yaml
    text = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "codex-gate.yml").read_text()
    wf = yaml.safe_load(text)
    assert "pull_request_target" in wf[True if "on" not in wf else "on"]
    assert "pull_request:" not in text


def test_collection_failures_propagate():
    from herdr import state_db
    from herdr.experience.collector import ExperienceCollector, _get_collector_db

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        sdb = td / "s.db"
        cdb = tmp_cdb = td / "c.db"
        state_db.init_db(sdb)
        col = ExperienceCollector(db_path=cdb, hub_client=None, contributor="u")
        conn = _get_collector_db(cdb)
        now = time.time()
        conn.execute("INSERT INTO local_collector_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("t-r13", "r-r13", None, None, "pending_collection", None,
                      '{"run_id":"r-r13"}', None, 0, 0, now, now))
        conn.commit()
        conn.close()
        with patch("herdr.state_db.get_task", side_effect=RuntimeError("db gone")):
            res = col.recover_and_sync(state_db_path=sdb)
        assert res["pending_collection_failed"] >= 1
        assert res["pending_collection_remaining"] >= 1


def test_upsert_keeps_concurrent_uploaded():
    import herdr.experience.collector as C
    from herdr import state_db

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        sdb, cdb = td / "s.db", td / "c.db"
        state_db.init_db(sdb)
        state_db.save_task({"task_id": "t", "run_id": "r", "workflow_id": "wf",
                            "customer": "acme", "task_type": "quote", "status": "completed"}, db_path=sdb)
        state_db.save_steer({"steer_id": "s1", "task_id": "t",
                             "instruction": "有效报价口径请长期遵循执行下去",
                             "operator": "human", "urgent": True, "status": "dispatched"}, db_path=sdb)
        conn = C._get_collector_db(cdb)
        now = time.time()
        conn.execute("INSERT INTO local_collector_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("t", "r", "o", "e", "pending_upload", '{"x":1}', None,
                      "down", 1, 0, now, now))
        conn.commit()
        conn.close()
        real_extract = C.extract_candidate_from_facts

        def flip_then_extract(**kw):
            c = C._get_collector_db(cdb)
            with c:
                c.execute("UPDATE local_collector_records SET status='uploaded', last_error=NULL, "
                          "retry_count=0, next_retry_at=0, updated_at=? WHERE task_id='t'", (time.time(),))
            c.close()
            return real_extract(**kw)

        class FailHub:
            def submit_candidate(self, exp):
                return {"ok": False, "error": "offline", "message": "down"}

        with patch.object(C, "extract_candidate_from_facts", side_effect=flip_then_extract):
            col = C.ExperienceCollector(db_path=cdb, hub_client=FailHub(), contributor="u")
            out = {"outcome_id": "o", "run_id": "r", "qualified_success": True,
                   "human_intervention_count": 1, "verification_passed": True,
                   "final_status": "completed"}
            col.process_task_settlement("t", outcome=out, state_db_path=sdb)
        conn = C._get_collector_db(cdb)
        try:
            assert conn.execute("SELECT status FROM local_collector_records WHERE task_id='t'").fetchone()[0] == "uploaded"
        finally:
            conn.close()


def test_sync_collection_failure_exits_nonzero():
    import os
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        cdb = td / "c.db"
        sdb = td / "s.db"
        sdb.write_bytes(b"not a database")
        conn = sqlite3.connect(str(cdb))
        conn.execute("""CREATE TABLE local_collector_records (task_id TEXT NOT NULL, run_id TEXT NOT NULL,
          outcome_id TEXT, experience_id TEXT, status TEXT NOT NULL, candidate_json TEXT, outcome_json TEXT,
          last_error TEXT, retry_count INTEGER NOT NULL DEFAULT 0, next_retry_at REAL NOT NULL DEFAULT 0,
          created_at REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY (task_id, run_id))""")
        now = time.time()
        conn.execute("INSERT INTO local_collector_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("t", "r", None, None, "pending_collection", None,
                      '{"run_id":"r"}', None, 0, 0, now, now))
        conn.commit()
        conn.close()
        env = dict(os.environ, HERDR_COLLECTOR_DB_PATH=str(cdb), HERDR_STATE_DB=str(sdb))
        p = subprocess.run([sys.executable, "bin/herdr-task", "sync-experience", "--force", "--json"],
                           capture_output=True, text=True, env=env, timeout=120)
        assert p.returncode != 0
