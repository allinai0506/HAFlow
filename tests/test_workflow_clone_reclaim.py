"""Workflow close must preserve output before deleting managed clones."""

import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from herdr.state_store import SQLiteStateStore

ROOT = Path(__file__).resolve().parents[1]


def scene(tmp_path, monkeypatch, *, status="cleaned", mode="context"):
    home = tmp_path / "home"
    clone = home / ".herdr-controller/clones/t"
    clone.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERDR_CLONES_DIR", str(clone.parent))
    monkeypatch.setenv(
        "HERDR_WORKFLOW_DOCS_DIR", str(home / ".herdr-controller/workflows")
    )
    (clone / "report.md").write_text("valuable result\n")
    if mode == "git":
        for args in [
            ("init",),
            ("config", "user.name", "Test"),
            ("config", "user.email", "t@example.com"),
            ("add", "."),
            ("commit", "-m", "output"),
        ]:
            subprocess.run(
                ["git", "-C", str(clone), *args], check=True, capture_output=True
            )
    (clone / ".herdr-launch-identity.json").write_text(
        json.dumps({"task_id": "t", "run_id": "r"})
    )
    store = SQLiteStateStore(tmp_path / "state.db")
    store.save_workflow({"workflow_id": "wf", "status": "running", "project_id": "p"})
    store.save_task(
        {
            "task_id": "t",
            "workflow_id": "wf",
            "run_id": "r",
            "node": "docs",
            "status": status,
            "clone_path": str(clone),
            "execution_mode": mode,
            "integration_mode": "none",
            "artifacts": ["report.md"],
        }
    )
    env = dict(
        os.environ,
        HOME=str(home),
        HERDR_STATE_DB=str(store.db_path),
        TASKS_FILE=str(tmp_path / "tasks.json"),
        WORKFLOWS_FILE=str(tmp_path / "workflows.json"),
        STAGE_STATE_FILE=str(tmp_path / "stages.json"),
    )
    return store, clone, env


def close(env, *args):
    return subprocess.run(
        [sys.executable, str(ROOT / "bin/herdr-task"), "close-workflow", "wf", *args],
        env=env,
        check=False,
        text=True,
        capture_output=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    "status,mode",
    [
        ("cleaned", "context"),
        ("completed", "context"),
        ("failed", "context"),
        ("cleaned", "git"),
    ],
)
def test_close_archives_deletes_and_keeps_deliverable_readable(
    tmp_path, monkeypatch, status, mode
):
    store, clone, env = scene(tmp_path, monkeypatch, status=status, mode=mode)
    result = close(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not clone.exists(), "workflow close left a complete sandbox behind"
    task = store.get_task("t")
    archive = task["clone_archive"]
    with tarfile.open(archive["path"], "r:gz") as tar:
        assert tar.extractfile("workspace/report.md").read() == b"valuable result\n"
    assert task["clone_retained"] is False
    assert task["status"] == ("failed" if status == "failed" else "cleaned")
    from herdr.deliverables import project_for_tasks, read_deliverable

    rows = project_for_tasks("wf", [task])["items"]
    report = next(row for row in rows if row.get("rel_path") == "report.md")
    assert report["status"] == "present" and report["readable"]
    assert (
        read_deliverable("wf", [task], [], "t", report["id"], download=True)["body"]
        == b"valuable result\n"
    )
    assert close(env).returncode == 0
    assert store.get_task("t")["clone_archive"] == archive


def test_dry_run_does_not_archive_or_delete(tmp_path, monkeypatch):
    store, clone, env = scene(tmp_path, monkeypatch)
    result = close(env, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert clone.exists() and not store.get_task("t").get("clone_archive")
    assert store.get_workflow("wf")["status"] == "running"


def test_foreign_launch_identity_blocks_before_delete(tmp_path, monkeypatch):
    store, clone, env = scene(tmp_path, monkeypatch)
    (clone / ".herdr-launch-identity.json").write_text('{"task_id":"other"}')
    result = close(env)
    assert result.returncode != 0
    assert clone.exists() and store.get_workflow("wf")["status"] != "completed"


def load_cli(monkeypatch, store, clone):
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.SourceFileLoader(
        "reclaim_cli", str(ROOT / "bin/herdr-task")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    monkeypatch.setattr(module, "_get_store", lambda: store)
    monkeypatch.setattr(module, "load_tasks", lambda: {"tasks": store.list_tasks()})
    monkeypatch.setattr(module, "CLONE_ROOT", str(clone.parent.resolve()))
    monkeypatch.setattr(module, "project_for_workflow", lambda _: None)
    monkeypatch.setattr(module, "stage_reset", lambda _: None)
    return module


def test_archive_budget_failure_retains_clone_and_workflow(tmp_path, monkeypatch):
    from herdr import clone_archive

    store, clone, _ = scene(tmp_path, monkeypatch)
    cli = load_cli(monkeypatch, store, clone)
    monkeypatch.setattr(clone_archive, "MAX_BYTES", 4)
    with pytest.raises(SystemExit) as exc:
        cli.close_workflow("wf")
    assert exc.value.code == 2
    assert clone.exists() and store.get_workflow("wf")["status"] == "running"
    assert not store.get_task("t").get("clone_archive")


def test_changed_output_after_archive_prevents_deletion(tmp_path, monkeypatch):
    store, clone, _ = scene(tmp_path, monkeypatch)
    cli = load_cli(monkeypatch, store, clone)
    finalize = cli._finalize_one

    def interleave(task, **kwargs):
        (clone / "report.md").write_text("new result")
        return finalize(task, **kwargs)

    monkeypatch.setattr(cli, "_finalize_one", interleave)
    with pytest.raises(SystemExit):
        cli.close_workflow("wf")
    assert clone.exists() and (clone / "report.md").read_text() == "new result"
    assert store.get_workflow("wf")["status"] != "completed"
    assert store.list_events(event_type="teardown_output_retained")


def test_delete_failure_does_not_complete_and_retry_succeeds(tmp_path, monkeypatch):
    store, clone, _ = scene(tmp_path, monkeypatch)
    cli = load_cli(monkeypatch, store, clone)
    delete = cli.delete_clone_safely
    monkeypatch.setattr(cli, "delete_clone_safely", lambda _: False)
    with pytest.raises(SystemExit):
        cli.close_workflow("wf")
    assert clone.exists() and store.get_workflow("wf")["status"] != "completed"
    monkeypatch.setattr(cli, "delete_clone_safely", delete)
    assert cli.close_workflow("wf")["close_state"] == "completed"
    assert not clone.exists()


def test_archive_never_follows_external_symlink_or_serves_secret(tmp_path, monkeypatch):
    store, clone, env = scene(tmp_path, monkeypatch)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret")
    (clone / "linked.md").symlink_to(outside)
    (clone / ".env").write_text("private credential")
    assert close(env).returncode == 0
    task = store.get_task("t")
    from herdr.clone_archive import read_archived_file

    with pytest.raises(ValueError):
        read_archived_file(task, "linked.md", 1024)
    from herdr.deliverables import project_for_tasks

    names = {r["name"] for r in project_for_tasks("wf", [task])["items"]}
    assert ".env" not in names and "linked.md" not in names
    assert outside.read_text() == "outside secret"


def test_corrupt_archive_cannot_be_served(tmp_path, monkeypatch):
    store, _clone, env = scene(tmp_path, monkeypatch)
    assert close(env).returncode == 0
    task = store.get_task("t")
    Path(task["clone_archive"]["path"]).write_bytes(b"corrupt")
    from herdr.deliverables import project_for_tasks

    result = project_for_tasks("wf", [task])
    assert result.get("error") and not result["items"]


def test_archive_from_another_run_is_rejected(tmp_path, monkeypatch):
    store, _clone, env = scene(tmp_path, monkeypatch)
    assert close(env).returncode == 0
    task = store.get_task("t")
    task["run_id"] = "different"
    from herdr.clone_archive import archive_manifest

    with pytest.raises(ValueError, match="ownership"):
        archive_manifest(task)


@pytest.mark.parametrize("partial", ["report", "git-and-identity"])
def test_partial_delete_retry_keeps_original_archive_and_file(
    tmp_path, monkeypatch, partial
):
    store, clone, _ = scene(
        tmp_path,
        monkeypatch,
        mode="git" if partial == "git-and-identity" else "context",
    )
    cli = load_cli(monkeypatch, store, clone)
    delete = cli.delete_clone_safely

    def partial_failure(_):
        (clone / "report.md").unlink()
        if partial == "git-and-identity":
            import shutil

            shutil.rmtree(clone / ".git")
            (clone / ".herdr-launch-identity.json").unlink()
        raise OSError("disk I/O interruption after partial deletion")

    monkeypatch.setattr(cli, "delete_clone_safely", partial_failure)
    with pytest.raises(OSError):
        cli.close_workflow("wf")
    original = store.get_task("t")["clone_archive"]
    assert clone.exists() and not (clone / "report.md").exists()
    monkeypatch.setattr(cli, "delete_clone_safely", delete)
    cli.close_workflow("wf")
    task = store.get_task("t")
    assert task["clone_archive"] == original and not clone.exists()
    from herdr.deliverables import project_for_tasks, read_deliverable

    item = next(
        r
        for r in project_for_tasks("wf", [task])["items"]
        if r.get("rel_path") == "report.md"
    )
    assert (
        read_deliverable("wf", [task], [], "t", item["id"], download=True)["body"]
        == b"valuable result\n"
    )


def test_controller_auto_close_runs_real_cli_and_reclaims(tmp_path, monkeypatch):
    import importlib
    import threading

    store, clone, env = scene(tmp_path, monkeypatch)
    controller = importlib.import_module("services.herdr-controller")
    monkeypatch.setattr(controller, "_get_store", lambda: store)
    monkeypatch.setattr(controller, "load_tasks", lambda: store.list_tasks())
    monkeypatch.setattr(controller, "_workflow_close_inflight", set())
    monkeypatch.setattr(controller.liveness, "workflow_is_foreign", lambda _: False)
    monkeypatch.setattr(controller, "attention_blocks_retry", lambda _: False)
    monkeypatch.setattr(controller, "attention_clear", lambda _: None)
    monkeypatch.setattr(controller, "TASK_MANAGER", str(ROOT / "bin/herdr-task"))
    run = subprocess.run
    finished = threading.Event()
    results = []

    def transport(argv, **kwargs):
        assert argv == [str(ROOT / "bin/herdr-task"), "close-workflow", "wf"]
        try:
            result = run([sys.executable, *argv], env=env, check=False, **kwargs)
            results.append(result)
            return result
        finally:
            finished.set()

    monkeypatch.setattr(controller.subprocess, "run", transport)
    controller.maybe_close_completed_workflow("wf")
    assert finished.wait(15), "Controller auto-close did not return"
    assert results and results[0].returncode == 0, results[0].stdout + results[0].stderr
    assert not clone.exists()
    assert store.get_workflow("wf")["status"] == "completed"
    task = store.get_task("t")
    from herdr.clone_archive import read_archived_file

    assert read_archived_file(task, "report.md", 1024) == b"valuable result\n"
