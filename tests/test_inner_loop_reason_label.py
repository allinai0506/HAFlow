"""inner_loop_exhausted stays a machine code; people see Chinese."""

import importlib.machinery
import importlib.util
from pathlib import Path

from herdr.dashboard import build_dashboard
from herdr.projection import extract_task_blockers
from herdr.reason_labels import INNER_LOOP_EXHAUSTED_TEXT, human_reason

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = (ROOT / "console" / "herdr_factory_console.py").read_text(encoding="utf-8")
NOTIFIER = (ROOT / "services" / "herdr-notifier.py").read_text(encoding="utf-8")
SENTINEL = (ROOT / "services" / "herdr-sentinel.py").read_text(encoding="utf-8")


def test_known_code_is_chinese_and_other_text_is_kept():
    assert human_reason("inner_loop_exhausted") == INNER_LOOP_EXHAUSTED_TEXT
    assert human_reason("  inner_loop_exhausted  ") == INNER_LOOP_EXHAUSTED_TEXT
    assert human_reason(None) == ""
    assert human_reason("No disk space") == "No disk space"
    assert "无法自愈" in INNER_LOOP_EXHAUSTED_TEXT
    assert "inner_loop_exhausted" not in INNER_LOOP_EXHAUSTED_TEXT


def test_projection_blocker_uses_chinese_label():
    blockers = extract_task_blockers(
        {"status": "blocked", "sentinel_reason": "inner_loop_exhausted"},
        "",
    )
    assert blockers == [INNER_LOOP_EXHAUSTED_TEXT]


def test_dashboard_attention_uses_chinese_label():
    payload = build_dashboard(
        [{
            "task_id": "t-block",
            "workflow_id": "wf-1",
            "status": "blocked",
            "node": "implementation",
            "sentinel_reason": "inner_loop_exhausted",
            "updated_at": 1700003100,
        }],
        blockers=[],
        actions_by_task={},
        deliveries=[],
        stalls={},
        anomalies=[],
        now=1700003600,
    )
    attention = next(item for item in payload["attention"] if item["task_id"] == "t-block")
    assert attention["reason"] == INNER_LOOP_EXHAUSTED_TEXT


def test_notifier_reason_matches_shared_label():
    path = ROOT / "services" / "herdr-notifier.py"
    spec = importlib.util.spec_from_loader(
        "herdr_notifier_reason_label",
        importlib.machinery.SourceFileLoader("herdr_notifier_reason_label", str(path)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.reason({"sentinel_reason": "inner_loop_exhausted"}) == INNER_LOOP_EXHAUSTED_TEXT
    assert module.reason({"failure_reason": "磁盘满了"}) == "磁盘满了"
    assert INNER_LOOP_EXHAUSTED_TEXT in NOTIFIER


def test_console_and_sentinel_show_the_same_chinese_label():
    assert f"inner_loop_exhausted:'{INNER_LOOP_EXHAUSTED_TEXT}'" in CONSOLE
    assert "function humanReason(" in CONSOLE
    assert "INNER_LOOP_EXHAUSTED_TEXT" in SENTINEL
    assert "[SENTINEL BLOCKER]" in SENTINEL
    assert '"reason": "inner_loop_exhausted"' in SENTINEL
