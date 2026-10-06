#!/usr/bin/env python3
"""pane 回收前必须把 scrollback 归档为可审计证据（tech-debt #7）。

现场缺陷：review-auto-r5 的 pane 被回收后，四项输出全文无法取证，
只剩 S6 摘要。修复：reap apply 在 close 前用注入的 capture transport
抓全文归档到 evidence 根目录，按 HERDR_TRANSCRIPT_RETENTION_DAYS
（默认 14 天）有界清理；归档失败不阻断回收。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from herdr.task_resources import (  # noqa: E402
    archive_pane_transcript,
    reap_task_pane,
)
from herdr import kernel  # noqa: E402
from tests.test_pane_lifecycle_capacity import (  # noqa: E402
    resource_scene,  # noqa: F401
    probe,
)


class ArchivePaneTranscriptTest:
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
        Path(old).utime((now - 30 * 86400, now - 30 * 86400))
        monkeypatch.setenv("HERDR_TRANSCRIPT_RETENTION_DAYS", "14")
        archive_pane_transcript("t", "p", "newest", root=tmp_path, now=now)
        assert not Path(old).exists()
        assert Path(fresh).exists()

    def test_retention_env_override_keeps_longer(self, tmp_path, monkeypatch):
        now = 1_700_000_000.0
        monkeypatch.setenv("HERDR_TRANSCRIPT_RETENTION_DAYS", "90")
        old = archive_pane_transcript("t", "p", "old", root=tmp_path, now=now - 30 * 86400)
        Path(old).utime((now - 30 * 86400, now - 30 * 86400))
        archive_pane_transcript("t", "p", "newest", root=tmp_path, now=now)
        assert Path(old).exists(), "90 天保留期内不得清理 30 天的归档"


class ReapTranscriptWiringTest:
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
