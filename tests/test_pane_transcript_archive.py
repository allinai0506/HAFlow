#!/usr/bin/env python3
"""pane 回收前必须把 scrollback 归档为可审计证据（tech-debt #7）。

现场缺陷一：review-auto-r5 的 pane 被回收后，四项输出全文无法取证，
只剩 S6 摘要。修复：reap apply 在 close 前用注入的 capture transport
抓全文归档到 evidence 根目录，按 HERDR_TRANSCRIPT_RETENTION_DAYS
（默认 14 天）有界清理；归档失败不阻断回收。

现场缺陷二（#159 复审）：capture 传输误用 `tmux capture-pane` 抓取
Herdr Pane ID——它不是 tmux 目标，实测回收成功、归档失败、无归档文件。
传输层必须走 Herdr 读取（与 dump_transcript 同参）。本文件两个测试类
曾因类名不以 Test 开头从未进入默认收集，其中两例还调用了不存在的
Path.utime；一并修正，并补 cmd_reap → 真实 capture 传输的接线回归。
"""

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from herdr.task_resources import (  # noqa: E402
    archive_pane_transcript,
    reap_task_pane,
)
from herdr import kernel, task_resources  # noqa: E402
from tests.test_pane_lifecycle_capacity import (  # noqa: E402
    resource_scene,  # noqa: F401
    probe,
)
from tests.test_fix_loop_pr1 import _load_module  # noqa: E402

_cli = _load_module("reap_transcript_cli", ROOT / "bin" / "herdr-task")


class TestArchivePaneTranscript:
    def test_writes_header_and_body(self, tmp_path):
        now = 1_700_000_000.0
        path = archive_pane_transcript(
            "review-auto-r5", "w2:p9", "output line\n".join(["", "", ""]),
            root=tmp_path, now=now)
        assert path and Path(path).exists()
        body = Path(path).read_text()
        assert "pane: w2:p9" in body
        assert "output line" in body

    def test_empty_text_archives_nothing(self, tmp_path):
        assert archive_pane_transcript("t", "p", "", root=tmp_path) is None
        assert archive_pane_transcript("t", "p", None, root=tmp_path) is None

    def test_retention_prunes_expired_archives(self, tmp_path, monkeypatch):
        now = 1_700_000_000.0
        old = archive_pane_transcript("t", "p", "old", root=tmp_path, now=now - 30 * 86400)
        fresh = archive_pane_transcript("t", "p", "fresh", root=tmp_path, now=now)
        assert old and fresh
        os.utime(old, (now - 30 * 86400, now - 30 * 86400))
        monkeypatch.setenv("HERDR_TRANSCRIPT_RETENTION_DAYS", "14")
        archive_pane_transcript("t", "p", "newest", root=tmp_path, now=now)
        assert not Path(old).exists()
        assert Path(fresh).exists()

    def test_retention_env_override_keeps_longer(self, tmp_path, monkeypatch):
        now = 1_700_000_000.0
        monkeypatch.setenv("HERDR_TRANSCRIPT_RETENTION_DAYS", "90")
        old = archive_pane_transcript("t", "p", "old", root=tmp_path, now=now - 30 * 86400)
        os.utime(old, (now - 30 * 86400, now - 30 * 86400))
        archive_pane_transcript("t", "p", "newest", root=tmp_path, now=now)
        assert Path(old).exists(), "90 天保留期内不得清理 30 天的归档"


class TestReapTranscriptWiring:
    def test_reap_archives_transcript_before_close(self, resource_scene, tmp_path):
        kernel.transition_task('task', 'superseded', reason="fixture", store=resource_scene)
        calls = []
        closed = []
        captures = iter(["full scrollback text"])
        result = reap_task_pane(
            resource_scene, 'task', apply=True, probe=probe,
            close=lambda pid: closed.append(pid) or True,
            capture=lambda pid: calls.append(pid) or next(captures),
            transcript_root=tmp_path)
        assert result['action'] == 'released'
        assert result['transcript_archived'] is True
        assert calls == ['pane'], "必须在 close 之前 capture"
        assert closed == ['pane']
        archived = list((tmp_path / 'task').glob('pane-transcript-*.txt'))
        assert len(archived) == 1
        assert "full scrollback text" in archived[0].read_text()

    def test_capture_failure_does_not_block_reclaim(self, resource_scene, tmp_path):
        kernel.transition_task('task', 'superseded', reason="fixture", store=resource_scene)
        closed = []

        def boom(_pane):
            raise RuntimeError("pane vanished mid-capture")

        result = reap_task_pane(
            resource_scene, 'task', apply=True, probe=probe,
            close=lambda pid: closed.append(pid) or True,
            capture=boom, transcript_root=tmp_path)
        assert result['action'] == 'released'
        assert result['transcript_archived'] is False
        assert closed == ['pane']

    def test_dry_run_does_not_capture(self, resource_scene, tmp_path):
        kernel.transition_task('task', 'superseded', reason="fixture", store=resource_scene)
        result = reap_task_pane(
            resource_scene, 'task', apply=False, probe=probe,
            capture=lambda pid: pytest.fail("dry-run 不得 capture"),
            transcript_root=tmp_path)
        assert result['action'] == 'would_release'


class TestCapturePaneTranscriptTransport:
    """cmd_reap 的 capture 传输必须是 Herdr pane read，而非 tmux capture-pane。

    Herdr Pane ID（如 w2:p9）不是 tmux 目标：tmux 传输实测归档失败且
    无归档文件。传输层与 dump_transcript 同参，失败返回空串且不阻断回收。
    """

    def _fake_herdr(self, monkeypatch, *, returncode=0, stdout="", error=None):
        calls = []

        def fake(*args):
            calls.append(list(args))
            if error is not None:
                raise error
            return subprocess.CompletedProcess(
                args=["herdr", *args], returncode=returncode,
                stdout=stdout, stderr="")

        monkeypatch.setattr(_cli, "_herdr", fake)
        return calls

    def test_transport_reads_via_herdr_pane_read(self, monkeypatch):
        calls = self._fake_herdr(monkeypatch, stdout="scrollback body")
        assert _cli._capture_pane_transcript("w2:p9") == "scrollback body"
        assert calls == [[
            "pane", "read", "w2:p9",
            "--source", "recent-unwrapped", "--lines", "20000",
        ]]

    def test_transport_failure_returns_empty(self, monkeypatch):
        self._fake_herdr(monkeypatch, returncode=1)
        assert _cli._capture_pane_transcript("w2:p9") == ""

    def test_transport_timeout_returns_empty(self, monkeypatch):
        self._fake_herdr(
            monkeypatch, error=subprocess.TimeoutExpired(cmd="herdr", timeout=20))
        assert _cli._capture_pane_transcript("w2:p9") == ""

    def test_cmd_reap_archives_via_real_capture_transport(
            self, resource_scene, tmp_path, monkeypatch):
        """真实接线：cmd_reap → _capture_pane_transcript（herdr 读取）→ 归档 → close。"""
        kernel.transition_task('task', 'superseded', reason="fixture", store=resource_scene)
        calls = self._fake_herdr(monkeypatch, stdout="archived scrollback")
        monkeypatch.setattr(_cli, "EVIDENCE_ROOT", str(tmp_path))
        monkeypatch.setattr(
            task_resources, "probe_live_runtime",
            lambda task, **kwargs: {"status": "available", "reason": "identity_match"})

        _cli.cmd_reap(types.SimpleNamespace(workflow_id="wf", apply=True))

        archived = list((tmp_path / "task").glob("pane-transcript-*.txt"))
        assert len(archived) == 1, "回收归档必须真实落盘"
        assert "archived scrollback" in archived[0].read_text()
        assert calls[0] == [
            "pane", "read", "pane",
            "--source", "recent-unwrapped", "--lines", "20000",
        ], "必须在 close 之前经 herdr pane read 抓取"
        assert ["pane", "close", "pane"] in calls
