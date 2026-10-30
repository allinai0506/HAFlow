"""Regression test for wf-haflow-1029-02 Fix 5:
preserve_clone excludes .git internal object databases from archive manifest and tarball,
preventing 900s close-workflow timeouts over dozens of task clones.
"""

import tarfile
from pathlib import Path
from herdr import clone_archive


def test_preserve_clone_bypasses_git_dir(tmp_path):
    clone = tmp_path / "clones" / "task-clone-1"
    clone.mkdir(parents=True)
    # Write a deliverable
    (clone / "report.md").write_text("# Deliverable Report\nAll tests passed.\n")
    # Write a heavy fake .git directory with objects
    git_dir = clone / ".git" / "objects" / "pack"
    git_dir.mkdir(parents=True)
    (git_dir / "pack-fake.pack").write_bytes(b"large binary pack data" * 1000)

    task = {
        "task_id": "t-archive-test",
        "workflow_id": "wf-arch-test",
        "clone_path": str(clone),
    }

    archive = clone_archive.preserve_clone(task, tmp_path / "clones")

    assert archive["path"]
    with tarfile.open(archive["path"], "r:gz") as tar:
        names = tar.getnames()
        assert "workspace/report.md" in names
        assert not any(".git" in name for name in names), f"Found .git in archive: {names}"
