"""Console dashboard API + page contracts (read-only, bounded, real clocks)."""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from console import herdr_factory_console as c

ROOT = Path(__file__).resolve().parent.parent
CLOCK_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


@pytest.fixture
def dash_env(tmp_path, monkeypatch):
    tasks = [
        {"task_id": "t-work", "workflow_id": "wf-1", "node": "implementation",
         "stage": "implementation", "agent": "codex", "status": "working",
         "goal": "做事", "created_at": 1700000000,
         "updated_at": 1700003000, "last_activity_at": 1700003000},
        {"task_id": "t-block", "workflow_id": "wf-1", "node": "review",
         "stage": "review", "agent": "codex", "status": "blocked",
         "stage_verdict": "blocked", "stage_verdict_note": "等你拍板",
         "goal": "验收", "created_at": 1700000000,
         "updated_at": 1700003100, "last_activity_at": 1700003100},
    ]
    wfmap = {"wf-1": {"workflow_id": "wf-1", "title": "w1",
                      "status": "running", "project_root": "/tmp/p1"}}
    monkeypatch.setattr(c, "tasks", lambda: [dict(t) for t in tasks])
    monkeypatch.setattr(c, "workflows", lambda: dict(wfmap))
    wf_file = tmp_path / "workflows.json"
    tasks_file = tmp_path / "tasks.json"
    wf_file.write_text(json.dumps({"workflows": wfmap}), encoding="utf-8")
    tasks_file.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
    return {"wf_file": wf_file, "tasks_file": tasks_file}


def test_dashboard_data_sections_and_real_clocks(dash_env):
    with patch.object(c.herdr_workflow_docs, "load_notes", return_value=[]):
        payload = c.dashboard_data()
    assert set(payload) >= {"generated_at", "generated_at_text", "tasks",
                            "attention", "deliveries", "stuck", "counts"}
    assert CLOCK_RE.match(payload["generated_at_text"])
    assert any(t["task_id"] == "t-work" for t in payload["tasks"])
    for t in payload["tasks"]:
        assert CLOCK_RE.match(t["updated_at_text"])
    bids = [a["task_id"] for a in payload["attention"]]
    assert "t-block" in bids
    att = next(a for a in payload["attention"] if a["task_id"] == "t-block")
    assert att["default_action"], "must carry default action"
    assert CLOCK_RE.match(att["updated_at_text"])


def test_dashboard_data_readonly_and_failure_isolated(dash_env):
    before = json.dumps(c.tasks(), sort_keys=True, default=str)

    def _boom(wid):
        raise RuntimeError("notes broken")

    with patch.object(c.herdr_workflow_docs, "load_notes", side_effect=_boom):
        payload = c.dashboard_data()
    # failure isolated: page still renders tasks/attention
    assert len(payload["tasks"]) >= 2
    assert any(a["task_id"] == "t-block" for a in payload["attention"])
    assert json.dumps(c.tasks(), sort_keys=True, default=str) == before


def test_dashboard_integrated_into_homepage():
    src = (ROOT / "console" / "herdr_factory_console.py").read_text(encoding="utf-8")
    for token in ("showDashboard", "exitDashboard", "loadDashboard",
                  "renderDashboard", "dashMode", "dashButton", "qView",
                  "view=dashboard", "dashTimer",
                  "setInterval(()=>{if(state.dashMode",
                  "dashSignoff", "dashExec"):
        assert token in src, f"missing homepage integration token: {token}"
    assert "DASHBOARD_HTML" not in src, "standalone dark page must be gone"
    assert "302" in src and "/?view=dashboard" in src


def test_dashboard_light_theme_reuses_homepage_tokens():
    html = c.HTML
    for token in ("dash-kpis", "dash-sec", "dash-task", "dash-q",
                  "dash-default", "dash-btns"):
        assert token in html, f"missing light dash style: {token}"
    for bad in ("❶", "❷", "❸", "❹", "--bg:#08090b"):
        assert bad not in html, "dark standalone markers must go"


def test_dashboard_two_column_layout_attention_first():
    src = (ROOT / "console" / "herdr_factory_console.py").read_text(encoding="utf-8")
    for token in ("dash-layout", "dash-main", "dash-rail", "position:sticky"):
        assert token in src, f"missing layout token: {token}"
    body = re.search(r"function renderDashboard\(\)\{(.*?)\n\}\nasync function dashSignoff",
                     src, re.DOTALL)
    assert body, "renderDashboard body not found"
    order = [body.group(1).index(t) for t in
             ("<span>任务及其状态</span>", "<span>最新交付物</span>",
              "<span>等你的问题</span>", "<span>卡住的东西</span>")]
    assert order == sorted(order), "sections must flow tasks->deliveries then rail"
    rail = body.group(1).index("dash-rail")
    att = body.group(1).index("<span>等你的问题</span>")
    assert rail < att, "attention must live in the rail"


def test_dashboard_double_click_entry():
    cmd = ROOT / "console" / "HerdrDashboard.command"
    assert cmd.exists()
    assert cmd.stat().st_mode & 0o111, "must be executable for double-click"
    text = cmd.read_text(encoding="utf-8")
    assert "view=dashboard" in text
    assert "com.user.herdr-factory-console" in text
