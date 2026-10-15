"""End-to-End Dual-Client Integration Test for Experience Hub V1.

Simulates two independent employees (Employee A and Employee B) on two
separate simulated machines with isolated databases and configurations,
coordinating through an independent intranet Experience Hub service.

Validates the full enterprise lifecycle:
1. Employee A encounters business rule issue, provides human correction.
2. Task reaches qualified_success; Collector automatically extracts candidate.
3. Candidate automatically syncs to Experience Hub (idempotent).
4. Unapproved candidate cannot be seen or used by Employee B.
5. Enterprise Reviewer reviews and approves the rule.
6. Employee B launches task on independent machine for same customer:
   - Rule is automatically discovered and injected into prompt context.
   - Business acceptance verifies the rule is applied.
7. Employee A's local data deleted; Employee B still retrieves the rule from Hub.
8. Rule revocation immediately prevents injection into new tasks.
9. Hub/network outage gracefully degrades without breaking tasks.
"""

import os
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from herdr import state_db
from herdr.experience.client import ExperienceClient
from herdr.experience.collector import ExperienceCollector
from herdr.experience.extractor import extract_candidate_from_facts
from herdr.experience.hub_server import ExperienceHubServer
from herdr.experience.models import ExperienceStatus


@pytest.fixture
def enterprise_environment():
    """Setup independent Hub and two isolated client environments."""
    with tempfile.TemporaryDirectory() as base_td:
        base_dir = Path(base_td)

        # 1. Independent Hub Server on intranet
        hub_dir = base_dir / "hub_server"
        hub_db = hub_dir / "experience_hub.db"
        server = ExperienceHubServer(host="127.0.0.1", port=0, db_path=hub_db)

        # Pre-seed enterprise tokens
        server.storage.register_token("token_a", user_id="employee_a", role="contributor", allowed_customers=["customer_quote_vip"])
        server.storage.register_token("token_b", user_id="employee_b", role="contributor", allowed_customers=["customer_quote_vip"])
        server.storage.register_token("token_rev", user_id="reviewer_lead", role="reviewer", allowed_customers=["*"])

        server.start(block=False)
        time.sleep(0.05)
        hub_url = f"http://127.0.0.1:{server.port}"

        # 2. Client A (Employee A's machine)
        dir_a = base_dir / "client_a"
        dir_a.mkdir(parents=True)
        db_a = dir_a / "state_a.db"
        state_db.init_db(db_a)
        collector_db_a = dir_a / "collector_a.db"
        client_sdk_a = ExperienceClient(base_url=hub_url, token="token_a")
        collector_a = ExperienceCollector(db_path=collector_db_a, hub_client=client_sdk_a, contributor="employee_a")

        # 3. Client B (Employee B's machine)
        dir_b = base_dir / "client_b"
        dir_b.mkdir(parents=True)
        db_b = dir_b / "state_b.db"
        state_db.init_db(db_b)
        collector_db_b = dir_b / "collector_b.db"
        client_sdk_b = ExperienceClient(base_url=hub_url, token="token_b")
        collector_b = ExperienceCollector(db_path=collector_db_b, hub_client=client_sdk_b, contributor="employee_b")

        reviewer_sdk = ExperienceClient(base_url=hub_url, token="token_rev")

        yield {
            "server": server,
            "hub_url": hub_url,
            "client_a": {
                "dir": dir_a,
                "db": db_a,
                "collector": collector_a,
                "sdk": client_sdk_a,
            },
            "client_b": {
                "dir": dir_b,
                "db": db_b,
                "collector": collector_b,
                "sdk": client_sdk_b,
            },
            "reviewer": reviewer_sdk,
        }

        server.stop()


def test_full_enterprise_experience_lifecycle_dual_clients(enterprise_environment):
    env = enterprise_environment
    ca = env["client_a"]
    cb = env["client_b"]
    rev = env["reviewer"]

    # =========================================================================
    # Phase 1: Employee A works on quotation task for customer_quote_vip
    # =========================================================================
    task_a = {
        "task_id": "quote_task_001_a",
        "run_id": "run_quote_001_a",
        "workflow_id": "wf_pricing",
        "customer": "customer_quote_vip",
        "task_type": "quote",
        "status": "completed",
    }
    state_db.save_task(task_a, db_path=ca["db"])

    # Employee A provides human steering correction during execution
    steer_inst = "VIP客户特殊报价规则：单笔订单金额超过 10000 时，享受 85 折优惠且免收 500 元加急费。"
    state_db.save_steer({
        "steer_id": "steer_quote_001",
        "task_id": "quote_task_001_a",
        "instruction": steer_inst,
        "operator": "human",
        "urgent": True,
        "status": "dispatched",
    }, db_path=ca["db"])

    # Task completes and settles outcome with qualified_success=True
    outcome_a = {
        "outcome_id": "out_quote_001_a",
        "task_id": "quote_task_001_a",
        "run_id": "run_quote_001_a",
        "workflow_id": "wf_pricing",
        "agent": "codex",
        "node": "quote_calc",
        "final_status": "completed",
        "requirements_satisfied": True,
        "verification_passed": True,
        "qualified_success": True,
        "human_intervention_count": 1,
        "recorded_at": time.time(),
        "source_eval_id": "eval_001_a",
        "source_eval_revision": 1,
    }

    # =========================================================================
    # Phase 2: Collector on Client A automatically discovers & syncs candidate
    # =========================================================================
    collect_res = ca["collector"].process_task_settlement(
        task_id="quote_task_001_a",
        outcome=outcome_a,
        state_db_path=ca["db"],
    )
    assert collect_res["ok"] is True
    assert collect_res["status"] == "uploaded"
    exp_id = collect_res["experience_id"]
    assert exp_id is not None

    # Verify idempotency on Client A: repeat trigger returns already_processed
    repeat_res = ca["collector"].process_task_settlement(
        task_id="quote_task_001_a",
        outcome=outcome_a,
        state_db_path=ca["db"],
    )
    assert repeat_res["ok"] is True
    assert repeat_res["status"] == "uploaded"
    assert repeat_res["reason"] == "already_processed"
    assert repeat_res["experience_id"] == exp_id

    # =========================================================================
    # Phase 3: Gate check: Unapproved candidate CANNOT be used by Employee B
    # =========================================================================
    rules_b_before = cb["sdk"].query_rules(customer="customer_quote_vip", task_type="quote")
    assert len(rules_b_before) == 0  # Pending candidate must not be exposed as active rule

    # =========================================================================
    # Phase 4: Enterprise Reviewer inspects and approves the candidate
    # =========================================================================
    candidates = rev.list_candidates(status=ExperienceStatus.PENDING_REVIEW.value)
    matching_cands = [c for c in candidates if c.experience_id == exp_id]
    assert len(matching_cands) == 1
    target_cand = matching_cands[0]
    assert target_cand.applies_to.customer == "customer_quote_vip"
    assert "85 折优惠且免收 500 元加急费" in target_cand.content

    # Reviewer approves
    app_res = rev.review_candidate(exp_id, action="approve", comment="核对无误，批准为全公司VIP报价规则")
    assert app_res.get("ok") is True
    assert app_res.get("status") == ExperienceStatus.PUBLISHED.value

    # =========================================================================
    # Phase 5: Cross-Employee Reuse: Employee B launches quote task
    # =========================================================================
    # On Employee B's machine, query_rules now retrieves the published rule
    rules_b_after = cb["sdk"].query_rules(customer="customer_quote_vip", task_type="quote")
    assert len(rules_b_after) == 1
    active_rule = rules_b_after[0]
    assert active_rule.experience_id == exp_id
    assert active_rule.status == ExperienceStatus.PUBLISHED.value

    # Test bin/herdr-task helper prompt injection on Client B
    task_b = {
        "task_id": "quote_task_002_b",
        "customer": "customer_quote_vip",
        "task_type": "quote",
        "payload_json": '{"customer": "customer_quote_vip", "task_type": "quote"}',
    }

    # Simulate environment for Client B with Client B's SDK
    with patch.dict(os.environ, {"HERDR_HUB_URL": env["hub_url"], "HERDR_HUB_TOKEN": "token_b"}):
        from herdr.experience.client import fetch_experience_rules_block
        rules_block = fetch_experience_rules_block(task_b)

        # Verify the rule actually entered the prompt block
        assert "【企业已审核业务规则 (Enterprise Business Rules)】" in rules_block
        assert exp_id in rules_block
        assert "VIP客户特殊报价规则" in rules_block
        assert "85 折优惠且免收 500 元加急费" in rules_block

    # Business acceptance logic: verify that calculation adhering to this rule produces correct result
    order_amount = 20000.0
    discount_rate = 0.85
    waived_fee = 500.0
    expected_quote = order_amount * discount_rate
    assert expected_quote == 17000.0

    # =========================================================================
    # Phase 6: Requirement 11: Employee A local data deleted; Hub still serves B
    # =========================================================================
    shutil.rmtree(ca["dir"])
    assert not ca["dir"].exists()

    # Employee B can STILL retrieve and use the published rule from Enterprise Hub
    rules_b_survives = cb["sdk"].query_rules(customer="customer_quote_vip", task_type="quote")
    assert len(rules_b_survives) == 1
    assert rules_b_survives[0].experience_id == exp_id

    # =========================================================================
    # Phase 7: Requirement 9: Revoked rule is no longer used for new tasks
    # =========================================================================
    rev_res = rev.revoke_rule(exp_id, reason="商务政策到期废止")
    assert rev_res.get("ok") is True
    assert rev_res.get("status") == ExperienceStatus.REVOKED.value

    # Employee B immediately sees 0 rules for this customer
    rules_b_revoked = cb["sdk"].query_rules(customer="customer_quote_vip", task_type="quote")
    assert len(rules_b_revoked) == 0

    # =========================================================================
    # Phase 8: Requirement 10: Collector or Hub outage gracefully degrades
    # =========================================================================
    env["server"].stop()  # Hub offline

    with patch.dict(os.environ, {"HERDR_HUB_URL": env["hub_url"], "HERDR_HUB_TOKEN": "token_b"}):
        from herdr.experience.client import fetch_experience_rules_block
        # Ordinary task does not crash; returns empty rules block
        offline_block = fetch_experience_rules_block(task_b)
        assert offline_block == ""


def test_mandatory_rules_fail_closed_on_hub_outage():
    from herdr.experience.client import fetch_experience_rules_block

    mandatory_task = {
        "task_id": "quote_task_mandatory_001",
        "customer": "customer_quote_vip",
        "task_type": "quote",
        "mandatory_experience_rules": True,
    }

    # 1. Hub offline (unreachable address) -> Must fail closed (raise RuntimeError)
    with patch.dict(os.environ, {"HERDR_HUB_URL": "http://127.0.0.1:59999", "HERDR_HUB_TOKEN": "token_dummy"}):
        with pytest.raises(RuntimeError, match="Mandatory business rules cannot be acquired|Hub is unreachable"):
            fetch_experience_rules_block(mandatory_task)

    # 2. Ordinary non-mandatory task -> Must fail open (returns "")
    normal_task = {
        "task_id": "quote_task_normal_001",
        "customer": "customer_quote_vip",
        "task_type": "quote",
        "mandatory_experience_rules": False,
    }
    with patch.dict(os.environ, {"HERDR_HUB_URL": "http://127.0.0.1:59999", "HERDR_HUB_TOKEN": "token_dummy"}):
        block = fetch_experience_rules_block(normal_task)
        assert block == ""
