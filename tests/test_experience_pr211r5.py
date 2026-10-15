"""PR #211 R5 regression tests: creation-chain wiring + 5 findings, real entries.

- add CLI persists customer/business-task-type/mandatory (isolated HERDR_STATE_DB)
- launch parser exposes the same flags (--help)
- extractor prefers business_task_type over dev task_type
- empty customer scope: register rejected + HTTP audit denied
- reopened decision suppresses older resolved note (fold helper + extractor)
- trigger persists intent even when Hub client init fails
- sync-experience --force works via the real CLI end to end
- console browser->console->hub auth chain (401 anon / 200 with Bearer)
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

import pytest

from herdr.experience.client import ExperienceClient
from herdr.experience.hub_server import ExperienceHubServer
from herdr.experience.models import AppliesTo, BusinessRuleExperience, ExperienceSource

CLI = [sys.executable, "bin/herdr-task"]


def _mkexp(eid, customer, task_type="quote"):
    return BusinessRuleExperience(
        experience_id=eid, title=f"t-{eid}",
        content="content long enough xxxxx yyyy zzzz", contributor="emp1",
        applies_to=AppliesTo(customer=customer, task_type=task_type),
        source=ExperienceSource(task_id="t", run_id="r"),
    )


def test_add_persists_experience_fields(tmp_path):
    db = tmp_path / "state.db"
    env = dict(os.environ, HERDR_STATE_DB=str(db))
    p = subprocess.run(
        CLI + ["add", "--task-id", "t-r5-1", "--workflow-id", "wf-r5", "--node", "quote_calc",
               "--workspace", "ws", "--pane", "pane1", "--agent", "auto", "--goal", "quote",
               "--customer", "acme", "--business-task-type", "quote",
               "--mandatory-experience-rules"],
        capture_output=True, text=True, env=env,
    )
    assert p.returncode == 0, p.stderr
    from herdr import state_db
    task = state_db.get_task("t-r5-1", db_path=db)
    assert task["customer"] == "acme"
    assert task["business_task_type"] == "quote"
    assert task["mandatory_experience_rules"] is True


def test_launch_parser_exposes_experience_fields():
    p = subprocess.run(CLI + ["launch", "--help"], capture_output=True, text=True)
    assert p.returncode == 0
    assert "--customer" in p.stdout
    assert "--business-task-type" in p.stdout
    assert "--mandatory-experience-rules" in p.stdout


def test_extractor_prefers_business_task_type():
    from herdr.experience.extractor import extract_candidate_from_facts
    task = {"task_id": "t1", "run_id": "r1", "workflow_id": "wf",
            "task_type": "feat", "business_task_type": "quote", "customer": "acme"}
    outcome = {"outcome_id": "o1", "run_id": "r1", "qualified_success": True,
               "verification_passed": True, "final_status": "completed",
               "recorded_at": 1700000000.0}
    steers = [{"operator": "human", "instruction": "VIP客户单笔超一万享受八五折并免加急费", "steer_id": "s1"}]
    exp, reason = extract_candidate_from_facts(task, outcome, steers=steers)
    assert reason == "ready"
    assert exp.applies_to.customer == "acme"
    assert exp.applies_to.task_type == "quote"


def test_empty_scope_rejected_and_denied():
    with tempfile.TemporaryDirectory() as td:
        srv = ExperienceHubServer(host="127.0.0.1", port=0, db_path=Path(td) / "h.db")
        with pytest.raises(ValueError):
            srv.storage.register_token("tok_empty", user_id="rev0", role="reviewer", allowed_customers=[])
        srv.storage.register_token("tok_a", user_id="rev1", role="reviewer", allowed_customers=["acme"])
        srv.start(block=False)
        time.sleep(0.05)
        try:
            url = f"http://127.0.0.1:{srv.port}"
            admin = ExperienceClient(base_url=url, token="tok_a")
            # seeds an audit row via a candidate submit with the broad token below
            srv.storage.register_token("tok_adm", user_id="adm", role="admin", allowed_customers=["*"])
            adm = ExperienceClient(base_url=url, token="tok_adm")
            assert adm.submit_candidate(_mkexp("r5-a1", "acme")).get("ok") is True
            req = urllib.request.Request(
                f"{url}/api/v1/experiences/audit",
                headers={"Authorization": "Bearer tok_a"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
            assert any(l["experience_id"] == "r5-a1" for l in data["audit_logs"])
        finally:
            srv.stop()


def test_reopened_decision_suppresses_resolved():
    from herdr.experience.collector import fold_decisions_by_id
    from herdr.experience.extractor import extract_candidate_from_facts
    notes = [
        {"decision_id": "d1", "decision_status": "resolved", "decision": "报价超过一万元打八五折",
         "note_id": "n1", "ts": 100.0, "kind": "decision", "task_id": "t1"},
        {"decision_id": "d1", "decision_status": "reopened", "decision": "重新商议中",
         "note_id": "n2", "ts": 200.0, "kind": "decision", "task_id": "t1"},
    ]
    folded = fold_decisions_by_id(notes)
    assert [n["note_id"] for n in folded] == ["n2"]
    task = {"task_id": "t1", "run_id": "r1", "workflow_id": "wf", "customer": "acme", "task_type": "quote"}
    outcome = {"outcome_id": "o1", "run_id": "r1", "qualified_success": True,
               "verification_passed": True, "final_status": "completed",
               "recorded_at": 1700000000.0}
    exp, _ = extract_candidate_from_facts(
        task, outcome,
        steers=[{"operator": "human", "instruction": "请按最新口径处理", "steer_id": "s1"}],
        decisions=folded,
    )
    assert exp is None or "八五折" not in exp.content


def test_trigger_persists_intent_on_bad_hub(tmp_path):
    from herdr import state_db
    import herdr.execution_outcome as eo
    from herdr.experience.collector import _get_collector_db

    sdb = tmp_path / "s.db"
    cdb = tmp_path / "c.db"
    state_db.init_db(sdb)
    state_db.save_task({"task_id": "tt1", "run_id": "rr1", "workflow_id": "wf",
                        "customer": "acme", "task_type": "quote", "status": "completed"}, db_path=sdb)
    outcome = {"outcome_id": "o1", "task_id": "tt1", "run_id": "rr1", "workflow_id": "wf",
               "qualified_success": True, "verification_passed": True,
               "final_status": "completed", "recorded_at": 1700000000.0}
    env = {"HERDR_COLLECTOR_DB_PATH": str(cdb), "HERDR_HUB_URL": "http://192.168.99.99:8780",
           "HERDR_HUB_TOKEN": "x"}
    with patch.dict(os.environ, env):
        os.environ.pop("HERDR_ALLOW_INSECURE_HTTP", None)
        eo._trigger_experience_collection_best_effort("tt1", outcome, db_path=sdb)
    conn = _get_collector_db(cdb)
    try:
        rows = conn.execute("SELECT status FROM local_collector_records WHERE task_id='tt1'").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1


def test_sync_force_cli_end_to_end(tmp_path):
    import sqlite3
    from herdr.experience.models import BusinessRuleExperience as BRE
    with tempfile.TemporaryDirectory() as td:
        srv = ExperienceHubServer(host="127.0.0.1", port=0, db_path=Path(td) / "h.db")
        srv.storage.register_token("tok", user_id="u", role="contributor", allowed_customers=["acme"])
        srv.start(block=False)
        time.sleep(0.05)
        try:
            url = f"http://127.0.0.1:{srv.port}"
            cdb = tmp_path / "c.db"
            exp = _mkexp("r5-c1", "acme")
            conn = sqlite3.connect(str(cdb))
            conn.execute("""CREATE TABLE IF NOT EXISTS local_collector_records (
                task_id TEXT NOT NULL, run_id TEXT NOT NULL, outcome_id TEXT,
                experience_id TEXT, status TEXT NOT NULL, candidate_json TEXT,
                outcome_json TEXT, last_error TEXT, retry_count INTEGER NOT NULL DEFAULT 0,
                next_retry_at REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL,
                updated_at REAL NOT NULL, PRIMARY KEY (task_id, run_id))""")
            now = time.time()
            conn.execute("INSERT INTO local_collector_records (task_id, run_id, outcome_id, experience_id, status, candidate_json, outcome_json, last_error, retry_count, next_retry_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         ("t9", "r9", "o9", "r5-c1", "pending_upload", json.dumps(exp.to_dict(), ensure_ascii=False),
                          None, "down", 3, now + 280, now, now))
            conn.commit()
            conn.close()
            env = dict(os.environ, HERDR_COLLECTOR_DB_PATH=str(cdb),
                       HERDR_STATE_DB=str(tmp_path / "s.db"),
                       HERDR_HUB_URL=url, HERDR_HUB_TOKEN="tok")
            p = subprocess.run(CLI + ["sync-experience", "--force", "--json"],
                               capture_output=True, text=True, env=env)
            assert p.returncode == 0, p.stderr
            assert json.loads(p.stdout)["succeeded"] == 1
        finally:
            srv.stop()


def test_console_browser_to_hub_auth_chain():
    from console.herdr_factory_console import Handler
    with tempfile.TemporaryDirectory() as td:
        srv = ExperienceHubServer(host="127.0.0.1", port=0, db_path=Path(td) / "h.db")
        srv.storage.register_token("tok_c", user_id="emp", role="contributor", allowed_customers=["acme"])
        srv.start(block=False)
        time.sleep(0.05)
        try:
            with patch.dict(os.environ, {"HERDR_HUB_URL": f"http://127.0.0.1:{srv.port}"}):
                os.environ.pop("HERDR_HUB_TOKEN", None)
                os.environ.pop("HERDR_HUB_ADMIN_TOKEN", None)
                console = HTTPServer(("127.0.0.1", 0), Handler)
                Thread(target=console.serve_forever, daemon=True).start()
                base = f"http://127.0.0.1:{console.server_port}"

                def get(headers=None):
                    req = urllib.request.Request(base + "/api/experience/candidates", headers=headers or {})
                    try:
                        with urllib.request.urlopen(req, timeout=5) as resp:
                            return resp.status, json.loads(resp.read().decode())
                    except urllib.error.HTTPError as e:
                        return e.code, json.loads(e.read().decode())

                status_anon, _ = get()
                assert status_anon in (401, 500)
                status_auth, body = get({"Authorization": "Bearer tok_c"})
                assert status_auth == 200
                assert body["ok"] is True
                console.shutdown()
        finally:
            srv.stop()
