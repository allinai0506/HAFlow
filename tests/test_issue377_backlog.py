"""issue #377 backlog 四条 P2 的回归守卫。

1. collect_and_sync_lessons：已上传（status='uploaded'）的候选不再重复
   提交；close 路径 local-only 模式（sync_to_hub=False）不做同步 Hub 上传。
2. close 的经验同步入口尊重 HERDR_EXPERIENCE_COLLECTION_DISABLED。
3. close 的经验同步把 store 的 db_path 传入 collect/后台 drain
   （WORKFLOW_FILE 选库场景不再走默认库）。
4. sentinel 交付证据改用有界 scrollback（recent-unwrapped）——初始
   prompt 滚出可见屏后 marker 仍可发现，长任务宽限不再丢失。
"""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from herdr.state_store import SQLiteStateStore

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- lessons

@pytest.fixture
def collector_db(tmp_path):
    return tmp_path / "collector.db"


class _HubStub:
    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])

    def submit_candidate(self, exp):
        self.calls.append(exp.experience_id)
        if self.results:
            return self.results.pop(0)
        return {"ok": True}


def _lessons_file(tmp_path):
    p = tmp_path / "lessons-learned.md"
    p.write_text("placeholder\n", encoding="utf-8")
    return p


class _FakeSource:
    def __init__(self, task_id, run_id):
        self.task_id = task_id
        self.run_id = run_id


class _FakeExp:
    def __init__(self, index):
        self.experience_id = f"exp-{index}"
        self.source = _FakeSource(f"t-{index}", f"run-{index}")

    def to_dict(self):
        return {"experience_id": self.experience_id}


def _stub_extractor(monkeypatch, n=2):
    """提取器格式易变：测试直接固定候选产出，聚焦去重/本地化逻辑。"""
    import herdr.experience.extractor as extractor_mod
    exps = [_FakeExp(i) for i in range(1, n + 1)]
    monkeypatch.setattr(extractor_mod, "extract_candidates_from_lessons",
                        lambda content, contributor=None, min_lesson_num=160: exps)
    return exps


def _seed_uploaded(collector_db, task_id, run_id):
    conn = sqlite3.connect(collector_db)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS local_collector_records (
                task_id TEXT, run_id TEXT, outcome_id INTEGER, experience_id TEXT,
                status TEXT, candidate_json TEXT, created_at REAL, updated_at REAL,
                PRIMARY KEY (task_id, run_id))
        """)
        conn.execute(
            "INSERT OR REPLACE INTO local_collector_records VALUES (?,?,NULL,?,'uploaded','{}',1,1)",
            (task_id, run_id))
        conn.commit()
    finally:
        conn.close()


def test_collect_lessons_skips_already_uploaded_rows(tmp_path, collector_db, monkeypatch):
    """Codex P2：已上传行的候选不得重复提交（每次调用 50 requests 的根因）。"""
    from herdr.experience.collector import ExperienceCollector
    _stub_extractor(monkeypatch, n=2)
    lessons = _lessons_file(tmp_path)
    hub = _HubStub(results=[{"ok": True}, {"ok": True}])
    collector = ExperienceCollector(db_path=collector_db, hub_client=hub)
    first = collector.collect_and_sync_lessons(lessons_path=lessons)
    assert first["ok"] is True and first["uploaded"] == 2
    assert len(hub.calls) == 2

    # 第二次调用：两行已是 uploaded → 零提交
    second = collector.collect_and_sync_lessons(lessons_path=lessons)
    assert second["ok"] is True and second.get("skipped") == 2
    assert len(hub.calls) == 2, "已上传行被重复提交"


def test_collect_lessons_local_only_mode_persists_without_hub_calls(tmp_path, collector_db, monkeypatch):
    """Codex P2：close 路径 sync_to_hub=False 只本地持久化 pending，不同步上传。"""
    from herdr.experience.collector import ExperienceCollector
    _stub_extractor(monkeypatch, n=2)
    lessons = _lessons_file(tmp_path)
    hub = _HubStub()
    hub.calls = []
    collector = ExperienceCollector(db_path=collector_db, hub_client=hub)
    result = collector.collect_and_sync_lessons(lessons_path=lessons, sync_to_hub=False)
    assert result["ok"] is True
    assert hub.calls == [], "local-only 模式不得发起 Hub 提交"
    conn = sqlite3.connect(collector_db)
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM local_collector_records GROUP BY status"
        ).fetchall()
    finally:
        conn.close()
    statuses = dict(rows)
    assert statuses.get("pending_upload", 0) == 2


# ---------------------------------------------------------------- close 入口

def _close_deps(store):
    return {"get_store": lambda: store}


def test_post_close_experience_sync_honors_opt_out(tmp_path, monkeypatch):
    """Codex P2：HERDR_EXPERIENCE_COLLECTION_DISABLED=1 时 close 不得触发采集。"""
    from herdr import task_cli_workflow_close as close_mod
    store = SQLiteStateStore(tmp_path / "state.db")
    store.save_workflow({"workflow_id": "wf-x", "status": "completed"})
    monkeypatch.setenv("HERDR_EXPERIENCE_COLLECTION_DISABLED", "1")
    monkeypatch.setattr(close_mod, "_experience_collector_factory",
                        lambda **kw: (_ for _ in ()).throw(
                            AssertionError("opt-out 下不得构造 collector")))
    close_mod._post_close_experience_sync("wf-x", store.db_path)


def test_post_close_experience_sync_keeps_collector_db_default_and_drains(tmp_path, monkeypatch):
    """Codex 三轮 P1×2：close 期记录必须落在默认 collector 库（recovery
    drain 的同一个库），workflow state db 只作任务事实源；已认证 Hub
    client 透传给后台 drain（token-file 凭据场景不静默 no_hub_client）。"""
    from herdr import task_cli_workflow_close as close_mod
    store = SQLiteStateStore(tmp_path / "state.db")
    store.save_workflow({"workflow_id": "wf-x", "status": "completed"})
    monkeypatch.delenv("HERDR_EXPERIENCE_COLLECTION_DISABLED", raising=False)

    recorded = {}

    class _FakeCollector:
        def __init__(self, db_path=None, hub_client=None):
            recorded["collector_db"] = db_path      # 必须保持默认（None）
            recorded["collector_hub"] = hub_client
            self.hub_client = hub_client

        def collect_and_sync_lessons(self, lessons_path=None, sync_to_hub=True):
            recorded["lessons_sync_to_hub"] = sync_to_hub
            return {"ok": True}

        def collect_workflow_tasks(self, workflow_id, state_db_path=None):
            recorded["tasks_wf"] = workflow_id
            recorded["tasks_db"] = state_db_path    # 任务事实源 = workflow 库
            return {"ok": True}

    monkeypatch.setattr(close_mod, "_experience_collector_factory",
                        lambda **kw: _FakeCollector(**kw))
    drained = []
    monkeypatch.setattr(close_mod, "_experience_background_drain",
                        lambda state_db_path=None, hub_client=None:
                            drained.append((state_db_path, hub_client)))
    close_mod._post_close_experience_sync("wf-x", store.db_path)
    # 任务事实源绑定 close 所用库
    assert recorded["tasks_db"] == store.db_path
    assert recorded["tasks_wf"] == "wf-x"
    assert recorded["lessons_sync_to_hub"] is False, "close 路径不得同步上传"
    # collector 记录库不得被误写成 workflow 库（Codex 三轮 P1）
    assert recorded["collector_db"] is None, "collector 记录库必须保持默认"
    # 已认证 client 透传给 drain；drain 的任务事实源同为 close 库
    assert drained == [(store.db_path, recorded["collector_hub"])]

    # hub client 缺失时透传 None（行为不变）
    recorded.clear(); drained.clear()

    class _NoHubCollector(_FakeCollector):
        def __init__(self, **kw):
            super().__init__(db_path=kw.get("db_path"), hub_client=kw.get("hub_client"))

    monkeypatch.setattr(close_mod, "_experience_collector_factory", lambda **kw: _NoHubCollector(**kw))
    close_mod._post_close_experience_sync("wf-x", store.db_path)
    assert drained == [(store.db_path, None)]


# ---------------------------------------------------------------- sentinel

def test_pane_delivery_evidence_finds_marker_in_scrollback(monkeypatch, tmp_path):
    """Codex P2：初始 prompt 滚出可见屏后，有界 scrollback 仍能发现 marker。"""
    import importlib as _importlib
    sentinel = _importlib.import_module("services.herdr-sentinel")
    marker_text = "HERDR_ORCH_TASK:" + "t-scroll" + " done"

    monkeypatch.setattr(sentinel, "pane_visible", lambda pid: "无关可见内容，marker 已滚出")
    monkeypatch.setattr(sentinel, "pane_recent",
                        lambda pid, lines=2000: f"历史回滚...\n{marker_text}\n...")
    evidence = sentinel._pane_delivery_evidence("pane-1", "t-scroll")
    assert evidence.get("has_marker") is True

    # scrollback 也无 marker → 仍判无标记（不得凭空捏造）
    monkeypatch.setattr(sentinel, "pane_recent", lambda pid, lines=2000: "无标记内容")
    evidence = sentinel._pane_delivery_evidence("pane-1", "t-scroll")
    assert evidence.get("has_marker") is False
