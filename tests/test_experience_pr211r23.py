"""PR #211 R23: per-row database identity wins; scoped terminal failure; prefetch refs."""

import importlib.machinery
import importlib.util
import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch


def _load_task_cli():
    path = str(Path(__file__).resolve().parent.parent / "bin" / "herdr-task")
    loader = importlib.machinery.SourceFileLoader("herdr_task_cli_r23", path)
    spec = importlib.util.spec_from_loader("herdr_task_cli_r23", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class _FailHub:
    def submit_candidate(self, exp):
        return {"ok": False, "error": "down", "message": "down"}


def _seed_state(db, task_id, run_id):
    from herdr import state_db

    state_db.save_task({"task_id": task_id, "run_id": run_id, "workflow_id": "wf",
                        "customer": "acme", "task_type": "quote", "status": "completed"},
                       db_path=db)
    state_db.save_steer({"steer_id": f"s-{task_id}", "task_id": task_id,
                         "instruction": "有效报价口径请长期遵循执行下去",
                         "operator": "human", "urgent": True, "status": "dispatched"}, db_path=db)


def _row(db, task_id):
    from herdr.experience.collector import _get_collector_db

    conn = _get_collector_db(db)
    try:
        return conn.execute("SELECT status, experience_id, last_error FROM local_collector_records "
                            "WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()


def test_row_database_wins_over_caller_path(tmp_path):
    """Recovery in A must not read B's rows from A (Codex R23 P1)."""
    from herdr import state_db
    from herdr.experience.collector import ExperienceCollector

    db_a, db_b, cdb = tmp_path / "a.db", tmp_path / "b.db", tmp_path / "c.db"
    state_db.init_db(db_a)
    state_db.init_db(db_b)
    _seed_state(db_a, "t-a", "r-a")
    _seed_state(db_b, "t-b", "r-b")

    col = ExperienceCollector(db_path=cdb, hub_client=_FailHub(), contributor="u")
    out_a = {"outcome_id": "o-a", "run_id": "r-a", "qualified_success": True,
             "human_intervention_count": 1, "requirements_satisfied": True,
             "verification_passed": True, "final_status": "completed"}
    out_b = dict(out_a, outcome_id="o-b", run_id="r-b")
    col.enqueue_task_settlement("t-a", out_a, state_db_path=db_a)
    col.enqueue_task_settlement("t-b", out_b, state_db_path=db_b)

    # An async worker for A must leave B's row intact.
    stats = col.process_pending(state_db_path=db_a)
    assert stats["failed"] == 0, stats
    assert _row(cdb, "t-a")["status"] == "pending_upload"
    assert _row(cdb, "t-b")["status"] == "pending_upload", (
        f"B's row was processed against A: {_row(cdb, 't-b')}")


def test_terminal_failure_is_scoped_to_run(tmp_path):
    """Two databases share a task id; the row whose source task is missing fails alone."""
    from herdr import state_db
    from herdr.experience.collector import ExperienceCollector

    db_a, db_b, cdb = tmp_path / "a.db", tmp_path / "b.db", tmp_path / "c.db"
    state_db.init_db(db_a)
    state_db.init_db(db_b)
    _seed_state(db_a, "t-shared", "r-present")

    col = ExperienceCollector(db_path=cdb, hub_client=_FailHub(), contributor="u")
    base = {"qualified_success": True, "human_intervention_count": 1,
            "requirements_satisfied": True, "verification_passed": True,
            "final_status": "completed"}
    col.enqueue_task_settlement("t-shared", dict(base, outcome_id="o-1", run_id="r-present"),
                                state_db_path=db_a)
    # Same task id, but database B does not contain that task at all.
    col.enqueue_task_settlement("t-shared", dict(base, outcome_id="o-2", run_id="r-absent"),
                                state_db_path=db_b)

    col.process_pending(state_db_path=db_a)
    conn = sqlite3.connect(str(cdb))
    conn.row_factory = sqlite3.Row
    try:
        rows = {r["run_id"]: r["status"] for r in conn.execute(
            "SELECT run_id, status FROM local_collector_records WHERE task_id='t-shared'")}
    finally:
        conn.close()
    assert rows["r-present"] == "pending_upload", rows
    assert rows["r-absent"] == "error", rows


def test_prefetched_rules_are_recorded_on_task(tmp_path):
    """The prefetch path must still write applied_experience_rules metadata."""
    from herdr import state_db

    mod = _load_task_cli()
    sdb = tmp_path / "s.db"
    state_db.init_db(sdb)
    state_db.save_task({"task_id": "t-p", "run_id": "r-p", "workflow_id": "wf",
                        "customer": "acme", "business_task_type": "quote",
                        "mandatory_experience_rules": True, "status": "pending",
                        "goal": "g", "clone_path": str(tmp_path)}, db_path=sdb)

    # _get_store() binds db_path at import time, so the CLI must be loaded
    # with HERDR_STATE_DB already pointing at this test's database.
    os.environ["HERDR_STATE_DB"] = str(sdb)
    try:
        mod = _load_task_cli()
    finally:
        os.environ.pop("HERDR_STATE_DB", None)

    delivered = {}

    def fake_deliver(_task, _store, _phase, _action, prompt, _send, **kw):
        delivered["prompt"] = prompt
        conn = state_db.get_db_connection(sdb)
        try:
            kw["prepare_task"](conn)
        finally:
            conn.close()

    from herdr import supervisor_delivery

    def boom(task, store=None, client=None):
        raise AssertionError("dispatch must not re-query the Hub")

    refs = [{"experience_id": "r23-1", "version": 3, "title": "Regional approval required"}]
    with patch.dict("os.environ", {"HERDR_STATE_DB": str(sdb)}):
        with patch.object(mod, "_fetch_experience_rules_block", boom):
            with patch.object(supervisor_delivery, "deliver", fake_deliver):
                mod.dispatch_task("t-p", "do work",
                                  prefetched_experience_suffix="\n\nRULE BLOCK\n",
                                  prefetched_experience_refs=refs)
    assert "RULE BLOCK" in delivered["prompt"]
    task = state_db.get_task("t-p", db_path=sdb)
    # update_task_metadata writes into the task payload, which is where the
    # non-prefetch path records attribution too.
    assert task.get("applied_experience_rules") == refs, task.get("applied_experience_rules")


def test_gate_returns_rule_references(tmp_path):
    from herdr.experience.client import ExperienceClient
    from herdr.experience.hub_server import ExperienceHubServer
    from herdr.experience.models import AppliesTo, BusinessRuleExperience, ExperienceSource

    mod = _load_task_cli()
    srv = ExperienceHubServer(host="127.0.0.1", port=0, db_path=tmp_path / "h.db")
    srv.storage.register_token("adm", user_id="a", role="admin", allowed_customers=["*"])
    srv.start(block=False)
    time.sleep(0.05)
    try:
        admin = ExperienceClient(base_url=f"http://127.0.0.1:{srv.port}", token="adm")
        exp = BusinessRuleExperience(
            experience_id="r23-g", title="Regional approval required",
            content="Quotation discount above 20 percent requires regional approval always",
            contributor="a", applies_to=AppliesTo(customer="acme", task_type="quote"),
            source=ExperienceSource(task_id="t", run_id="r"))
        admin.submit_candidate(exp)
        admin.review_candidate("r23-g", action="approve")

        class Args:
            task_id = "t-r23g"
            customer = "acme"
            business_task_type = "quote"
            task_type = "feature"
            mandatory_experience_rules = True

        with patch.dict("os.environ", {"HERDR_HUB_URL": f"http://127.0.0.1:{srv.port}",
                                       "HERDR_HUB_TOKEN": "adm"}):
            block, refs = mod._mandatory_experience_gate(Args(), "quote")
        assert "r23-g" in block
        assert [r["experience_id"] for r in refs] == ["r23-g"]
        assert refs[0]["version"] >= 1
    finally:
        srv.stop()