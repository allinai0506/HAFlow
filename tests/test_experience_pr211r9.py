"""PR #211 R9: async settlement must not immediately retry the just-failed upload."""

import time
from pathlib import Path

from herdr import state_db
from herdr.experience.collector import ExperienceCollector


def test_async_settlement_uploads_once_on_failure(tmp_path):
    sdb = tmp_path / "s.db"
    cdb = tmp_path / "c.db"
    state_db.init_db(sdb)
    state_db.save_task({"task_id": "t-r9", "run_id": "r-r9", "workflow_id": "wf",
                        "customer": "acme", "task_type": "quote", "status": "completed"}, db_path=sdb)
    state_db.save_steer({"steer_id": "s-r9", "task_id": "t-r9",
                         "instruction": "有效报价口径请长期遵循执行下去",
                         "operator": "human", "urgent": True, "status": "dispatched"}, db_path=sdb)

    calls = []

    class FailHub:
        def submit_candidate(self, exp):
            calls.append(time.time())
            return {"ok": False, "error": "network_error", "message": "down"}

    col = ExperienceCollector(db_path=cdb, hub_client=FailHub(), contributor="u")
    out = {"outcome_id": "o-r9", "run_id": "r-r9", "qualified_success": True,
           "human_intervention_count": 1, "requirements_satisfied": True,
           "verification_passed": True, "final_status": "completed"}
    t = col.process_task_settlement_async("t-r9", outcome=out, state_db_path=sdb)
    t.join(timeout=20)
    time.sleep(0.5)
    assert len(calls) == 1
