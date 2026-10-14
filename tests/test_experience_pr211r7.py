"""PR #211 R7 regression tests: XSS, mandatory inheritance, admin listing, poison queue."""

import json
import tempfile
import time
from pathlib import Path

import pytest

from herdr.experience.client import ExperienceClient
from herdr.experience.hub_server import ExperienceHubServer
from herdr.experience.models import AppliesTo, BusinessRuleExperience, ExperienceSource


def _mkexp(eid, customer="acme"):
    return BusinessRuleExperience(
        experience_id=eid, title=f"t-{eid}",
        content="content long enough xxxxx yyyy", contributor="x",
        applies_to=AppliesTo(customer=customer, task_type="quote"),
        source=ExperienceSource(task_id="t", run_id="r"),
    )


def test_evil_experience_id_rejected_and_escaped():
    with tempfile.TemporaryDirectory() as td:
        srv = ExperienceHubServer(host="127.0.0.1", port=0, db_path=Path(td) / "h.db")
        srv.storage.register_token("tok", user_id="evil", role="contributor", allowed_customers=["acme"])
        srv.start(block=False)
        time.sleep(0.05)
        try:
            url = f"http://127.0.0.1:{srv.port}"
            c = ExperienceClient(base_url=url, token="tok")
            evil = "x');alert(document.domain);//"
            res = c.submit_candidate(_mkexp(evil))
            assert res.get("ok") is False
            assert res.get("status_code") == 400
            assert srv.storage.get_experience(evil) is None
            assert c.submit_candidate(_mkexp("ok-id_123")).get("ok") is True
        finally:
            srv.stop()
    text = Path("console/herdr_factory_console.py").read_text()
    assert "jsArg(c.experience_id)" in text
    assert "jsArg(r.experience_id)" in text
    assert "sessionStorage" in text
    assert "localStorage.getItem('herdr_hub_token')" not in text


def test_replacement_inherits_mandatory_policy():
    from herdr.controller_actions import inherit_experience_policy
    old = {"task_id": "t1", "customer": "acme", "business_task_type": "quote",
           "mandatory_experience_rules": True}
    assert inherit_experience_policy(old) == {
        "customer": "acme", "business_task_type": "quote",
        "mandatory_experience_rules": True,
    }
    assert inherit_experience_policy({"task_id": "t2"}) == {}
    from herdr.controller_actions import generate_controller_actions
    actions = generate_controller_actions(
        {"task_id": "t1", "workflow_id": "wf", "stage": "test", "status": "failed",
         "stage_verdict_note": "FAIL", "agent": "codex", "customer": "acme",
         "mandatory_experience_rules": True},
        {"workflow_id": "wf"}, project_root="/tmp",
    )
    launched = [a for a in actions if a.api_payload.get("type") == "launch"]
    assert launched
    for a in launched:
        assert a.api_payload.get("customer") == "acme"
        assert a.api_payload.get("mandatory_experience_rules") is True
        assert "--customer" in a.command_line and "acme" in a.command_line


def test_admin_listing_respects_scope():
    with tempfile.TemporaryDirectory() as td:
        srv = ExperienceHubServer(host="127.0.0.1", port=0, db_path=Path(td) / "h.db")
        srv.storage.register_token("tok_adm", user_id="adm", role="admin", allowed_customers=["*"])
        srv.storage.register_token("tok_a", user_id="rev", role="reviewer", allowed_customers=["acme"])
        srv.start(block=False)
        time.sleep(0.05)
        try:
            url = f"http://127.0.0.1:{srv.port}"
            adm = ExperienceClient(base_url=url, token="tok_adm")
            assert adm.submit_candidate(_mkexp("g-r7", "*")).get("ok") is True
            assert adm.submit_candidate(_mkexp("c-r7", "acme")).get("ok") is True
            assert adm.submit_candidate(_mkexp("b-r7", "beta")).get("ok") is True
            for eid in ("g-r7", "c-r7", "b-r7"):
                assert adm.review_candidate(eid, action="approve").get("ok") is True
            assert sorted(r.experience_id for r in adm.query_rules(scope="all")) == ["b-r7", "c-r7", "g-r7"]
            rev = ExperienceClient(base_url=url, token="tok_a")
            assert sorted(r.experience_id for r in rev.query_rules(scope="all")) == ["c-r7", "g-r7"]
            assert sorted(r.experience_id for r in adm.query_rules()) == ["g-r7"]
        finally:
            srv.stop()


def test_poison_row_does_not_block_queue(tmp_path):
    from herdr import state_db
    from herdr.experience.collector import ExperienceCollector, _get_collector_db

    sdb = tmp_path / "s.db"
    cdb = tmp_path / "c.db"
    state_db.init_db(sdb)
    state_db.save_task({"task_id": "good", "run_id": "r-good", "workflow_id": "wf",
                        "customer": "acme", "task_type": "quote", "status": "completed"}, db_path=sdb)
    state_db.save_steer({"steer_id": "s1", "task_id": "good",
                         "instruction": "有效报价口径请长期遵循执行下去",
                         "operator": "human", "urgent": True, "status": "dispatched"}, db_path=sdb)
    col = ExperienceCollector(db_path=cdb, hub_client=None, contributor="u")
    conn = _get_collector_db(cdb)
    now = time.time()
    conn.execute("INSERT INTO local_collector_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("ghost", "r-ghost", None, None, "pending_collection", None,
                  '{"run_id":"r-ghost"}', None, 0, 0, now - 100, now - 100))
    conn.execute("INSERT INTO local_collector_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("good", "r-good", None, None, "pending_collection", None,
                  json.dumps({"outcome_id": "o1", "run_id": "r-good", "qualified_success": True,
                              "verification_passed": True, "final_status": "completed"}),
                  None, 0, 0, now - 10, now - 10))
    conn.commit()
    conn.close()
    col.process_pending(state_db_path=sdb)
    conn = _get_collector_db(cdb)
    try:
        ghost = conn.execute("SELECT status FROM local_collector_records WHERE task_id='ghost'").fetchone()
        good = conn.execute("SELECT status FROM local_collector_records WHERE task_id='good'").fetchone()
    finally:
        conn.close()
    assert ghost["status"] != "pending_collection"
    assert good["status"] in ("pending_upload", "no_candidate")
