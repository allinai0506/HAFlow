import importlib.machinery
import importlib.util
import os
from pathlib import Path
import subprocess
import pytest

from herdr.state_store import SQLiteStateStore

# Load herdr-worker dynamically
worker_spec = importlib.util.spec_from_file_location("herdr_worker", Path(__file__).resolve().parent.parent / "services" / "herdr-worker.py")
herdr_worker = importlib.util.module_from_spec(worker_spec)
worker_spec.loader.exec_module(herdr_worker)

# Load herdr-task dynamically
task_path = Path(__file__).resolve().parent.parent / "bin" / "herdr-task"
task_loader = importlib.machinery.SourceFileLoader("herdr_task", str(task_path))
task_spec = importlib.util.spec_from_loader("herdr_task", task_loader)
herdr_task = importlib.util.module_from_spec(task_spec)
task_loader.exec_module(herdr_task)


def test_install_worker_sandbox_push_guard(tmp_path: Path):
    clone = tmp_path / "sandbox_clone"
    clone.mkdir()
    subprocess.run(["git", "init", str(clone)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.email", "test@test.com"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "core.hooksPath", ".task-hooks"], check=True)

    remote_repo = tmp_path / "fake_remote.git"
    subprocess.run(["git", "init", "--bare", str(remote_repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "remote", "add", "origin", str(remote_repo)], check=True)

    (clone / "file.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "-C", str(clone), "add", "."], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-m", "initial"], check=True)

    herdr_worker.install_worker_sandbox_push_guard(clone)

    # 1. Verify custom core.hooksPath has pre-push installed
    custom_pre_push = clone / ".task-hooks" / "pre-push"
    assert custom_pre_push.exists()
    assert "[GIT GUARD REJECT]" in custom_pre_push.read_text(encoding="utf-8")

    # 2. Verify pre-push hook installed and executable
    pre_push = clone / ".git" / "hooks" / "pre-push"
    assert pre_push.exists()
    assert os.access(pre_push, os.X_OK)
    content = pre_push.read_text(encoding="utf-8")
    assert "[GIT GUARD REJECT]" in content

    # 3. Verify pushUrl disabled in git config
    push_url = subprocess.run(
        ["git", "-C", str(clone), "config", "remote.origin.pushUrl"],
        capture_output=True, text=True, check=True
    ).stdout.strip()
    assert push_url == "DISABLED_FOR_WORKER_LOCAL_TEST_ONLY"

    # 4. Verify pushing to origin fails via disabled pushUrl
    push_res = subprocess.run(
        ["git", "-C", str(clone), "push", "origin", "HEAD:main"],
        capture_output=True, text=True
    )
    assert push_res.returncode != 0
    assert "DISABLED_FOR_WORKER_LOCAL_TEST_ONLY" in push_res.stderr

    # 5. Verify direct URL push (bypassing origin pushUrl) is blocked by pre-push hook
    push_res_direct = subprocess.run(
        ["git", "-C", str(clone), "push", str(remote_repo), "HEAD:main"],
        capture_output=True, text=True
    )
    assert push_res_direct.returncode != 0
    assert "[GIT GUARD REJECT]" in push_res_direct.stderr

    # 6. Verify git push --no-verify to origin also fails via disabled pushUrl
    push_res_no_verify = subprocess.run(
        ["git", "-C", str(clone), "push", "--no-verify", "origin", "HEAD:main"],
        capture_output=True, text=True
    )
    assert push_res_no_verify.returncode != 0
    assert "DISABLED_FOR_WORKER_LOCAL_TEST_ONLY" in push_res_no_verify.stderr


def test_adopt_head_refuses_when_branch_mismatch(tmp_path: Path, monkeypatch):
    """P1 invariant: Agent committing on a stray/custom branch must fail closed with exit code 4."""
    db_file = tmp_path / "state.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(db_file))
    store = SQLiteStateStore(db_path=db_file)

    clone = tmp_path / "repo"
    clone.mkdir()
    subprocess.run(["git", "init", str(clone)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.email", "test@test.com"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "commit.gpgsign", "false"], check=True)

    (clone / "base.txt").write_text("base", encoding="utf-8")
    subprocess.run(["git", "-C", str(clone), "add", "."], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-m", "base commit"], check=True)
    baseline_commit = subprocess.check_output(
        ["git", "-C", str(clone), "rev-parse", "HEAD"], text=True
    ).strip()

    task_branch = "agent/test/feat-t1"
    subprocess.run(["git", "-C", str(clone), "branch", task_branch], check=True)

    # Worker Agent erroneously switched to its own branch
    custom_branch = "agent/opencode/fix-custom-feature"
    subprocess.run(["git", "-C", str(clone), "checkout", "-b", custom_branch], check=True)
    (clone / "work.txt").write_text("agent work", encoding="utf-8")
    subprocess.run(["git", "-C", str(clone), "add", "."], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-m", "work commit"], check=True)

    task = {
        "task_id": "t1",
        "workflow_id": "wf-test",
        "stage": "impl",
        "status": "completed",
        "branch": task_branch,
        "baseline_commit": baseline_commit,
        "onto_branch": None,
        "created_at": 1000,
    }
    store.save_task(task)

    # Adoption must fail closed (exit code 4: REFUSED / current_branch_mismatch)
    with pytest.raises(SystemExit) as excinfo:
        herdr_task._adopt_head_if_attributable("t1", task, str(clone))
    assert excinfo.value.code == 4

    # Verify task branch was not modified or auto-rebound
    branch_sha = subprocess.check_output(
        ["git", "-C", str(clone), "rev-parse", task_branch], text=True
    ).strip()
    assert branch_sha == baseline_commit


def test_adopt_head_succeeds_on_task_branch(tmp_path: Path, monkeypatch):
    """When Agent works on the designated task branch, adoption succeeds cleanly."""
    db_file = tmp_path / "state_success.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(db_file))
    store = SQLiteStateStore(db_path=db_file)

    clone = tmp_path / "repo_success"
    clone.mkdir()
    subprocess.run(["git", "init", str(clone)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.email", "test@test.com"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "commit.gpgsign", "false"], check=True)

    (clone / "base.txt").write_text("base", encoding="utf-8")
    subprocess.run(["git", "-C", str(clone), "add", "."], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-m", "base commit"], check=True)
    baseline_commit = subprocess.check_output(
        ["git", "-C", str(clone), "rev-parse", "HEAD"], text=True
    ).strip()

    task_branch = "agent/test/feat-t-success"
    subprocess.run(["git", "-C", str(clone), "checkout", "-b", task_branch], check=True)
    (clone / "work.txt").write_text("legitimate agent work", encoding="utf-8")
    subprocess.run(["git", "-C", str(clone), "add", "."], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-m", "work commit"], check=True)
    work_sha = subprocess.check_output(
        ["git", "-C", str(clone), "rev-parse", "HEAD"], text=True
    ).strip()

    task = {
        "task_id": "t-success",
        "workflow_id": "wf-test",
        "stage": "impl",
        "status": "completed",
        "branch": task_branch,
        "baseline_commit": baseline_commit,
        "onto_branch": None,
        "created_at": 1000,
    }
    store.save_task(task)

    herdr_task._adopt_head_if_attributable("t-success", task, str(clone))

    stored = store.get_task("t-success")
    assert stored["status"] == "committed"
    assert stored.get("commit") == work_sha
    assert stored.get("commit_result") == "adopted"


def test_adopt_head_does_not_align_when_head_not_descendant(tmp_path: Path, monkeypatch):
    db_file = tmp_path / "state2.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(db_file))
    store = SQLiteStateStore(db_path=db_file)

    clone = tmp_path / "repo2"
    clone.mkdir()
    subprocess.run(["git", "init", str(clone)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.email", "test@test.com"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "commit.gpgsign", "false"], check=True)

    (clone / "a.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "-C", str(clone), "add", "."], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-m", "commit a"], check=True)
    baseline_commit = subprocess.check_output(
        ["git", "-C", str(clone), "rev-parse", "HEAD"], text=True
    ).strip()

    subprocess.run(["git", "-C", str(clone), "checkout", "--orphan", "orphan_branch"], check=True)
    subprocess.run(["git", "-C", str(clone), "rm", "-rf", "."], check=True)
    (clone / "orphan.txt").write_text("orphan", encoding="utf-8")
    subprocess.run(["git", "-C", str(clone), "add", "."], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-m", "orphan commit"], check=True)

    task_branch = "agent/test/feat-t2"
    subprocess.run(["git", "-C", str(clone), "branch", task_branch, baseline_commit], check=True)

    task = {
        "task_id": "t2",
        "workflow_id": "wf-test",
        "stage": "impl",
        "status": "completed",
        "branch": task_branch,
        "baseline_commit": baseline_commit,
        "onto_branch": None,
        "created_at": 1000,
    }
    store.save_task(task)

    with pytest.raises(SystemExit) as excinfo:
        herdr_task._adopt_head_if_attributable("t2", task, str(clone))
    assert excinfo.value.code == 4  # REFUSED

    branch_sha = subprocess.check_output(
        ["git", "-C", str(clone), "rev-parse", task_branch], text=True
    ).strip()
    assert branch_sha == baseline_commit
