"""PR #211 R22: unscoped-record authorization; reuse pre-allocation rules."""

import importlib.machinery
import importlib.util
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def _load_task_cli():
    path = str(Path(__file__).resolve().parent.parent / "bin" / "herdr-task")
    loader = importlib.machinery.SourceFileLoader("herdr_task_cli_r22", path)
    spec = importlib.util.spec_from_loader("herdr_task_cli_r22", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _call(base, method, path, token, body=None):
    data = json.dumps(body or {}).encode()
    req = Request(base + path, data=data, method=method,
                  headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except Exception:
            return exc.code, {}


def _hub_with_tokens(tmp_path, token_specs):
    from herdr.experience.hub_server import ExperienceHubServer

    srv = ExperienceHubServer(host="127.0.0.1", port=0, db_path=tmp_path / "h.db")
    for token, (uid, role, customers) in token_specs.items():
        srv.storage.register_token(token, user_id=uid, role=role, allowed_customers=customers)
    srv.start(block=False)
    time.sleep(0.05)
    return srv, f"http://127.0.0.1:{srv.port}"


def _seed_unscoped(srv, experience_id):
    from herdr.experience.models import AppliesTo, BusinessRuleExperience, ExperienceSource

    exp = BusinessRuleExperience(
        experience_id=experience_id, title="t",
        content=f"Rule {experience_id} carries no customer scope and must always be respected",
        contributor="a", applies_to=AppliesTo(customer=None, task_type="quote"),
        source=ExperienceSource(task_id="t", run_id="r"))
    srv.storage.save_candidate(exp, actor="a")
    return exp


def test_restricted_cannot_reject_unscoped_record(tmp_path):
    srv, base = _hub_with_tokens(tmp_path, {"adm": ("a", "admin", ["*"]),
                                            "acme": ("u", "reviewer", ["acme"])})
    try:
        _seed_unscoped(srv, "r22-a")
        status, body = _call(base, "POST", "/api/v1/experiences/r22-a/review", "acme",
                             {"action": "reject"})
        assert status == 403, (status, body)
        assert srv.storage.get_experience("r22-a").status != "rejected"
    finally:
        srv.stop()


def test_restricted_cannot_revoke_or_read_unscoped_record(tmp_path):
    srv, base = _hub_with_tokens(tmp_path, {"adm": ("a", "admin", ["*"]),
                                            "acme": ("u", "reviewer", ["acme"])})
    try:
        _seed_unscoped(srv, "r22-b")
        # Unscoped records can never reach published (storage rejects approve),
        # so this pins authorization: 403 must be decided before any state check.
        status, body = _call(base, "POST", "/api/v1/experiences/r22-b/revoke", "acme", {"reason": "x"})
        assert status == 403, (status, body)
        assert srv.storage.get_experience("r22-b").status == "pending_review"
        status, _ = _call(base, "GET", "/api/v1/experiences/r22-b", "acme")
        assert status == 403, status
    finally:
        srv.stop()


def test_restricted_cannot_submit_unscoped_candidate(tmp_path):
    srv, base = _hub_with_tokens(tmp_path, {"acme": ("c", "contributor", ["acme"])})
    try:
        status, body = _call(base, "POST", "/api/v1/experiences/candidates", "acme", {
            "experience_id": "r22-c", "type": "business_rule", "title": "t",
            "content": "Unscoped candidate from a restricted contributor, must be refused here",
            "applies_to": {"customer": None, "task_type": "quote"},
            "source": {"task_id": "t", "run_id": "r"}})
        assert status == 403, (status, body)
        assert srv.storage.get_experience("r22-c") is None
    finally:
        srv.stop()


def test_wildcard_token_keeps_full_authority(tmp_path):
    srv, base = _hub_with_tokens(tmp_path, {"adm": ("a", "admin", ["*"])})
    try:
        _seed_unscoped(srv, "r22-d")
        status, _ = _call(base, "GET", "/api/v1/experiences/r22-d", "adm")
        assert status == 200, status

        from herdr.experience.models import AppliesTo, BusinessRuleExperience, ExperienceSource

        exp = BusinessRuleExperience(
            experience_id="r22-d2", title="t",
            content="Published rule for acme that only a wildcard token may revoke here",
            contributor="a", applies_to=AppliesTo(customer="acme", task_type="quote"),
            source=ExperienceSource(task_id="t", run_id="r"))
        srv.storage.save_candidate(exp, actor="a")
        srv.storage.review_candidate("r22-d2", "approve", reviewer="a")
        assert srv.storage.get_experience("r22-d2").status == "published"
        status, body = _call(base, "POST", "/api/v1/experiences/r22-d2/revoke", "adm", {"reason": "x"})
        assert status == 200, (status, body)
        assert srv.storage.get_experience("r22-d2").status == "revoked"
    finally:
        srv.stop()


def test_scoped_token_still_manages_own_customer(tmp_path):
    srv, base = _hub_with_tokens(tmp_path, {"acme": ("u", "reviewer", ["acme"])})
    try:
        from herdr.experience.models import AppliesTo, BusinessRuleExperience, ExperienceSource

        exp = BusinessRuleExperience(
            experience_id="r22-e", title="t",
            content="Rule scoped to acme that this reviewer is authorized to reject always",
            contributor="a", applies_to=AppliesTo(customer="acme", task_type="quote"),
            source=ExperienceSource(task_id="t", run_id="r"))
        srv.storage.save_candidate(exp, actor="a")
        status, body = _call(base, "POST", "/api/v1/experiences/r22-e/review", "acme",
                             {"action": "reject"})
        assert status == 200, (status, body)
        assert srv.storage.get_experience("r22-e").status == "rejected"
    finally:
        srv.stop()


def test_dispatch_reuses_preallocated_rules(tmp_path):
    """A Hub outage between the gate and dispatch must not strand the launch."""
    from herdr import state_db

    mod = _load_task_cli()
    sdb = tmp_path / "s.db"
    state_db.init_db(sdb)
    state_db.save_task({"task_id": "t-r22", "run_id": "r-r22", "workflow_id": "wf",
                        "customer": "acme", "business_task_type": "quote",
                        "mandatory_experience_rules": True, "status": "pending",
                        "goal": "g", "clone_path": str(tmp_path)}, db_path=sdb)

    fetched = []

    def boom(task, store=None, client=None):
        fetched.append(1)
        raise RuntimeError("hub down at dispatch time")

    delivered = {}

    def fake_deliver(_task, _store, _phase, _action, prompt, _send, **kw):
        delivered["prompt"] = prompt
        # deliver() owns the status transition via this callback; without it
        # the task would stay pending and the test would pass vacuously.
        conn = state_db.get_db_connection(sdb)
        try:
            kw["prepare_task"](conn)
        finally:
            conn.close()

    from herdr import supervisor_delivery

    with patch.dict("os.environ", {"HERDR_STATE_DB": str(sdb)}):
        with patch.object(mod, "_fetch_experience_rules_block", boom):
            with patch.object(supervisor_delivery, "deliver", fake_deliver):
                mod.dispatch_task("t-r22", "do work",
                                  prefetched_experience_suffix="\n\nRULE BLOCK\n")
    assert fetched == [], "dispatch must not re-query the Hub when a block was prefetched"
    assert "RULE BLOCK" in delivered["prompt"]
    assert state_db.get_task("t-r22", db_path=sdb)["status"] == "dispatched"


def test_dispatch_without_prefetch_still_fails_closed(tmp_path):
    from herdr import state_db

    mod = _load_task_cli()
    sdb = tmp_path / "s.db"
    state_db.init_db(sdb)
    state_db.save_task({"task_id": "t-r22b", "run_id": "r-r22b", "workflow_id": "wf",
                        "customer": "acme", "business_task_type": "quote",
                        "mandatory_experience_rules": True, "status": "pending",
                        "goal": "g", "clone_path": str(tmp_path)}, db_path=sdb)

    def boom(task, store=None, client=None):
        raise RuntimeError("hub down")

    import pytest

    with patch.dict("os.environ", {"HERDR_STATE_DB": str(sdb)}):
        with patch.object(mod, "_fetch_experience_rules_block", boom):
            with pytest.raises(RuntimeError):
                mod.dispatch_task("t-r22b", "do work")
    assert state_db.get_task("t-r22b", db_path=sdb)["status"] == "pending"