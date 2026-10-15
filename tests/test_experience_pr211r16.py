"""PR #211 R16: replay policy inheritance; force single-attempt per row."""

import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch


def test_build_launch_argv_carries_policy():
    from herdr.replay_engine import build_launch_argv
    argv = build_launch_argv(
        task_id="t", workflow_id="wf", source=".", agent="auto",
        goal="g", prompt="p", node="dev", run_id="r", replay_of="o",
        customer="acme", business_task_type="quote", mandatory_experience_rules=True,
    )
    assert "--customer" in argv and "acme" in argv
    assert "--business-task-type" in argv and "quote" in argv
    assert "--mandatory-experience-rules" in argv
    plain = build_launch_argv(
        task_id="t", workflow_id="wf", source=".", agent="auto",
        goal="g", prompt="p", node="dev", run_id="r", replay_of="o",
    )
    assert "--customer" not in plain and "--mandatory-experience-rules" not in plain


def test_replay_run_inherits_policy():
    from herdr import state_db
    from herdr import replay_engine

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        db = td / "s.db"
        state_db.init_db(db)
        state_db.save_task({"task_id": "src1", "run_id": "run-src1", "workflow_id": "wf-src",
                            "customer": "acme", "business_task_type": "quote",
                            "mandatory_experience_rules": True, "status": "completed",
                            "goal": "g", "agent": "auto"}, db_path=db)
        seen = {}

        def fake_probe(argv, timeout=120, db_path=None):
            seen.setdefault("argvs", []).append(list(argv))
            from herdr import state_db as _sdb
            _sdb.save_task({"task_id": "t-rep1", "run_id": "run-rep1", "workflow_id": "wf-rep1",
                            "replay_of": "run-src1", "status": "pending"}, db_path=db)
            return {"argv": list(argv), "ok": True}

        with patch.dict("os.environ", {"HERDR_WORKFLOW_DOCS_DIR": str(td / "docs")}), \
             patch.object(replay_engine.projects, "freeze_run_definition", return_value="/tmp/frozen.json"), \
             patch.object(replay_engine.projects, "register_workflow", return_value={"ok": True}), \
             patch.object(replay_engine, "_run_probe_argv", side_effect=fake_probe):
            out = replay_engine.replay_run("run-src1", db_path=db,
                                           definition={"nodes": []},
                                           task_id="t-rep1", replay_run_id="run-rep1",
                                           workflow_id="wf-rep1")
        launch = out["launch_argv"]
        assert "--customer" in launch and "acme" in launch
        assert "--mandatory-experience-rules" in launch


def test_force_attempts_failing_row_once(tmp_path):
    from herdr.experience.collector import ExperienceCollector, _get_collector_db
    import sqlite3

    cdb = tmp_path / "c.db"
    conn = sqlite3.connect(str(cdb))
    conn.execute("""CREATE TABLE local_collector_records (task_id TEXT NOT NULL, run_id TEXT NOT NULL,
      outcome_id TEXT, experience_id TEXT, status TEXT NOT NULL, candidate_json TEXT, outcome_json TEXT,
      last_error TEXT, retry_count INTEGER NOT NULL DEFAULT 0, next_retry_at REAL NOT NULL DEFAULT 0,
      created_at REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY (task_id, run_id))""")
    exp = {"experience_id": "e", "type": "business_rule", "title": "t", "content": "x",
           "contributor": "u", "applies_to": {"customer": "acme"}, "source": {"task_id": "t", "run_id": "r"}}
    import json
    now = time.time()
    conn.execute("INSERT INTO local_collector_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("t", "r", None, "e", "pending_upload", json.dumps(exp),
                  None, "x", 0, 0, now, now))
    conn.commit()
    conn.close()
    calls = []

    class FailHub:
        def submit_candidate(self, e):
            calls.append(1)
            return {"ok": False, "error": "down", "message": "down"}

    col = ExperienceCollector(db_path=cdb, hub_client=FailHub(), contributor="u")
    res = col.recover_and_sync(force=True)
    assert len(calls) == 1
    assert res["failed"] == 1 and res["pending_upload_remaining"] == 1
