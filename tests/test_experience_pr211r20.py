"""PR #211 R20: legacy empty task_type matching; outcome lookup DB identity."""

import json
import sqlite3
import time


def _publish_legacy_empty_type(server, client, experience_id="r20-legacy"):
    """A published rule stored with an empty task type (legacy representation).

    The HTTP path normalizes '' to NULL in AppliesTo.from_dict, so the row is
    written through the storage layer to reproduce pre-normalization data.
    """
    from herdr.experience.models import AppliesTo, BusinessRuleExperience, ExperienceSource

    exp = BusinessRuleExperience(
        experience_id=experience_id, title="legacy general rule",
        content="Legacy general rule for every quotation task, follow it every time",
        contributor="a",
        applies_to=AppliesTo(customer="acme", task_type=""),
        source=ExperienceSource(task_id="t-legacy", run_id="r-legacy"))
    server.storage.save_candidate(exp, actor="a")
    assert client.review_candidate(experience_id, action="approve").get("status") == "published"


def test_legacy_empty_task_type_matches_typed_query(tmp_path):
    from herdr.experience.client import ExperienceClient
    from herdr.experience.hub_server import ExperienceHubServer

    srv = ExperienceHubServer(host="127.0.0.1", port=0, db_path=tmp_path / "h.db")
    srv.storage.register_token("adm", user_id="a", role="admin", allowed_customers=["*"])
    srv.start(block=False)
    time.sleep(0.05)
    try:
        admin = ExperienceClient(base_url=f"http://127.0.0.1:{srv.port}", token="adm")
        _publish_legacy_empty_type(srv, admin)
        typed = admin.query_rules(customer="acme", task_type="quote")
        assert [r.experience_id for r in typed] == ["r20-legacy"]
        generic = admin.query_rules(customer="acme", task_type="general")
        assert [r.experience_id for r in generic] == ["r20-legacy"]
    finally:
        srv.stop()


def test_pending_collection_uses_caller_state_db(tmp_path):
    from herdr import state_db
    from herdr.experience.collector import ExperienceCollector, _get_collector_db

    sdb, cdb = tmp_path / "nondefault.db", tmp_path / "c.db"
    state_db.init_db(sdb)
    state_db.save_task({"task_id": "t-r20", "run_id": "r-r20", "workflow_id": "wf",
                        "customer": "acme", "task_type": "quote", "status": "completed"},
                       db_path=sdb)
    state_db.save_steer({"steer_id": "s-r20", "task_id": "t-r20",
                         "instruction": "有效报价口径请长期遵循执行下去",
                         "operator": "human", "urgent": True, "status": "dispatched"}, db_path=sdb)
    state_db.insert_execution_outcome({
        "outcome_id": "o-r20", "run_id": "r-r20", "task_id": "t-r20", "workflow_id": "wf",
        "agent": "auto", "node": "quote", "final_status": "completed",
        "source_eval_id": "e1", "source_eval_revision": 1, "recorded_at": time.time(),
        "qualified_success": True, "verification_passed": True}, db_path=sdb)

    conn = _get_collector_db(cdb)
    now = time.time()
    # Recovery row without outcome_json: the extractor must consult the caller's DB.
    conn.execute("INSERT INTO local_collector_records (task_id, run_id, outcome_id, experience_id, status, candidate_json, outcome_json, last_error, retry_count, next_retry_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("t-r20", "r-r20", None, None, "pending_collection", None, None,
                  None, 0, 0, now, now))
    conn.commit()
    conn.close()

    class FailHub:
        def submit_candidate(self, exp):
            return {"ok": False, "error": "down", "message": "down"}

    col = ExperienceCollector(db_path=cdb, hub_client=FailHub(), contributor="u")
    stats = col.process_pending(state_db_path=sdb)
    assert stats["processed"] == 1, stats
    conn = _get_collector_db(cdb)
    try:
        row = conn.execute("SELECT status, candidate_json, experience_id, last_error "
                           "FROM local_collector_records WHERE task_id='t-r20'").fetchone()
    finally:
        conn.close()
    assert row["status"] == "pending_upload", row["last_error"]
    assert json.loads(row["candidate_json"])["experience_id"] == row["experience_id"]