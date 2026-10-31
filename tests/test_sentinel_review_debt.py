"""PR #360/#361 sentinel 评审遗留回归（批次 2）。

- [2a] salvage 的 commit 元数据必须与状态转换同一 CAS 事务，不得先写 metadata。
- [2b] committed-but-working 的宽限必须有有限界限（新鲜提交才续期）。
- [2c] 无 baseline 的遗留任务：继承 HEAD 不是交付证据，按创建时间归属。
- [2d] `_task_has_valid_gate_verdict` 必须校验 created_at 新鲜度（对齐 Controller）。
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import time
from unittest.mock import patch

import pytest

from herdr.state_store import SQLiteStateStore


@pytest.fixture
def sentinel(monkeypatch):
    monkeypatch.setenv("HERDR_TASK_HARD_TIMEOUT", "10")
    return importlib.import_module("services.herdr-sentinel")


def _make_clone(tmp_path, commits, env_extra=None):
    """commits: list of (relative_time_before_now, marker); oldest first."""
    clone = tmp_path / "clone"
    clone.mkdir()
    def git(*args, env=None):
        return subprocess.run(["git", "-C", str(clone), *args], check=True,
                              text=True, capture_output=True, env=env).stdout.strip()
    git("init", "-q", "-b", "main")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "fixture")
    shas = []
    for index, (age, marker) in enumerate(commits):
        (clone / f"f{index}").write_text(marker)
        env = dict(os.environ)
        stamp = int(time.time() - age)
        env.update({"GIT_AUTHOR_DATE": f"@{stamp} +0000", "GIT_COMMITTER_DATE": f"@{stamp} +0000"})
        env.update(env_extra or {})
        git("add", ".", env=env)
        git("commit", "-qm", marker, "--no-gpg-sign", env=env)
        shas.append(git("rev-parse", "HEAD"))
    return clone, shas


def _task(clone, *, now=None, idle=100.0, created_at=None, **extra):
    now = time.time() if now is None else now
    task = {
        "task_id": "t-sentinel",
        "workflow_id": "wf-sentinel",
        "node": "craft",
        "stage": "craft",
        "status": "working",
        "pane_id": "w1:p1",
        "clone_path": str(clone),
        "gate": False,
        "updated_at": now - idle,
        "version": 2,
        "created_at": now - 3600 if created_at is None else created_at,
    }
    task.update(extra)
    return task


def _state():
    return {"stalls": {}, "timeouts": {}}


def _persist(store, task, *, idle, now):
    """落库并把 updated_at 回拨到 idle 基准（save_task 会刷新 updated_at）。"""
    import sqlite3
    store.save_task(task)
    stamp = now - idle
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE tasks SET updated_at=?, "
            "payload_json=json_set(payload_json,'$.updated_at',?) WHERE task_id=?",
            (stamp, stamp, task["task_id"]))
    return store.get_task(task["task_id"])


def test_salvage_metadata_and_status_share_one_cas_transition(sentinel, tmp_path, monkeypatch):
    """[2a] 先写 metadata 会自增 version 使 CAS 双拒；必须经同一原子转换。"""
    now = time.time()
    clone, shas = _make_clone(tmp_path, [(10_000.0, "baseline"), (100.0, "work")])
    store = SQLiteStateStore(tmp_path / "state.db")
    now = time.time()
    stored = _persist(store, _task(clone, baseline_commit=shas[0]), idle=100.0, now=now)
    monkeypatch.setattr(sentinel, "_get_store", lambda: store)
    version_before = stored["version"] = store.get_task("t-sentinel")["version"]
    calls = []
    real_update = sentinel.update_statuses
    def spy(changes, **kwargs):
        calls.append((dict(changes), kwargs))
        return real_update(changes, **kwargs)
    monkeypatch.setattr(sentinel, "update_statuses", spy)
    with patch.object(sentinel, "_pane_delivery_evidence",
                      return_value={"has_marker": True, "agent_status": "idle"}):
        sentinel.check_task_stalls([stored], _state(),
                                   notify_fn=lambda *a: None, now=now)
    task = store.get_task("t-sentinel")
    assert task["status"] == "agent_done"
    assert task["commit"] == shas[1] and task["candidate_sha"] == shas[1]
    assert task["version"] == version_before + 1
    assert calls and calls[0][0] == {"t-sentinel": ("agent_done", "sentinel_salvaged_commit")}
    assert calls[0][1].get("metadata", {}).get("t-sentinel", {}).get("commit") == shas[1]


def test_committed_but_stuck_agent_enters_recovery_after_fresh_window(sentinel, tmp_path, monkeypatch):
    """[2b] 提交一次后挂死：提交不再新鲜时必须落入恢复路径，而非永久豁免。"""
    now = time.time()
    clone, shas = _make_clone(tmp_path, [(10_000.0, "baseline"), (500.0, "stale work")])
    store = SQLiteStateStore(tmp_path / "state.db")
    now = time.time()
    stored = _persist(store, _task(clone, baseline_commit=shas[0]), idle=100.0, now=now)
    monkeypatch.setattr(sentinel, "_get_store", lambda: store)
    with patch.object(sentinel, "_pane_delivery_evidence",
                      return_value={"has_marker": True, "agent_status": "working"}):
        sentinel.check_task_stalls([stored], _state(),
                                   notify_fn=lambda *a: None, now=now)
    task = store.get_task("t-sentinel")
    assert task["status"] in {"agent_done", "failed"}


def test_fresh_commit_progress_still_grants_working_grace(sentinel, tmp_path, monkeypatch):
    """[2b] 对照：新鲜提交 + agent working 继续宽限，不提前判死。"""
    now = time.time()
    clone, shas = _make_clone(tmp_path, [(10_000.0, "baseline"), (5.0, "fresh work")])
    store = SQLiteStateStore(tmp_path / "state.db")
    now = time.time()
    stored = _persist(store, _task(clone, baseline_commit=shas[0]), idle=100.0, now=now)
    monkeypatch.setattr(sentinel, "_get_store", lambda: store)
    with patch.object(sentinel, "_pane_delivery_evidence",
                      return_value={"has_marker": True, "agent_status": "working"}):
        sentinel.check_task_stalls([stored], _state(),
                                   notify_fn=lambda *a: None, now=now)
    assert store.get_task("t-sentinel")["status"] == "working"


def test_inherited_head_is_not_salvaged_for_legacy_task(sentinel, tmp_path, monkeypatch):
    """[2c] 无 baseline 的遗留任务：派发前的继承 HEAD 不是任务产出。"""
    now = time.time()
    clone, _ = _make_clone(tmp_path, [(10_000.0, "inherited")])
    store = SQLiteStateStore(tmp_path / "state.db")
    stored = _persist(store, _task(clone, created_at=now - 60.0, baseline_commit=None), idle=100.0, now=now)
    monkeypatch.setattr(sentinel, "_get_store", lambda: store)
    with patch.object(sentinel, "_pane_delivery_evidence",
                      return_value={"has_marker": True, "agent_status": "idle"}):
        sentinel.check_task_stalls([stored], _state(),
                                   notify_fn=lambda *a: None, now=now)
    task = store.get_task("t-sentinel")
    assert task["status"] == "failed"
    assert not task.get("commit") and not task.get("candidate_sha")


def test_attributed_head_still_salvages_without_baseline(sentinel, tmp_path, monkeypatch):
    """[2c] 对照：无 baseline 但提交时间在派发之后，salvage 保持可用。"""
    now = time.time()
    clone, shas = _make_clone(tmp_path, [(30.0, "task work")])
    store = SQLiteStateStore(tmp_path / "state.db")
    now = time.time()
    stored = _persist(store, _task(clone, created_at=now - 60.0, baseline_commit=None), idle=100.0, now=now)
    monkeypatch.setattr(sentinel, "_get_store", lambda: store)
    with patch.object(sentinel, "_pane_delivery_evidence",
                      return_value={"has_marker": True, "agent_status": "idle"}):
        sentinel.check_task_stalls([stored], _state(),
                                   notify_fn=lambda *a: None, now=now)
    task = store.get_task("t-sentinel")
    assert task["status"] == "agent_done"
    assert task["commit"] == shas[0]


def test_stale_gate_verdict_is_not_valid_for_current_execution(sentinel, tmp_path):
    """[2d] 陈旧 gate verdict 不得通过当前执行校验（对齐 Controller created_at 界）。"""
    clone, _ = _make_clone(tmp_path, [(10_000.0, "base")])
    now = time.time()
    verdict_dir = clone / ".herdr"
    verdict_dir.mkdir(exist_ok=True)
    verdict_path = verdict_dir / "gate-verdict.json"
    verdict_path.write_text(json.dumps({"verdict": "pass", "note": "leftover"}), encoding="utf-8")
    stale_stamp = now - 1000.0
    os.utime(verdict_path, (stale_stamp, stale_stamp))
    stale_task = _task(clone, created_at=now - 60.0)
    assert sentinel._task_has_valid_gate_verdict(stale_task)[0] is False
    fresh_stamp = now - 10.0
    os.utime(verdict_path, (fresh_stamp, fresh_stamp))
    assert sentinel._task_has_valid_gate_verdict(stale_task)[0] is True


def test_rework_bound_takes_precedence_over_created_at(sentinel, tmp_path):
    """[2d] 对照：有 rework_started 时沿用既有 rework 时间界限，行为不变。"""
    clone, _ = _make_clone(tmp_path, [(10_000.0, "base")])
    now = time.time()
    verdict_dir = clone / ".herdr"
    verdict_dir.mkdir(exist_ok=True)
    verdict_path = verdict_dir / "gate-verdict.json"
    verdict_path.write_text(json.dumps({"verdict": "pass"}), encoding="utf-8")
    stamp = now - 1000.0
    os.utime(verdict_path, (stamp, stamp))
    task = _task(clone, created_at=now - 2000.0, rework_started_at=now - 900.0)
    assert sentinel._task_has_valid_gate_verdict(task)[0] is False
