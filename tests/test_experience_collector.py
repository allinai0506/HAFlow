"""Tests for Experience Extractor and Local Collector."""

import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from herdr.experience.collector import ExperienceCollector
from herdr.experience.extractor import extract_candidate_from_facts
from herdr.experience.models import BusinessRuleExperience, ExperienceStatus


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def test_candidate_extracted_on_qualified_success_with_human_correction():
    task = {
        "task_id": "task_quote_001",
        "run_id": "run_quote_001",
        "workflow_id": "wf_quote_v1",
        "status": "completed",
        "customer": "customer_acme",
        "task_type": "quote",
    }
    outcome = {
        "outcome_id": "out_001",
        "run_id": "run_quote_001",
        "task_id": "task_quote_001",
        "qualified_success": True,
        "verification_passed": True,
        "recorded_at": time.time(),
    }
    steers = [
        {
            "steer_id": "steer_101",
            "task_id": "task_quote_001",
            "operator": "human",
            "instruction": "客户 ACME 报价规则：税前总额超过 10000 时享受 85 折优惠并额外减免 500 元。",
        }
    ]

    exp, reason = extract_candidate_from_facts(
        task=task,
        outcome=outcome,
        steers=steers,
        contributor="emp_alice",
    )

    assert exp is not None
    assert reason == "ready"
    assert exp.type == "business_rule"
    assert exp.status == ExperienceStatus.PENDING_REVIEW.value
    assert exp.applies_to.customer == "customer_acme"
    assert exp.applies_to.task_type == "quote"
    assert "85 折优惠并额外减免 500 元" in exp.content
    assert exp.contributor == "emp_alice"
    assert exp.source.task_id == "task_quote_001"
    assert "steer:steer_101" in exp.source.evidence_refs


def test_no_candidate_when_verification_failed():
    task = {
        "task_id": "task_failed_001",
        "run_id": "run_001",
        "status": "failed",
    }
    outcome = {
        "outcome_id": "out_failed",
        "run_id": "run_001",
        "task_id": "task_failed_001",
        "qualified_success": False,
        "verification_passed": False,
    }
    steers = [
        {
            "operator": "human",
            "instruction": "特殊规则：报价不得低于成本价 1.2 倍",
        }
    ]

    exp, reason = extract_candidate_from_facts(task=task, outcome=outcome, steers=steers)
    assert exp is None
    assert reason == "unqualified_success"


def test_no_candidate_when_no_human_correction():
    task = {
        "task_id": "task_auto_001",
        "run_id": "run_auto_001",
        "status": "completed",
    }
    outcome = {
        "outcome_id": "out_auto",
        "run_id": "run_auto_001",
        "task_id": "task_auto_001",
        "qualified_success": True,
        "verification_passed": True,
    }
    # No human intervention
    exp, reason = extract_candidate_from_facts(task=task, outcome=outcome, steers=[], decisions=[])
    assert exp is None
    assert reason == "no_human_intervention"


def test_no_candidate_on_trivial_instruction():
    task = {
        "task_id": "task_trivial_001",
        "run_id": "run_001",
        "status": "completed",
    }
    outcome = {
        "outcome_id": "out_001",
        "qualified_success": True,
        "verification_passed": True,
    }
    steers = [
        {"operator": "human", "instruction": "ok, continue"}
    ]
    exp, reason = extract_candidate_from_facts(task=task, outcome=outcome, steers=steers)
    assert exp is None
    assert reason == "no_actionable_rule"


def test_sensitive_info_redacted():
    task = {
        "task_id": "task_sec_001",
        "run_id": "run_sec_001",
        "status": "completed",
    }
    outcome = {
        "outcome_id": "out_sec",
        "qualified_success": True,
        "verification_passed": True,
    }
    steers = [
        {
            "operator": "human",
            "instruction": "客户 API 规则：必须使用 Token sk-1234567890abcdef1234567890 访问接口并设置折扣 9 折",
        }
    ]
    exp, reason = extract_candidate_from_facts(task=task, outcome=outcome, steers=steers)
    assert exp is not None
    assert "sk-1234567890abcdef1234567890" not in exp.content
    assert "[redacted]" in exp.content.lower()
    assert "折扣 9 折" in exp.content


def test_collector_idempotency_and_offline_buffering(temp_dir):
    collector_db = temp_dir / "collector.db"
    mock_hub = MagicMock()
    # First attempt: Hub offline (raises network error)
    mock_hub.submit_candidate.side_effect = ConnectionError("Hub unreachable")

    collector = ExperienceCollector(db_path=collector_db, hub_client=mock_hub, contributor="test_user")

    # Mock state_db.get_task and state_db.list_steers
    from unittest.mock import patch
    fake_task = {
        "task_id": "task_idem_1",
        "run_id": "run_idem_1",
        "status": "completed",
        "customer": "cust_x",
        "task_type": "pricing",
    }
    fake_steers = [
        {"operator": "human", "instruction": "必须遵循阶梯定价：100件以上单价80元，否则单价100元"}
    ]
    fake_outcome = {
        "outcome_id": "out_idem_1",
        "qualified_success": True,
        "verification_passed": True,
    }

    with patch("herdr.state_db.get_task", return_value=fake_task), \
         patch("herdr.state_db.list_steers", return_value=fake_steers):

        # 1. Process settlement with offline hub
        res1 = collector.process_task_settlement("task_idem_1", fake_outcome)
        assert res1["ok"] is True
        assert res1["status"] == "pending_upload"
        exp_id = res1["experience_id"]
        assert exp_id is not None

        # 2. Hub comes back online -> retry
        mock_hub.submit_candidate.side_effect = None
        mock_hub.submit_candidate.return_value = {"ok": True, "experience_id": exp_id}

        retry_res = collector.retry_pending_uploads()
        assert retry_res["retried"] == 1
        assert retry_res["succeeded"] == 1

        # 3. Repeated trigger on same task/run -> idempotent no-op
        res2 = collector.process_task_settlement("task_idem_1", fake_outcome)
        assert res2["ok"] is True
        assert res2["status"] == "uploaded"
        assert res2["reason"] == "already_processed"
        assert res2["experience_id"] == exp_id
