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
