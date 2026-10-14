"""PR #211 R6 regression tests: migration, controller chain, idempotent privacy,
invalidation ordering. All via real entries (legacy DB, planner, HTTP, docs dir).
"""

import json
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from herdr.direct_dispatch import experience_launch_flags
from herdr.experience.collector import fold_decisions_by_id
from herdr.experience.client import ExperienceClient
from herdr.experience.hub_server import ExperienceHubServer
from herdr.experience.models import AppliesTo, BusinessRuleExperience, ExperienceSource


def _mkexp(eid, customer="acme"):
    return BusinessRuleExperience(
        experience_id=eid, title=f"t-{eid}",
        content="secret sauce content xxxxx yyyy", contributor="x",
        applies_to=AppliesTo(customer=customer, task_type="quote"),
        source=ExperienceSource(task_id="task_A", run_id="run_A"),
    )


def _legacy_db(path, tokens):
    conn = sqlite3.connect(str(path))
    conn.execute("""CREATE TABLE auth_tokens (token TEXT PRIMARY KEY, user_id TEXT NOT NULL,
      role TEXT NOT NULL, allowed_customers_json TEXT NOT NULL, created_at REAL NOT NULL)""")
    conn.execute("""CREATE TABLE experiences (experience_id TEXT PRIMARY KEY, type TEXT NOT NULL,
      title TEXT NOT NULL, content TEXT NOT NULL, contributor TEXT NOT NULL, customer TEXT,
      task_type TEXT, status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
      applies_to_json TEXT NOT NULL, source_json TEXT NOT NULL, review_json TEXT,
      audit_trail_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
    conn.execute("""CREATE TABLE audit_logs (id INTEGER PRIMARY KEY AUTOINCREMENT,
      action TEXT NOT NULL, actor TEXT NOT NULL, experience_id TEXT,
      details_json TEXT, timestamp REAL NOT NULL)""")
    for tok, user, role in tokens:
        conn.execute("INSERT INTO auth_tokens VALUES (?,?,?,?,?)",
                     (tok, user, role, json.dumps(["*"]), time.time()))
    conn.commit()
    conn.close()


def test_legacy_migration_hashes_raw_tokens():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "legacy.db"
        import secrets
        raw_hex = secrets.token_hex(32)  # openssl rand -hex 32 style
        assert len(raw_hex) == 64
        _legacy_db(db, [(raw_hex, "admin", "admin"), ("plain-token-abc", "emp", "contributor")])
        srv = ExperienceHubServer(host="127.0.0.1", port=0, db_path=db)
        assert srv.storage.has_admin() is True
        assert srv.storage.authenticate_token(raw_hex)["user_id"] == "admin"
        assert srv.storage.authenticate_token("plain-token-abc")["user_id"] == "emp"


def test_planner_spec_and_flags_carry_business_context():
    from herdr.direct_dispatch import _dispatch_spec, normalize_node
    node = normalize_node({"id": "quote_calc", "default_task_type": "quote",
                            "customer": "acme", "business_task_type": "quote",
                            "mandatory_experience_rules": True})
    assert node["customer"] == "acme"
    spec = _dispatch_spec(node, "req", "goal", [], "t1")
    assert spec["customer"] == "acme"
    assert spec["business_task_type"] == "quote"
    assert spec["mandatory_experience_rules"] is True
    flags = experience_launch_flags(spec)
    assert flags == ["--customer", "acme", "--business-task-type", "quote",
                     "--mandatory-experience-rules"]
    assert experience_launch_flags({}) == []


def test_resubmit_returns_minimal_for_non_owner():
    with tempfile.TemporaryDirectory() as td:
        srv = ExperienceHubServer(host="127.0.0.1", port=0, db_path=Path(td) / "h.db")
        srv.storage.register_token("tok_a", user_id="emp_a", role="contributor", allowed_customers=["acme"])
        srv.storage.register_token("tok_b", user_id="emp_b", role="contributor", allowed_customers=["acme"])
        srv.start(block=False)
        time.sleep(0.05)
        try:
            url = f"http://127.0.0.1:{srv.port}"
            ca = ExperienceClient(base_url=url, token="tok_a")
            cb = ExperienceClient(base_url=url, token="tok_b")
            assert ca.submit_candidate(_mkexp("dup-r6")).get("reason") == "created"
            again_owner = ca.submit_candidate(_mkexp("dup-r6"))
            assert again_owner.get("reason") == "already_exists"
            assert again_owner.get("experience", {}).get("source", {}).get("task_id") == "task_A"
            again_other = cb.submit_candidate(_mkexp("dup-r6"))
            assert again_other.get("ok") is True
            assert again_other.get("reason") == "already_exists"
            assert "experience" not in again_other
        finally:
            srv.stop()


def test_invalidation_suppresses_decision(tmp_path, monkeypatch):
    import herdr.workflow_docs as wd
    from herdr.experience.collector import ExperienceCollector
    from herdr import state_db

    docs = tmp_path / "docs"
    monkeypatch.setenv("HERDR_WORKFLOW_DOCS_DIR", str(docs))
    wd.append_note("wf-r6", kind="decision", title="报价八五折", body="x",
                   node="quote_calc", task_id="t-r6",
                   fields={"decision_id": "d9", "decision_status": "resolved",
                           "decision": "报价超过一万元打八五折"})
    wd.append_note("wf-r6", kind="invalidation", title="撤销报价口径", body="x",
                   node="quote_calc", invalidates=["quote_calc"])
    sdb = tmp_path / "s.db"
    cdb = tmp_path / "c.db"
    state_db.init_db(sdb)
    state_db.save_task({"task_id": "t-r6", "run_id": "r-r6", "workflow_id": "wf-r6",
                        "customer": "acme", "task_type": "quote", "status": "completed"}, db_path=sdb)
    state_db.save_steer({"steer_id": "s-r6", "task_id": "t-r6",
                         "instruction": "请按最新有效口径处理报价事宜",
                         "operator": "human", "urgent": True, "status": "dispatched"}, db_path=sdb)
    col = ExperienceCollector(db_path=cdb, hub_client=None, contributor="u")
    out = {"outcome_id": "o-r6", "run_id": "r-r6", "qualified_success": True,
           "verification_passed": True, "final_status": "completed",
           "recorded_at": time.time()}
    res = col.process_task_settlement("t-r6", outcome=out, state_db_path=sdb)
    assert res["status"] in ("no_candidate", "pending_upload")
    if res["status"] == "pending_upload":
        from herdr.experience.collector import _get_collector_db
        conn = _get_collector_db(cdb)
        try:
            row = conn.execute("SELECT candidate_json FROM local_collector_records WHERE task_id='t-r6'").fetchone()
            assert "八五折" not in (row["candidate_json"] or "")
        finally:
            conn.close()


def test_fold_keeps_newest_per_decision():
    notes = [
        {"decision_id": "d1", "decision_status": "resolved", "decision": "旧口径",
         "note_id": "n1", "ts": 100.0},
        {"decision_id": "d1", "decision_status": "reopened", "decision": "重议中",
         "note_id": "n2", "ts": 200.0},
        {"note_id": "n3", "ts": 50.0},
    ]
    folded = fold_decisions_by_id(notes)
    by_id = {n.get("note_id") for n in folded}
    assert by_id == {"n2", "n3"}
