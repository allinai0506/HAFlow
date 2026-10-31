"""PR #374 评审遗留回归：存活宽限不得依赖初始 prompt 留在可见屏。

标记滚出可见屏/屏幕读取失败 ≠ 任务不在此 pane——实例所有权
（owned_live_pane 的 identity_match）是等价的豁免证据；所有权未证明时
维持 PR #336 契约（不豁免，进入失败处置）。
"""
from __future__ import annotations

import subprocess
import time
from unittest.mock import patch

import pytest

from herdr.state_store import SQLiteStateStore


@pytest.fixture
def sentinel(monkeypatch):
    monkeypatch.setenv("HERDR_TASK_HARD_TIMEOUT", "10")
    return __import__("importlib").import_module("services.herdr-sentinel")


def _make_clone(tmp_path, commits):
    clone = tmp_path / "clone"
    clone.mkdir()

    def git(*args, env=None):
        e = dict(__import__("os").environ)
        e.update(env or {})
        return subprocess.run(["git", "-C", str(clone), *args], check=True,
                              text=True, capture_output=True, env=e).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "fixture")
    shas = []
    now = time.time()
    for index, (age, marker) in enumerate(commits):
        (clone / f"f{index}").write_text(marker)
        stamp = int(now - age)
        genv = {"GIT_AUTHOR_DATE": f"@{stamp} +0000",
                "GIT_COMMITTER_DATE": f"@{stamp} +0000"}
        git("add", ".", env=genv)
        git("commit", "-qm", marker, "--no-gpg-sign", env=genv)
        shas.append(git("rev-parse", "HEAD"))
    return clone, shas


def _persist(store, task, *, idle, now):
    import sqlite3
    store.save_task(task)
    stamp = now - idle
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE tasks SET updated_at=?, "
            "payload_json=json_set(payload_json,'$.updated_at',?) WHERE task_id=?",
            (stamp, stamp, task["task_id"]))
    return store.get_task(task["task_id"])


def _task(clone, *, now, idle=15.0, baseline_commit=None):
    return {
        "task_id": "t-owned-grace",
        "workflow_id": "wf-owned-grace",
        "node": "craft",
        "stage": "craft",
        "status": "working",
        "pane_id": "w1:p1",
        "clone_path": str(clone),
        "gate": False,
        "baseline_commit": baseline_commit,
        "updated_at": now - idle,
        "created_at": now - 3600,
    }


def _state():
    return {"stalls": {}, "timeouts": {}}


def test_working_agent_with_scrolled_off_marker_gets_grace_via_ownership(
        sentinel, tmp_path, monkeypatch):
    """标记滚出可见屏但实例所有权已证明 → 必须获得有界宽限（#374 评审遗留）。"""
    now = time.time()
    clone, shas = _make_clone(tmp_path, [(10_000.0, "baseline")])
    store = SQLiteStateStore(tmp_path / "state.db")
    stored = _persist(store, _task(clone, now=now, baseline_commit=shas[0]),
                      idle=15.0, now=now)
    monkeypatch.setattr(sentinel, "_get_store", lambda: store)
    monkeypatch.setattr(sentinel, "_owned_pane",
                        lambda task: (True, "identity_match"), raising=False)
    with patch.object(sentinel, "_pane_delivery_evidence",
                      return_value={"has_marker": False, "agent_status": "working"}):
        sentinel.check_task_stalls([stored], _state(),
                                   notify_fn=lambda *a: None, now=now)
    assert store.get_task("t-owned-grace")["status"] == "working"


def test_working_agent_without_marker_or_ownership_still_fails(
        sentinel, tmp_path, monkeypatch):
    """对照（PR #336 契约）：标记缺失且所有权未证明 → 不豁免，进入失败处置。"""
    now = time.time()
    clone, shas = _make_clone(tmp_path, [(10_000.0, "baseline")])
    store = SQLiteStateStore(tmp_path / "state.db")
    stored = _persist(store, _task(clone, now=now, baseline_commit=shas[0]),
                      idle=15.0, now=now)
    monkeypatch.setattr(sentinel, "_get_store", lambda: store)
    monkeypatch.setattr(sentinel, "_owned_pane",
                        lambda task: (False, "pane_not_found"), raising=False)
    with patch.object(sentinel, "_pane_delivery_evidence",
                      return_value={"has_marker": False, "agent_status": "working"}):
        sentinel.check_task_stalls([stored], _state(),
                                   notify_fn=lambda *a: None, now=now)
    assert store.get_task("t-owned-grace")["status"] == "failed"


def test_working_agent_with_visible_marker_keeps_grace_without_ownership_probe(
        sentinel, tmp_path, monkeypatch):
    """对照：标记可见时维持既有宽限，且不触发所有权探测（惰性边界）。"""
    now = time.time()
    clone, shas = _make_clone(tmp_path, [(10_000.0, "baseline")])
    store = SQLiteStateStore(tmp_path / "state.db")
    stored = _persist(store, _task(clone, now=now, baseline_commit=shas[0]),
                      idle=15.0, now=now)
    monkeypatch.setattr(sentinel, "_get_store", lambda: store)
    monkeypatch.setattr(sentinel, "_owned_pane",
                        lambda task: (_ for _ in ()).throw(
                            AssertionError("marker 可见时不得探测所有权")), raising=False)
    with patch.object(sentinel, "_pane_delivery_evidence",
                      return_value={"has_marker": True, "agent_status": "working"}):
        sentinel.check_task_stalls([stored], _state(),
                                   notify_fn=lambda *a: None, now=now)
    assert store.get_task("t-owned-grace")["status"] == "working"
