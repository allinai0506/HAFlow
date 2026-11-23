"""Regression tests for P2 clone_archive .gitignore-aware archiving."""
import subprocess
from pathlib import Path

from herdr import clone_archive


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text("reports/\nperf/\n*.log\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", "src/app.py")
    _git(repo, "commit", "-m", "init")


def test_clone_archive_skips_untracked_gitignored_dirs_and_files(tmp_path, monkeypatch):
    clones_dir = tmp_path / "clones"
    repo = clones_dir / "task-p2"
    _init_repo(repo)

    # Large untracked gitignored directories (like NexusArchive reports/ & perf/) and ignored file
    (repo / "reports").mkdir()
    (repo / "reports" / "big-dump.bin").write_bytes(b"x" * 200_000)
    (repo / "perf").mkdir()
    (repo / "perf" / "trace.bin").write_bytes(b"y" * 200_000)
    (repo / "debug.log").write_text("verbose log\n", encoding="utf-8")
    # Non-ignored untracked file must still be archived
    (repo / "notes.md").write_text("# Keep me\n", encoding="utf-8")

    monkeypatch.setenv("HERDR_WORKFLOW_DOCS_DIR", str(tmp_path / "docs"))
    # Tighten MAX_BYTES so gitignored bloat (400KB) would fail if not skipped
    monkeypatch.setattr(clone_archive, "MAX_BYTES", 150_000)

    task = {
        "task_id": "task-p2",
        "workflow_id": "wf-p2",
        "run_id": "run-p2",
        "clone_path": str(repo),
        "workspace": str(repo),
    }

    plan = clone_archive.plan_clone(task, clones_dir)
    assert plan["archive_required"] is True

    archive = clone_archive.preserve_clone(task, clones_dir)
    manifest = clone_archive.archive_manifest(dict(task, clone_archive=archive))
    entries = set(manifest["entries"].keys())

    assert "src/app.py" in entries
    assert "notes.md" in entries
    assert not any(k == "reports" or k.startswith("reports/") for k in entries)
    assert not any(k == "perf" or k.startswith("perf/") for k in entries)
    assert "debug.log" not in entries

    # Immediate verify_source must succeed cleanly
    clone_archive.verify_source(task, clones_dir, archive)


def test_clone_archive_preserves_tracked_file_and_declared_deliverable_inside_gitignored_dir(tmp_path, monkeypatch):
    clones_dir = tmp_path / "clones"
    repo = clones_dir / "task-p2-decl"
    _init_repo(repo)

    # 1. Force-tracked file inside an ignored directory MUST still be archived
    (repo / "reports").mkdir()
    (repo / "reports" / "tracked.md").write_text("tracked\n", encoding="utf-8")
    _git(repo, "add", "-f", "reports/tracked.md")
    _git(repo, "commit", "-m", "track report")

    # 2. Untracked gitignored sibling (skipped) vs explicitly declared deliverable (preserved)
    (repo / "reports" / "noise.bin").write_bytes(b"z" * 1024)
    (repo / "reports" / "deliverable.md").write_text("# Declared\n", encoding="utf-8")

    monkeypatch.setenv("HERDR_WORKFLOW_DOCS_DIR", str(tmp_path / "docs"))
    task = {
        "task_id": "task-p2-decl",
        "workflow_id": "wf-p2-decl",
        "run_id": "run-p2-decl",
        "clone_path": str(repo),
        "workspace": str(repo),
        "artifacts": ["reports/deliverable.md"],
    }

    archive = clone_archive.preserve_clone(task, clones_dir)
    manifest = clone_archive.archive_manifest(dict(task, clone_archive=archive))
    entries = set(manifest["entries"].keys())

    assert "reports/tracked.md" in entries
    assert "reports/deliverable.md" in entries
    assert "reports/noise.bin" not in entries
    clone_archive.verify_source(task, clones_dir, archive)


def test_clone_archive_blocks_gitdir_file_external_excludesfile_and_bounds_stdout(tmp_path, monkeypatch):
    # 1. Regular .git file (gitdir redirection) must be rejected
    fake_wt = tmp_path / "fake-wt"
    fake_wt.mkdir()
    (fake_wt / ".git").write_text("gitdir: /nonexistent\n", encoding="utf-8")
    assert clone_archive._git_ignored_rel_paths(fake_wt) == (frozenset(), frozenset())

    # 2. Agent-planted core.excludesFile pointing to an external '*' file must NOT hide untracked work
    repo = tmp_path / "repo-sec"
    _init_repo(repo)
    external_ignore = tmp_path / "evil-excludes"
    external_ignore.write_text("*\n", encoding="utf-8")
    _git(repo, "config", "core.excludesFile", str(external_ignore))
    (repo / "important_untracked.md").write_text("keep\n", encoding="utf-8")
    (repo / "reports").mkdir()
    (repo / "reports" / "ignored.bin").write_bytes(b"a" * 16)

    ignored_dirs, ignored_files = clone_archive._git_ignored_rel_paths(repo)
    assert "reports" in ignored_dirs
    assert "important_untracked.md" not in ignored_files

    # 3. Byte cap fail-safe returns empty sets instead of buffering unbounded stdout
    monkeypatch.setattr(clone_archive, "MAX_GIT_IGNORE_BYTES", 4)
    assert clone_archive._git_ignored_rel_paths(repo) == (frozenset(), frozenset())


def test_clone_archive_preserves_descendants_of_declared_directory(tmp_path, monkeypatch):
    """Codex P1: declaring a directory (e.g. artifacts=['reports']) must protect its descendants too."""
    clones_dir = tmp_path / "clones"
    repo = clones_dir / "task-p2-dir-decl"
    _init_repo(repo)
    (repo / "reports" / "nested").mkdir(parents=True)
    (repo / "reports" / "result.txt").write_text("deliverable\n", encoding="utf-8")
    (repo / "reports" / "nested" / "detail.json").write_text("{}\n", encoding="utf-8")
    (repo / "perf").mkdir()
    (repo / "perf" / "ignored.bin").write_bytes(b"p" * 128)

    monkeypatch.setenv("HERDR_WORKFLOW_DOCS_DIR", str(tmp_path / "docs"))
    task = {
        "task_id": "task-p2-dir-decl",
        "workflow_id": "wf-p2-dir-decl",
        "run_id": "run-p2-dir-decl",
        "clone_path": str(repo),
        "workspace": str(repo),
        "artifacts": ["reports"],
    }

    archive = clone_archive.preserve_clone(task, clones_dir)
    manifest = clone_archive.archive_manifest(dict(task, clone_archive=archive))
    entries = set(manifest["entries"].keys())
    assert "reports" in entries
    assert "reports/result.txt" in entries
    assert "reports/nested/detail.json" in entries
    assert "perf/ignored.bin" not in entries
    clone_archive.verify_source(task, clones_dir, archive)


def test_clone_archive_verify_source_survives_interrupted_deletion_after_git_removed(tmp_path, monkeypatch):
    """Codex P2: interrupted deletion after .git/.gitignore removal must still honor persisted exclusions."""
    import shutil

    clones_dir = tmp_path / "clones"
    repo = clones_dir / "task-p2-interrupt"
    _init_repo(repo)
    (repo / "reports").mkdir()
    (repo / "reports" / "big.bin").write_bytes(b"x" * 200_000)

    monkeypatch.setenv("HERDR_WORKFLOW_DOCS_DIR", str(tmp_path / "docs"))
    monkeypatch.setattr(clone_archive, "MAX_BYTES", 150_000)
    task = {
        "task_id": "task-p2-interrupt",
        "workflow_id": "wf-p2-interrupt",
        "run_id": "run-p2-interrupt",
        "clone_path": str(repo),
        "workspace": str(repo),
    }

    archive = clone_archive.preserve_clone(task, clones_dir)
    # Simulate interrupted reclaim: .git and .gitignore deleted, reports/ still on disk
    shutil.rmtree(repo / ".git")
    (repo / ".gitignore").unlink()
    reclaim_task = dict(task, clone_reclaim_started=archive["manifest_sha256"])
    # Must NOT raise 'archive byte budget exceeded' or 'clone output changed after archive'
    clone_archive.verify_source(reclaim_task, clones_dir, archive)
