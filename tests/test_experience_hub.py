"""Tests for Experience Hub Server, Auth, RBAC, Customer Boundaries, and Review flow."""

import os
import tempfile
import time
from pathlib import Path

import pytest

from herdr.experience.client import ExperienceClient
from herdr.experience.hub_server import ExperienceHubServer, HubStorage
from herdr.experience.models import (
    AppliesTo,
    AuditEvent,
    BusinessRuleExperience,
    ExperienceSource,
    ExperienceStatus,
    ExperienceType,
)


@pytest.fixture
def hub_service():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test_hub.db"
        server = ExperienceHubServer(host="127.0.0.1", port=0, db_path=db_path)
        # Setup tokens
        server.storage.register_token("token_emp_a", user_id="emp_a", role="contributor", allowed_customers=["customer_acme"])
        server.storage.register_token("token_emp_b", user_id="emp_b", role="contributor", allowed_customers=["customer_acme", "customer_beta"])
        server.storage.register_token("token_reviewer", user_id="rev_1", role="reviewer", allowed_customers=["*"])
        server.storage.register_token("token_admin", user_id="admin_1", role="admin", allowed_customers=["*"])
        server.start(block=False)
        time.sleep(0.05)

        base_url = f"http://127.0.0.1:{server.port}"
        yield server, base_url

        server.stop()


def _make_candidate(exp_id: str, customer: str = "customer_acme", content: str = "报价必须打9折") -> BusinessRuleExperience:
    return BusinessRuleExperience(
        experience_id=exp_id,
        type=ExperienceType.BUSINESS_RULE.value,
        title=f"{customer} 报价规则",
        content=content,
        contributor="emp_a",
        source=ExperienceSource(task_id="t1", run_id="r1"),
        applies_to=AppliesTo(customer=customer, task_type="quote"),
        status=ExperienceStatus.PENDING_REVIEW.value,
    )


def test_unauthenticated_request_rejected(hub_service):
    _, base_url = hub_service
    client = ExperienceClient(base_url=base_url, token="")
    assert client.is_healthy() is True

    # Submitting candidate without token -> 401
    cand = _make_candidate("exp_unauth_1")
    res = client.submit_candidate(cand)
    assert res.get("ok") is False
    assert res.get("status_code") == 401

    # Querying rules without token -> 401
    rules = client.query_rules(customer="customer_acme")
    assert rules == []


def test_contributor_submit_candidate_and_idempotency(hub_service):
    _, base_url = hub_service
    client = ExperienceClient(base_url=base_url, token="token_emp_a")

    cand = _make_candidate("exp_sub_1", customer="customer_acme", content="特殊账期：30天付款")
    res1 = client.submit_candidate(cand)
    assert res1.get("ok") is True
    assert res1.get("experience_id") == "exp_sub_1"
    assert res1.get("reason") == "created"

    # Repeat submit -> idempotent 200 with already_exists
    res2 = client.submit_candidate(cand)
    assert res2.get("ok") is True
    assert res2.get("experience_id") == "exp_sub_1"
    assert res2.get("reason") == "already_exists"


def test_non_reviewer_cannot_approve(hub_service):
    _, base_url = hub_service
    client_a = ExperienceClient(base_url=base_url, token="token_emp_a")
    cand = _make_candidate("exp_review_fail_1", customer="customer_acme")
    client_a.submit_candidate(cand)

    # Emp A (contributor) tries to approve -> 403 Forbidden
    res = client_a.review_candidate("exp_review_fail_1", action="approve", comment="我自己通过")
    assert res.get("ok") is False
    assert res.get("status_code") == 403


def test_reviewer_approve_reject_and_revoke_flow(hub_service):
    _, base_url = hub_service
    client_contrib = ExperienceClient(base_url=base_url, token="token_emp_a")
    client_reviewer = ExperienceClient(base_url=base_url, token="token_reviewer")

    cand = _make_candidate("exp_flow_1", customer="customer_acme", content="VIP客户提供免费打样")
    client_contrib.submit_candidate(cand)

    # 1. Before approval: query_rules MUST NOT return it
    rules_before = client_reviewer.query_rules(customer="customer_acme")
    assert not any(r.experience_id == "exp_flow_1" for r in rules_before)

    # 2. Reviewer approves
    res_app = client_reviewer.review_candidate("exp_flow_1", action="approve", comment="符合商务政策")
    assert res_app.get("ok") is True
    assert res_app.get("status") == "published"

    # 3. After approval: query_rules MUST return it
    rules_after = client_reviewer.query_rules(customer="customer_acme")
    matching = [r for r in rules_after if r.experience_id == "exp_flow_1"]
    assert len(matching) == 1
    assert matching[0].content == "VIP客户提供免费打样"
    assert matching[0].status == "published"

    # 4. Reviewer revokes the rule
    res_rev = client_reviewer.revoke_rule("exp_flow_1", reason="政策到期撤回")
    assert res_rev.get("ok") is True
    assert res_rev.get("status") == "revoked"

    # 5. After revocation: query_rules MUST NOT return it anymore
    rules_revoked = client_reviewer.query_rules(customer="customer_acme")
    assert not any(r.experience_id == "exp_flow_1" for r in rules_revoked)


def test_cross_customer_access_blocked(hub_service):
    _, base_url = hub_service
    client_reviewer = ExperienceClient(base_url=base_url, token="token_reviewer")

    # Seed rule for customer_beta
    cand = _make_candidate("exp_beta_1", customer="customer_beta", content="客户Beta核心保密条款")
    client_reviewer.submit_candidate(cand)
    client_reviewer.review_candidate("exp_beta_1", action="approve")

    # Emp A only has access to customer_acme
    client_emp_a = ExperienceClient(base_url=base_url, token="token_emp_a")

    # Try submitting candidate for customer_beta -> 403 Forbidden
    unauth_cand = _make_candidate("exp_beta_unauth", customer="customer_beta")
    sub_res = client_emp_a.submit_candidate(unauth_cand)
    assert sub_res.get("ok") is False
    assert sub_res.get("status_code") == 403

    # Try querying rules for customer_beta -> 403 Forbidden
    rules = client_emp_a.query_rules(customer="customer_beta")
    assert rules == []  # blocked, forbidden


def test_audit_logs_recorded(hub_service):
    _, base_url = hub_service
    client_reviewer = ExperienceClient(base_url=base_url, token="token_reviewer")
    cand = _make_candidate("exp_audit_1", customer="customer_acme")
    client_reviewer.submit_candidate(cand)
    client_reviewer.review_candidate("exp_audit_1", action="approve")
    client_reviewer.revoke_rule("exp_audit_1", reason="测试归档")

    # Fetch audit logs
    res = client_reviewer._request("GET", "/api/v1/experiences/audit")
    assert res.get("ok") is True
    logs = res.get("audit_logs") or []
    actions = [l["action"] for l in logs]
    assert "submit_candidate" in actions
    assert "review_approve" in actions
    assert "revoke_rule" in actions


def test_startup_without_admin_token_rejected():
    import subprocess
    import sys
    # Running bin/herdr-experience-hub with no admin token or env var should fail
    env = dict(os.environ)
    env.pop("HERDR_HUB_ADMIN_TOKEN", None)
    with tempfile.TemporaryDirectory() as td:
        empty_db = Path(td) / "uninit.db"
        proc = subprocess.run(
            [sys.executable, "bin/herdr-experience-hub", "start", "--db-path", str(empty_db), "--check-init-only"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert proc.returncode != 0
        assert "admin-token" in proc.stderr.lower() or "管理员凭证" in proc.stderr


def test_contributor_cannot_forge_published_status(hub_service):
    _, base_url = hub_service
    client = ExperienceClient(base_url=base_url, token="token_emp_a")

    cand = _make_candidate("exp_forge_1", customer="customer_acme", content="伪造发布的规则")
    cand.status = "published"  # Attempt to forge published status!
    res = client.submit_candidate(cand)
    assert res.get("ok") is True

    # Server must have forced status to pending_review
    detail = client.get_experience("exp_forge_1")
    assert detail is not None
    assert detail.status == "pending_review"

    # Querying published rules should NOT return it
    rules = client.query_rules(customer="customer_acme")
    assert not any(r.experience_id == "exp_forge_1" for r in rules)


def test_content_hash_conflict_returns_409(hub_service):
    _, base_url = hub_service
    client = ExperienceClient(base_url=base_url, token="token_emp_a")

    cand1 = _make_candidate("exp_collision_1", customer="customer_acme", content="原始业务规则 A")
    res1 = client.submit_candidate(cand1)
    assert res1.get("ok") is True

    # Attempt to submit same ID with different content -> 409 conflict
    cand2 = _make_candidate("exp_collision_1", customer="customer_acme", content="篡改冲突的业务规则 B")
    res2 = client.submit_candidate(cand2)
    assert res2.get("ok") is False
    assert res2.get("status_code") == 409
    assert "conflict" in (res2.get("error") or "").lower()


def test_contributor_cannot_read_other_contributor_candidates(hub_service):
    _, base_url = hub_service
    client_a = ExperienceClient(base_url=base_url, token="token_emp_a")
    client_b = ExperienceClient(base_url=base_url, token="token_emp_b")

    cand_a = _make_candidate("exp_private_a", customer="customer_acme", content="员工A的未审核私有经验")
    res = client_a.submit_candidate(cand_a)
    assert res.get("ok") is True

    # Contributor B listing candidates: should NOT see employee A's candidate
    b_cands = client_b.list_candidates()
    assert not any(c.experience_id == "exp_private_a" for c in b_cands)

    # Contributor B querying candidate details by ID: should get 403 Forbidden
    res_b_detail = client_b._request("GET", "/api/v1/experiences/candidate?id=exp_private_a")
    assert res_b_detail.get("ok") is False
    assert res_b_detail.get("status_code") == 403


def test_reviewer_cross_customer_review_and_revoke_forbidden(hub_service):
    server, base_url = hub_service
    # Register reviewer with access ONLY to customer_beta
    server.storage.register_token("token_rev_beta", user_id="rev_beta", role="reviewer", allowed_customers=["customer_beta"])

    client_a = ExperienceClient(base_url=base_url, token="token_emp_a")
    client_rev_beta = ExperienceClient(base_url=base_url, token="token_rev_beta")
    client_admin = ExperienceClient(base_url=base_url, token="token_admin")

    cand_acme = _make_candidate("exp_acme_cross", customer="customer_acme", content="Acme专有商务规则")
    client_a.submit_candidate(cand_acme)

    # Reviewer Beta attempts to approve Acme candidate -> 403 Forbidden
    res_app = client_rev_beta.review_candidate("exp_acme_cross", action="approve")
    assert res_app.get("ok") is False
    assert res_app.get("status_code") == 403

    # Admin approves it
    res_admin_app = client_admin.review_candidate("exp_acme_cross", action="approve")
    assert res_admin_app.get("ok") is True

    # Reviewer Beta attempts to revoke Acme rule -> 403 Forbidden
    res_rev = client_rev_beta.revoke_rule("exp_acme_cross", reason="非法越权撤回")
    assert res_rev.get("ok") is False
    assert res_rev.get("status_code") == 403


def test_unknown_customer_not_treated_as_global(hub_service):
    _, base_url = hub_service
    client_admin = ExperienceClient(base_url=base_url, token="token_admin")

    cand_unknown = _make_candidate("exp_unknown_1", customer="unknown", content="未知客户的临时规则")
    client_admin.submit_candidate(cand_unknown)
    client_admin.review_candidate("exp_unknown_1", action="approve")

    # Querying for customer_acme must NOT return the unknown customer's rule
    rules_acme = client_admin.query_rules(customer="customer_acme")
    assert not any(r.experience_id == "exp_unknown_1" for r in rules_acme)

    # Querying without customer context should NOT return unknown customer rules
    rules_all = client_admin.query_rules(customer=None)
    assert not any(r.experience_id == "exp_unknown_1" for r in rules_all)


def test_token_in_url_rejected(hub_service):
    import urllib.request
    _, base_url = hub_service
    # Sending token in query param without Authorization header must be rejected (401)
    req = urllib.request.Request(f"{base_url}/api/v1/experiences/rules?token=token_emp_a")
    try:
        urllib.request.urlopen(req)
        assert False, "Expected HTTP 401"
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
