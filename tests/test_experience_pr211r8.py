"""PR #211 R8 regression tests: Codex follow-ups on f6b7e2f (real entries)."""

from tests.support.console_source import console_source

import tempfile
import time
from pathlib import Path

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


def test_launch_inherit_reads_payload_first():
    from console.herdr_factory_console import _launch_experience_inherit
    # fix-loop style: policy rides in the payload, no supersedes record
    assert _launch_experience_inherit(
        {"customer": "acme", "business_task_type": "quote",
         "mandatory_experience_rules": True}, None) == [
        "--customer", "acme", "--business-task-type", "quote",
        "--mandatory-experience-rules",
    ]
    # replacement style: falls back to the superseded record
    assert _launch_experience_inherit(
        {"supersedes": "t-old"},
        {"customer": "beta", "mandatory_experience_rules": True}) == [
        "--customer", "beta", "--mandatory-experience-rules",
    ]
    assert _launch_experience_inherit({}, None) == []


def test_scope_all_with_customer_stays_narrowed():
    with tempfile.TemporaryDirectory() as td:
        srv = ExperienceHubServer(host="127.0.0.1", port=0, db_path=Path(td) / "h.db")
        srv.storage.register_token("tok_adm", user_id="adm", role="admin", allowed_customers=["*"])
        srv.storage.register_token("tok_a", user_id="rev", role="reviewer", allowed_customers=["acme"])
        srv.start(block=False)
        time.sleep(0.05)
        try:
            url = f"http://127.0.0.1:{srv.port}"
            adm = ExperienceClient(base_url=url, token="tok_adm")
            for eid, cust in (("g-r8", "*"), ("c-r8", "acme"), ("b-r8", "beta")):
                assert adm.submit_candidate(_mkexp(eid, cust)).get("ok") is True
                assert adm.review_candidate(eid, action="approve").get("ok") is True
            got = sorted(r.experience_id for r in adm.query_rules(customer="acme", scope="all"))
            assert got == ["c-r8", "g-r8"]
            rev = ExperienceClient(base_url=url, token="tok_a")
            got_rev = sorted(r.experience_id for r in rev.query_rules(customer="acme", scope="all"))
            assert got_rev == ["c-r8", "g-r8"]
            assert sorted(r.experience_id for r in adm.query_rules(scope="all")) == ["b-r8", "c-r8", "g-r8"]
        finally:
            srv.stop()


def test_customer_filter_is_applicable():
    text = console_source(Path(__file__).resolve().parents[1])
    assert "applyHubFilter" in text
    assert "_hubCustomerFilter" in text
