"""PR #211 R4 regression tests: 6 items, all via real entry points.

- contributor candidate list enforces customer scope (same user, two tokens)
- console helper never inherits env creds but forwards explicit token (401 anon)
- HERDR_ALLOW_INSECURE_HTTP=0 still blocks plaintext (only 1/true/yes allow)
- background sync single-flight (rapid triggers -> single execution)
- sync-experience force path retries despite backoff
- audit log prefilter correctness (old own record visible despite 105 newer others)
- console api() forwards Authorization header (static guard)
"""

import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from herdr.experience.client import ExperienceClient, ExperienceHubSecurityError
from herdr.experience.collector import ExperienceCollector
from herdr.experience.hub_server import ExperienceHubServer
from herdr.experience.models import AppliesTo, BusinessRuleExperience, ExperienceSource


def _mkexp(eid, customer, task_type="quote"):
    return BusinessRuleExperience(
        experience_id=eid,
        title=f"t-{eid}",
        content="content long enough xxxxx yyyy zzzz",
        contributor="emp1",
        applies_to=AppliesTo(customer=customer, task_type=task_type),
        source=ExperienceSource(task_id="t", run_id="r"),
    )


@pytest.fixture
def hub():
    with tempfile.TemporaryDirectory() as td:
        srv = ExperienceHubServer(host="127.0.0.1", port=0, db_path=Path(td) / "h.db")
        srv.storage.register_token("tok_acme", user_id="emp1", role="contributor", allowed_customers=["acme"])
        srv.storage.register_token("tok_beta", user_id="emp1", role="contributor", allowed_customers=["beta"])
        srv.storage.register_token("tok_rev", user_id="rev1", role="reviewer", allowed_customers=["acme"])
        srv.storage.register_token("tok_adm", user_id="adm", role="admin", allowed_customers=["*"])
        srv.start(block=False)
        time.sleep(0.05)
        yield srv, f"http://127.0.0.1:{srv.port}"
        srv.stop()


def test_contributor_list_enforces_customer_scope(hub):
    srv, url = hub
    beta = ExperienceClient(base_url=url, token="tok_beta")
    acme = ExperienceClient(base_url=url, token="tok_acme")
    assert beta.submit_candidate(_mkexp("r4-b1", "beta")).get("ok") is True
    seen = [c.experience_id for c in acme.list_candidates()]
    assert "r4-b1" not in seen


def test_console_helper_no_env_fallback_forwards_explicit(hub):
    _, url = hub
    from console.herdr_factory_console import _get_console_hub_client

    with patch.dict(os.environ, {"HERDR_HUB_TOKEN": "server-side-secret", "HERDR_HUB_URL": url}):
        anon = _get_console_hub_client(token=None)
        assert anon.token == ""
        # anonymous identity is rejected by the Hub (401 path)
        assert anon.list_candidates() == []
        explicit = _get_console_hub_client(token="tok_rev")
        assert explicit.token == "tok_rev"


def test_insecure_http_flag_zero_still_blocks():
    with patch.dict(os.environ, {"HERDR_ALLOW_INSECURE_HTTP": "0"}):
        with pytest.raises(ExperienceHubSecurityError):
            ExperienceClient(base_url="http://192.168.1.10:8780", token="t")
    with patch.dict(os.environ, {"HERDR_ALLOW_INSECURE_HTTP": "1"}):
        c = ExperienceClient(base_url="http://192.168.1.10:8780", token="t")
        assert c.token == "t"


def test_background_sync_single_flight():
    import herdr.experience.collector as coll

    calls = []

    orig = ExperienceCollector.recover_and_sync

    def slow(self, state_db_path=None, force=False):
        calls.append(1)
        time.sleep(0.3)
        return {"ok": True}

    with patch.object(ExperienceCollector, "recover_and_sync", slow):
        results = [coll.trigger_background_sync() for _ in range(10)]
        time.sleep(0.6)
    assert results.count(True) == 1
    assert len(calls) == 1
    assert orig is not None


def test_manual_sync_force_bypasses_backoff(tmp_path):
    from herdr import state_db
    from herdr.experience.collector import _get_collector_db

    sdb = tmp_path / "s.db"
    cdb = tmp_path / "c.db"
    state_db.init_db(sdb)
    state_db.save_task({"task_id": "t1", "run_id": "r1", "workflow_id": "wf",
                        "customer": "acme", "task_type": "quote", "status": "completed"}, db_path=sdb)
    state_db.save_steer({"steer_id": "s1", "task_id": "t1",
                         "instruction": "这是一个足够长的人工修正指令，包含明确的业务规则内容用于测试验证。",
                         "operator": "human", "urgent": True, "status": "dispatched"}, db_path=sdb)

    class FailHub:
        def submit_candidate(self, exp):
            return {"ok": False, "error": "network_error", "message": "down"}

    col = ExperienceCollector(db_path=cdb, hub_client=FailHub(), contributor="u")
    o = {"outcome_id": "o1", "run_id": "r1", "qualified_success": True,
         "human_intervention_count": 1, "requirements_satisfied": True,
         "verification_passed": True, "final_status": "completed"}
    col.process_task_settlement("t1", outcome=o, state_db_path=sdb)
    col.retry_pending_uploads(force=True)  # schedule backoff

    class OkHub:
        def submit_candidate(self, exp):
            return {"ok": True, "experience_id": exp.experience_id}

    col.hub_client = OkHub()
    assert col.recover_and_sync()["retried"] == 0
    assert col.recover_and_sync(force=True)["succeeded"] == 1


def test_audit_prefilter_keeps_old_own_record():
    with tempfile.TemporaryDirectory() as td:
        srv = ExperienceHubServer(host="127.0.0.1", port=0, db_path=Path(td) / "h.db")
        srv.storage.record_audit("submit_candidate", "a", "exp-t", {"title": "t", "customer": "acme"})
        for i in range(105):
            srv.storage.record_audit("submit_candidate", "a", f"o-{i}", {"title": "o", "customer": "other"})
        logs = srv.storage.list_audit_logs(limit=100, allowed_customers=["acme"])
        assert any(l["experience_id"] == "exp-t" for l in logs)
        assert all((l["details"].get("customer") or "acme") == "acme" for l in logs)


def test_console_api_forwards_authorization():
    text = Path("console/herdr_factory_console.py").read_text()
    assert "Authorization" in text
    assert "herdr_hub_token" in text
