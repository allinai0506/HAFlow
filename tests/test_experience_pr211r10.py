"""PR #211 R10: normalize carry-through, settlement single-flight, sync exit code."""

import json
import sqlite3

from herdr.experience.collector import _get_collector_db
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def test_normalize_keeps_business_keys_both_paths():
    from herdr.workflow import normalize_workflow
    from herdr.direct_dispatch import normalize_node
    n = normalize_workflow({"nodes": [{"id": "q", "default_task_type": "quote",
                                       "customer": "acme", "business_task_type": "quote",
                                       "mandatory_experience_rules": True}]})["nodes"][0]
    assert (n["customer"], n["business_task_type"], n["mandatory_experience_rules"]) == ("acme", "quote", True)
    legacy = {"stages": [{"key": "q"}],
              "stage_policies": {"q": {"customer": "acme", "business_task_type": "quote",
                                       "mandatory_experience_rules": True}}}
    n2 = normalize_workflow(legacy)["nodes"][0]
    assert (n2["customer"], n2["business_task_type"], n2["mandatory_experience_rules"]) == ("acme", "quote", True)
    planned = normalize_node(n)
    assert (planned["customer"], planned["mandatory_experience_rules"]) == ("acme", True)


def test_async_settlement_single_flight(tmp_path):
    from herdr import state_db
    from herdr.experience.collector import ExperienceCollector

    sdb = tmp_path / "s.db"
    cdb = tmp_path / "c.db"
    state_db.init_db(sdb)
    state_db.save_task({"task_id": "t-r10", "run_id": "r-r10", "workflow_id": "wf",
                        "customer": "acme", "task_type": "quote", "status": "completed"}, db_path=sdb)
    state_db.save_steer({"steer_id": "s-r10", "task_id": "t-r10",
                         "instruction": "有效报价口径请长期遵循执行下去",
                         "operator": "human", "urgent": True, "status": "dispatched"}, db_path=sdb)
    calls = []

    class SlowHub:
        def submit_candidate(self, exp):
            calls.append(1)
            time.sleep(2)
            return {"ok": False, "error": "network_error", "message": "down"}

    col = ExperienceCollector(db_path=cdb, hub_client=SlowHub(), contributor="u")
    out = {"outcome_id": "o-r10", "run_id": "r-r10", "qualified_success": True,
           "human_intervention_count": 1, "requirements_satisfied": True,
           "verification_passed": True, "final_status": "completed"}
    threads = [col.process_task_settlement_async("t-r10", outcome=out, state_db_path=sdb)
               for _ in range(3)]
    for t in threads:
        if t is not None:
            t.join(timeout=20)
    time.sleep(0.5)
    assert len(calls) == 1


def test_sync_no_client_fails_loud(tmp_path):
    import os
    cdb = tmp_path / "c.db"
    conn = _get_collector_db(cdb)
    now = time.time()
    conn.execute("INSERT INTO local_collector_records (task_id, run_id, outcome_id, experience_id, status, candidate_json, outcome_json, last_error, retry_count, next_retry_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("t9", "r9", "o9", "e9", "pending_upload", '{"x":1}', None,
                  "down", 3, now + 280, now, now))
    conn.commit()
    conn.close()
    env = dict(os.environ, HERDR_COLLECTOR_DB_PATH=str(cdb), HERDR_STATE_DB=str(tmp_path / "s.db"))
    env.pop("HERDR_HUB_URL", None)
    env.pop("HERDR_HUB_TOKEN", None)
    p = subprocess.run([sys.executable, "bin/herdr-task", "sync-experience", "--json"],
                       capture_output=True, text=True, env=env)
    assert p.returncode != 0
    body = json.loads(p.stdout or "{}")
    assert body.get("pending_upload_remaining", 0) >= 1
