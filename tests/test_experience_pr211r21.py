"""PR #211 R21: pre-allocation mandatory gate; durable state-db identity; SQL scope filter."""

import importlib.machinery
import importlib.util
import json
import re
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch


def _load_task_cli():
    path = str(Path(__file__).resolve().parent.parent / "bin" / "herdr-task")
    loader = importlib.machinery.SourceFileLoader("herdr_task_cli_r21", path)
    spec = importlib.util.spec_from_loader("herdr_task_cli_r21", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class _Args:
    def __init__(self, **kw):
        self.task_id = "t-r21"
        self.customer = "acme"
        self.business_task_type = "quote"
        self.task_type = "feature"
        self.mandatory_experience_rules = True
        self.__dict__.update(kw)


def test_gate_blocks_when_hub_unreachable():
    mod = _load_task_cli()
    with patch.dict("os.environ", {"HERDR_HUB_URL": "http://127.0.0.1:59999",
                                   "HERDR_HUB_TOKEN": "x"}):
        import pytest
        with pytest.raises(RuntimeError):
            mod._mandatory_experience_gate(_Args(), "quote")


def test_gate_is_noop_for_non_mandatory():
    mod = _load_task_cli()
    with patch.dict("os.environ", {"HERDR_HUB_URL": "http://127.0.0.1:59999",
                                   "HERDR_HUB_TOKEN": "x"}):
        assert mod._mandatory_experience_gate(
            _Args(mandatory_experience_rules=False), "quote") is None


def test_gate_passes_with_published_rule(tmp_path):
    from herdr.experience.client import ExperienceClient
    from herdr.experience.hub_server import ExperienceHubServer
    from herdr.experience.models import AppliesTo, BusinessRuleExperience, ExperienceSource

    srv = ExperienceHubServer(host="127.0.0.1", port=0, db_path=tmp_path / "h.db")
    srv.storage.register_token("adm", user_id="a", role="admin", allowed_customers=["*"])
    srv.start(block=False)
    time.sleep(0.05)
    try:
        admin = ExperienceClient(base_url=f"http://127.0.0.1:{srv.port}", token="adm")
        exp = BusinessRuleExperience(
            experience_id="r21-1", title="t",
            content="Quotation discount above 20 percent requires regional approval always",
            contributor="a", applies_to=AppliesTo(customer="acme", task_type="quote"),
            source=ExperienceSource(task_id="t", run_id="r"))
        admin.submit_candidate(exp)
        admin.review_candidate("r21-1", action="approve")
        mod = _load_task_cli()
        with patch.dict("os.environ", {"HERDR_HUB_URL": f"http://127.0.0.1:{srv.port}",
                                       "HERDR_HUB_TOKEN": "adm"}):
            mod._mandatory_experience_gate(_Args(), "quote")
    finally:
        srv.stop()


def test_gate_runs_before_resource_allocation():
    """The fail-closed gate must precede clone/pane allocation, not dispatch."""
    src = (Path(__file__).resolve().parent.parent / "bin" / "herdr-task").read_text()
    start = src.index("def _launch_task(args):")
    body = src[start:src.index("\ndef ", start + 10)]
    gate = body.index("_mandatory_experience_gate(args, node_id)")
    allocate = body.index("worker_cmd = [")
    register = body.index("store.save_task(task)")
    dispatch = body.index("    dispatch_task(")
    assert gate < allocate, "gate must run before the worker allocates clone/pane"
    assert gate < register, "gate must run before the task is registered"
    assert gate < dispatch


def test_recovery_uses_persisted_state_db(tmp_path):
    from herdr import state_db
    from herdr.experience.collector import ExperienceCollector, _get_collector_db

    sdb, cdb = tmp_path / "side.db", tmp_path / "c.db"
    state_db.init_db(sdb)
    state_db.save_task({"task_id": "t-r21d", "run_id": "r-r21d", "workflow_id": "wf",
                        "customer": "acme", "task_type": "quote", "status": "completed"},
                       db_path=sdb)
    state_db.save_steer({"steer_id": "s-r21d", "task_id": "t-r21d",
                         "instruction": "有效报价口径请长期遵循执行下去",
                         "operator": "human", "urgent": True, "status": "dispatched"}, db_path=sdb)

    class FailHub:
        def submit_candidate(self, exp):
            return {"ok": False, "error": "down", "message": "down"}

    col = ExperienceCollector(db_path=cdb, hub_client=FailHub(), contributor="u")
    out = {"outcome_id": "o-r21d", "run_id": "r-r21d", "qualified_success": True,
           "human_intervention_count": 1, "requirements_satisfied": True,
           "verification_passed": True, "final_status": "completed"}
    col.enqueue_task_settlement("t-r21d", out, state_db_path=sdb)
    conn = _get_collector_db(cdb)
    try:
        row = conn.execute("SELECT state_db_path FROM local_collector_records "
                           "WHERE task_id='t-r21d'").fetchone()
    finally:
        conn.close()
    assert row["state_db_path"], "durable row must record its state database"

    # Recovery without an explicit path (Sentinel trigger_background_sync).
    stats = col.process_pending()
    assert stats["failed"] == 0 and stats["processed"] == 1, stats
    conn = _get_collector_db(cdb)
    try:
        row = conn.execute("SELECT status, experience_id FROM local_collector_records "
                           "WHERE task_id='t-r21d'").fetchone()
    finally:
        conn.close()
    assert row["status"] == "pending_upload", "row must not be quarantined as task_not_found"


def test_restricted_scope_all_filters_in_sql(tmp_path):
    from herdr.experience.client import ExperienceClient
    from herdr.experience.hub_server import ExperienceHubServer
    from herdr.experience.models import AppliesTo, BusinessRuleExperience, ExperienceSource

    srv = ExperienceHubServer(host="127.0.0.1", port=0, db_path=tmp_path / "h.db")
    srv.storage.register_token("adm", user_id="a", role="admin", allowed_customers=["*"])
    srv.storage.register_token("limited", user_id="u", role="contributor",
                               allowed_customers=["acme"])
    srv.start(block=False)
    time.sleep(0.05)
    try:
        admin = ExperienceClient(base_url=f"http://127.0.0.1:{srv.port}", token="adm")
        for eid, cust in (("r21-acme", "acme"), ("r21-other", "globex")):
            exp = BusinessRuleExperience(
                experience_id=eid, title="t",
                content=f"Rule for {cust} customers that must always be respected here",
                contributor="a", applies_to=AppliesTo(customer=cust, task_type="quote"),
                source=ExperienceSource(task_id="t", run_id="r"))
            admin.submit_candidate(exp)
            admin.review_candidate(eid, action="approve")

        statements = []
        original = srv.storage._get_conn

        def traced():
            conn = original()
            conn.set_trace_callback(statements.append)
            return conn

        srv.storage._get_conn = traced
        rules = ExperienceClient(base_url=f"http://127.0.0.1:{srv.port}",
                                 token="limited").query_rules(scope="all")
        assert [r.experience_id for r in rules] == ["r21-acme"]
        selects = [s for s in statements if "FROM experiences" in s and "SELECT" in s]
        assert selects, "no experience query captured"
        # Bound values are inlined in trace output, so an out-of-scope customer
        # appearing anywhere proves the narrowing did not happen in SQL.
        assert any("customer IN (" in s and "acme" in s for s in selects), (
            "customer scope must be narrowed by the SQL query, not post-filtered")
        assert not any("globex" in s for s in selects), (
            "out-of-scope customer must never reach the query for a restricted token")
    finally:
        srv.stop()


def test_admin_scope_all_without_allowed_still_returns_all(tmp_path):
    from herdr.experience.hub_server import ExperienceHubServer
    from herdr.experience.models import AppliesTo, BusinessRuleExperience, ExperienceSource

    srv = ExperienceHubServer(host="127.0.0.1", port=0, db_path=tmp_path / "h.db")
    srv.start(block=False)
    time.sleep(0.05)
    try:
        for eid, cust in (("r21-a", "acme"), ("r21-b", "globex")):
            exp = BusinessRuleExperience(
                experience_id=eid, title="t",
                content=f"Rule for {cust} customers that must always be respected here",
                contributor="a", applies_to=AppliesTo(customer=cust, task_type="quote"),
                source=ExperienceSource(task_id="t", run_id="r"))
            srv.storage.save_candidate(exp, actor="a")
            srv.storage.review_candidate(eid, "approve", reviewer="a")
        rules = srv.storage.query_published_rules(scope_all=True)
        assert {r.experience_id for r in rules} == {"r21-a", "r21-b"}
        narrowed = srv.storage.query_published_rules(scope_all=True, allowed_customers=["acme"])
        assert {r.experience_id for r in narrowed} == {"r21-a"}
    finally:
        srv.stop()