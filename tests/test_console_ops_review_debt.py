"""PR #358/#361/#364 cockpit/console 评审遗留回归（批次 4）。

- [4b] maybe_close_completed_workflow 必须在限制视图中声明 load_tasks，
  大工作流 close 超时按任务数缩放而非固定 900s。
- [4c] Ops Center 卡片 startup 字段（含 failed_stage）由同一 inventory
  投影携带，不得每卡片重开 recovery snapshot（超大 config 使整页失败）。
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import time
from types import SimpleNamespace

from herdr.state_store import SQLiteStateStore


def test_maybe_close_declares_load_tasks_and_scales_timeout():
    """[4b] 限制视图（真实 Dependencies）下 load_tasks 必须可用且超时缩放。"""
    from services.controller import workflow_close
    from services.controller.bindings import Dependencies

    declared = workflow_close.DEPENDENCIES["maybe_close_completed_workflow"]
    assert "load_tasks" in declared

    runs = []

    def fake_run(cmd, **kwargs):
        runs.append(kwargs)
        return SimpleNamespace(returncode=0, stdout="closed", stderr="")

    tasks = [{"task_id": f"t-{i}", "workflow_id": "wf-big"} for i in range(28)]
    values = {
        "TASK_MANAGER": "herdr-task",
        "_close_deferred_logged": set(),
        "_workflow_close_inflight": set(),
        "_workflow_entry": lambda wid: {"workflow_id": wid, "status": "running"},
        "attention_blocks_retry": lambda key: False,
        "attention_clear": lambda key: None,
        "_note_close_backoff": lambda wid, key: None,
        "git_escalated_tasks": lambda *a, **k: [],
        "git_finalize_pending_tasks": lambda *a, **k: [],
        "liveness": SimpleNamespace(workflow_is_foreign=lambda entry: False),
        "subprocess": SimpleNamespace(run=fake_run),
        "threading": SimpleNamespace(
            Thread=lambda target, daemon, name: SimpleNamespace(start=target)),
        "load_tasks": lambda: tasks,
    }
    ports = Dependencies(dict(values), declared)
    workflow_close.maybe_close_completed_workflow(ports, "wf-big")
    assert runs and runs[0]["timeout"] == max(900, 28 * 120)


def test_ops_cards_carry_failed_stage_without_per_card_snapshot(tmp_path, monkeypatch):
    """[4c] 超大 config 不得使 Ops Center 失败；failed_stage 由投影直达卡片。"""
    db = tmp_path / "state.db"
    store = SQLiteStateStore(db)
    store.save_workflow({
        "workflow_id": "wf-startup-failed",
        "title": "startup failed",
        "status": "completed",
        "outcome": "startup_failed",
        "startup_ready": False,
        "failed_stage": "preflight",
        "startup_failure_reason": "preflight refused",
        "config": {"name": "huge-config"},
    })
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE workflows SET config_json=? WHERE workflow_id=?",
                     (json.dumps({"pad": "x" * 300_000}), "wf-startup-failed"))
    monkeypatch.setenv("HERDR_STATE_DB", str(db))

    inventory = importlib.import_module("console.inventory")
    payload = inventory.ops_center()
    cards = payload["workflow_cards"]
    card = next(c for c in cards if c.get("workflow_id") == "wf-startup-failed")
    assert card["outcome"] == "startup_failed"
    assert card["startup_ready"] is False
    assert card["failed_stage"] == "preflight"
    assert card["startup_failure_reason"] == "preflight refused"


def test_build_workflow_cards_carries_failed_stage_from_projection(tmp_path):
    """[4c] 投影层单元：workflow_lookup 记录中的 failed_stage 落到卡片。"""
    from herdr.ops_center_projection import _build_workflow_cards

    record = {
        "workflow_id": "wf-x",
        "title": "t",
        "config": {"name": "n", "nodes": [{"id": "plan", "required_task_ids": []}]},
        "status": "completed",
        "startup_ready": False,
        "outcome": "startup_failed",
        "failed_stage": "startup",
        "startup_failure_reason": "r",
    }

    def lookup(wid):
        return dict(record), record["title"]

    cards = _build_workflow_cards({"wf-x": []}, time.time(),
                                  all_tasks=[], workflow_lookup=lookup)
    assert cards and cards[0]["failed_stage"] == "startup"
    assert cards[0]["outcome"] == "startup_failed"
