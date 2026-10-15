"""PR #211 R17: successor policy carry-through; backoff on first failure."""

import sys
import tempfile
import time
from pathlib import Path


def test_successor_argv_carries_policy():
    from herdr.workflow_recovery import successor_launch_command
    pred = {"task_id": "t-old", "commit": "a" * 40, "node": "quote_calc",
            "stage": "quote", "customer": "acme", "business_task_type": "quote",
            "mandatory_experience_rules": True, "dispatch_role": "worker",
            "dispatch_round": 2, "goal": "g"}
    wf = {"workflow_id": "wf", "execution_id": "exe1"}
    argv = successor_launch_command("herdr-task", wf, pred, "t-new", ".", "fix it")
    assert "--customer" in argv and "acme" in argv
    assert "--business-task-type" in argv and "quote" in argv
    assert "--mandatory-experience-rules" in argv
    bare = successor_launch_command(
        "herdr-task", wf,
        {"task_id": "t-old", "commit": "a" * 40, "node": "n", "stage": "s"},
        "t-new2", ".", "fix")
    assert "--customer" not in bare and "--mandatory-experience-rules" not in bare


def test_first_failure_schedules_backoff(tmp_path):
    from herdr import state_db
    from herdr.experience.collector import ExperienceCollector, _get_collector_db

    sdb, cdb = tmp_path / "s.db", tmp_path / "c.db"
    state_db.init_db(sdb)
    state_db.save_task({"task_id": "t-r17", "run_id": "r-r17", "workflow_id": "wf",
                        "customer": "acme", "task_type": "quote", "status": "completed"}, db_path=sdb)
    state_db.save_steer({"steer_id": "s-r17", "task_id": "t-r17",
                         "instruction": "有效报价口径请长期遵循执行下去",
                         "operator": "human", "urgent": True, "status": "dispatched"}, db_path=sdb)

    class FailHub:
        def submit_candidate(self, exp):
            return {"ok": False, "error": "down", "message": "down"}

    col = ExperienceCollector(db_path=cdb, hub_client=FailHub(), contributor="u")
    out = {"outcome_id": "o-r17", "run_id": "r-r17", "qualified_success": True,
           "human_intervention_count": 1, "requirements_satisfied": True,
           "verification_passed": True, "final_status": "completed"}
    col.process_task_settlement("t-r17", outcome=out, state_db_path=sdb)
    conn = _get_collector_db(cdb)
    try:
        row = conn.execute("SELECT retry_count, next_retry_at FROM local_collector_records WHERE task_id='t-r17'").fetchone()
    finally:
        conn.close()
    assert row["retry_count"] >= 1
    assert row["next_retry_at"] > time.time()


def test_sequential_settlements_upload_once(tmp_path):
    from herdr import state_db
    from herdr.experience.collector import ExperienceCollector

    sdb, cdb = tmp_path / "s.db", tmp_path / "c.db"
    state_db.init_db(sdb)
    state_db.save_task({"task_id": "t-s17", "run_id": "r-s17", "workflow_id": "wf",
                        "customer": "acme", "task_type": "quote", "status": "completed"}, db_path=sdb)
    state_db.save_steer({"steer_id": "s-s17", "task_id": "t-s17",
                         "instruction": "有效报价口径请长期遵循执行下去",
                         "operator": "human", "urgent": True, "status": "dispatched"}, db_path=sdb)
    calls = []

    class FailHub:
        def submit_candidate(self, exp):
            calls.append(1)
            return {"ok": False, "error": "down", "message": "down"}

    col = ExperienceCollector(db_path=cdb, hub_client=FailHub(), contributor="u")
    out = {"outcome_id": "o-s17", "run_id": "r-s17", "qualified_success": True,
           "human_intervention_count": 1, "requirements_satisfied": True,
           "verification_passed": True, "final_status": "completed"}
    for _ in range(3):
        th = col.process_task_settlement_async("t-s17", outcome=dict(out), state_db_path=sdb)
        if th:
            th.join(timeout=20)
        time.sleep(0.3)
    assert len(calls) == 1
