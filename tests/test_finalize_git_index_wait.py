"""Native index contention must wait without exhausting commit failures."""
import importlib.util
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture
def controller(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_ATTENTION_FILE", str(tmp_path / "attention.json"))
    spec = importlib.util.spec_from_file_location("ctrl_git_index_wait", ROOT / "services/herdr-controller.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

@pytest.fixture
def locked_repo(tmp_path):
    repo = tmp_path / "clone"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert git("init").returncode == 0
    git("config", "user.name", "Regression")
    git("config", "user.email", "test@example.invalid")
    (repo / "owned.txt").write_text("baseline\n")
    assert git("add", "owned.txt").returncode == 0
    assert git("commit", "-m", "baseline").returncode == 0
    (repo / "owned.txt").write_text("task WIP\n")
    (repo / ".git/index.lock").write_bytes(b"")
    return repo, git


def task(repo):
    return {"task_id": "t-lock", "workflow_id": "wf-lock", "run_id": "run-lock", "status": "completed", "integration_mode": "git", "clone_path": str(repo)}


def test_native_lock_wait_preserves_budget_and_wip(controller, locked_repo):
    repo, git = locked_repo
    t = task(repo)
    failure = git("add", "-u")
    assert failure.returncode == 128
    # Task CLI currently wraps the native 128 in a CalledProcessError exit 1.
    proc = subprocess.CompletedProcess([], 75, "HERDR_COMMIT_RESULT=" + json.dumps({"task_id": "t-lock", "result": "wait", "reason": "git_index_lock"}), failure.stderr)
    with patch.object(controller, "get_task", return_value=t), patch.object(controller, "workflow_closed", return_value=False), patch.object(controller, "maybe_complete_on_task_done", return_value=[]), patch.object(controller, "ensure_no_git_processes"), patch.object(controller.subprocess, "run", return_value=proc), patch.object(controller, "_escalate_finalize") as escalate:
        for i in range(8):
            now = 1000 + i * 61
            assert controller._check_finalize_retry(t, "completed", now)
            episode = controller.attention_get("t-lock:finalize")
            assert episode["attempts"] == 0
            assert episode["reason"] == "git_index_lock"
            assert episode["next_retry_at"] == now + 60
        escalate.assert_not_called()
    assert (repo / ".git/index.lock").read_bytes() == b""
    assert (repo / "owned.txt").read_text() == "task WIP\n"
    # Removing the trigger in this TEMP repo restores the native add path.
    (repo / ".git/index.lock").unlink()
    assert git("add", "-u").returncode == 0


@pytest.mark.parametrize("stderr", ["fatal: gate failed", "fatal: Unable to create '/different/.git/index.lock': File exists.\n", "fatal: Unable to create '{lock}': Permission denied.\n", "Unable to create '{lock}': File exists.\n"])
def test_unrelated_errors_keep_failure_budget(controller, locked_repo, stderr):
    repo, _ = locked_repo
    t = task(repo)
    proc = subprocess.CompletedProcess([], 1, "", stderr.format(lock=repo / ".git/index.lock"))
    with patch.object(controller, "get_task", return_value=t), patch.object(controller, "workflow_closed", return_value=False), patch.object(controller, "maybe_complete_on_task_done", return_value=[]), patch.object(controller, "ensure_no_git_processes"), patch.object(controller.subprocess, "run", return_value=proc), patch.object(controller, "_record_finalize_event"):
        controller._check_finalize_retry(t, "completed", 1000)
    assert controller.attention_get("t-lock:finalize")["attempts"] == 1


def test_legacy_escalation_is_not_cleared(controller, locked_repo):
    repo, _ = locked_repo
    t = dict(task(repo), finalize_escalated=True, finalize_escalate_reason="retry_exhausted")
    with patch.object(controller, "finalize_completed_task") as finalize:
        assert controller._check_finalize_retry(t, "completed", 1000)
        finalize.assert_not_called()


@pytest.mark.parametrize("linked", [False, True])
def test_native_cli_wait_then_same_task_commit(controller, locked_repo, tmp_path, monkeypatch, linked):
    import os
    import sys
    from herdr.state_store import SQLiteStateStore
    repo, git = locked_repo
    if linked:
        (repo / ".git/index.lock").unlink()
        linked_repo = tmp_path / "linked"
        assert git("worktree", "add", "-b", "task-linked", str(linked_repo)).returncode == 0
        repo = linked_repo
        (repo / "owned.txt").write_text("task WIP\n")
    lock_path = Path(subprocess.run(["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-path", "index.lock"], capture_output=True, text=True, check=True).stdout.strip())
    lock_path.write_bytes(b"native lock sentinel")
    (repo / "new.txt").write_text("new task output\n")
    db = tmp_path / "state.db"
    store = SQLiteStateStore(db)
    t = task(repo)
    t.update(agent="codex", stage="implementation", node="implementation", baseline_untracked=[])
    store.save_task(t)
    env = dict(os.environ, HERDR_STATE_DB=str(db), TASKS_FILE=str(tmp_path / "tasks.json"), HERDR_GIT_LOCK_ROOT=str(tmp_path / "gitlocks"), HERDR_ATTENTION_FILE=str(tmp_path / "attention.json"))
    command = [sys.executable, str(ROOT / "bin/herdr-task"), "commit", "t-lock"]
    before_head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"])
    index = lock_path.with_name("index")
    before_index = index.read_bytes()
    first = subprocess.run(command, env=env, capture_output=True, text=True)
    assert first.returncode == 75, first.stdout + first.stderr
    marker = json.loads(next(s.split("=", 1)[1] for s in first.stdout.splitlines() if s.startswith("HERDR_COMMIT_RESULT=")))
    assert marker == {"task_id": "t-lock", "result": "wait", "reason": "git_index_lock"}
    assert "Traceback" not in first.stderr
    native_run = subprocess.run
    def run_task(command, **kwargs):
        if command[0] == controller.TASK_MANAGER:
            return native_run([sys.executable, *command], **dict(kwargs, env=env))
        return native_run(command, **kwargs)
    with patch.object(controller, "get_task", side_effect=store.get_task), patch.object(controller, "workflow_closed", return_value=False), patch.object(controller, "maybe_complete_on_task_done", return_value=[]), patch.object(controller, "ensure_no_git_processes"), patch.object(controller.subprocess, "run", side_effect=run_task), patch.object(controller, "_escalate_finalize") as escalate:
        for i in range(7):
            assert controller._check_finalize_retry(store.get_task("t-lock"), "completed", 2000 + 61 * i)
        escalate.assert_not_called()
        assert controller.attention_get("t-lock:finalize")["attempts"] == 0
    assert lock_path.read_bytes() == b"native lock sentinel"
    assert index.read_bytes() == before_index
    assert subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"]) == before_head
    assert (repo / "owned.txt").read_text() == "task WIP\n"
    assert (repo / "new.txt").read_text() == "new task output\n"
    lock_path.unlink()  # Only the test fixture releases its own TEMP lock.
    # Let the real controller drive the same Task when its persisted wait is due.
    next_at = controller.attention_get("t-lock:finalize")["next_retry_at"]
    def commit_then_controlled_integrate(command, **kwargs):
        if command[0] == controller.TASK_MANAGER and "commit" in command:
            return native_run([sys.executable, *command], **dict(kwargs, env=env))
        if command[0] == controller.TASK_MANAGER and "integrate" in command:
            return subprocess.CompletedProcess(command, 75, "", "controlled integration busy")
        return native_run(command, **kwargs)
    with patch.object(controller, "get_task", side_effect=store.get_task), patch.object(controller, "workflow_closed", return_value=False), patch.object(controller, "maybe_complete_on_task_done", return_value=[]), patch.object(controller, "ensure_no_git_processes"), patch.object(controller.subprocess, "run", side_effect=commit_then_controlled_integrate), patch.object(controller, "_record_finalize_event"):
        assert not controller._check_finalize_retry(store.get_task("t-lock"), "completed", next_at - 1)
        controller._check_finalize_retry(store.get_task("t-lock"), "completed", next_at)
    assert store.get_task("t-lock")["status"] == "committed"
    assert subprocess.check_output(["git", "-C", str(repo), "show", "HEAD:owned.txt"]).decode() == "task WIP\n"
    assert subprocess.check_output(["git", "-C", str(repo), "show", "HEAD:new.txt"]).decode() == "new task output\n"


@pytest.mark.parametrize("payload", [{}, {"task_id": "other", "result": "wait", "reason": "git_index_lock"}, {"task_id": "t-lock", "result": "wait", "reason": "git_busy"}])
def test_generic_or_foreign_busy_keeps_budget(controller, locked_repo, payload):
    repo, _ = locked_repo
    t = task(repo)
    proc = subprocess.CompletedProcess([], 75, "HERDR_COMMIT_RESULT=" + json.dumps(payload), "")
    with patch.object(controller, "get_task", return_value=t), patch.object(controller, "workflow_closed", return_value=False), patch.object(controller, "maybe_complete_on_task_done", return_value=[]), patch.object(controller, "ensure_no_git_processes"), patch.object(controller.subprocess, "run", return_value=proc):
        controller._check_finalize_retry(t, "completed", 1000)
    assert controller.attention_get("t-lock:finalize")["attempts"] == 1


def test_wait_throttles_and_preserves_prior_error_count(controller, locked_repo):
    repo, _ = locked_repo
    t = task(repo)
    controller.attention_note("t-lock:finalize", t, "finalize", reason="commit_retry", attempts=2)
    outcome = {"retryable": True, "kind": "wait", "reason": "git_index_lock", "budgeted": False}
    with patch.object(controller, "get_task", return_value=t), patch.object(controller, "workflow_closed", return_value=False), patch.object(controller, "finalize_completed_task", return_value=outcome) as finalize:
        controller._check_finalize_retry(t, "completed", 1000)
        assert not controller._check_finalize_retry(t, "completed", 1059)
        finalize.assert_called_once()
        assert controller.attention_get("t-lock:finalize")["attempts"] == 2
        controller._check_finalize_retry(t, "completed", 1060)
        assert finalize.call_count == 2


def test_native_gate_failure_is_not_lock_wait(controller, locked_repo, tmp_path):
    import os
    import sys
    from herdr.state_store import SQLiteStateStore
    repo, _ = locked_repo
    (repo / ".git/index.lock").unlink()
    hook = repo / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\necho 'real gate refusal' >&2\nexit 1\n")
    hook.chmod(0o755)
    store = SQLiteStateStore(tmp_path / "gate.db")
    t = task(repo)
    t.update(agent="codex", stage="implementation", node="implementation", baseline_untracked=[])
    store.save_task(t)
    env = dict(os.environ, HERDR_STATE_DB=str(tmp_path / "gate.db"), TASKS_FILE=str(tmp_path / "gate-tasks.json"), HERDR_GIT_LOCK_ROOT=str(tmp_path / "gate-locks"))
    proc = subprocess.run([sys.executable, str(ROOT / "bin/herdr-task"), "commit", "t-lock"], env=env, text=True, capture_output=True)
    assert proc.returncode == 1
    assert "real gate refusal" in proc.stderr
    assert "git_index_lock" not in proc.stdout
    with patch.object(controller, "get_task", side_effect=store.get_task), patch.object(controller, "workflow_closed", return_value=False), patch.object(controller, "maybe_complete_on_task_done", return_value=[]), patch.object(controller, "ensure_no_git_processes"), patch.object(controller.subprocess, "run", return_value=proc), patch.object(controller, "_record_finalize_event"):
        controller._check_finalize_retry(t, "completed", 1000)
    assert controller.attention_get("t-lock:finalize")["attempts"] == 1


def test_hook_cannot_forge_native_commit_wait(locked_repo):
    import importlib.machinery
    repo, git = locked_repo
    (repo / ".git/index.lock").unlink()
    assert git("add", "-u").returncode == 0
    lock = repo / ".git/index.lock"
    hook = repo / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\necho \"fatal: Unable to create '" + str(lock) + "': File exists.\" >&2\nexit 1\n")
    hook.chmod(0o755)
    spec = importlib.util.spec_from_loader("task_hook_wait", importlib.machinery.SourceFileLoader("task_hook_wait", str(ROOT / "bin/herdr-task")))
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    result = cli._run_commit_index_command("t-lock", str(repo), ["git", "-C", str(repo), "commit", "-m", "blocked"])
    assert result.returncode == 1
    assert not lock.exists()


@pytest.mark.parametrize("changed", [{"run_id": "new-run"}, {"version": 2}, {"status": "committed"}])
def test_old_wait_does_not_clear_new_episode(controller, locked_repo, changed):
    repo, _ = locked_repo
    old = dict(task(repo), version=1)
    fresh = dict(old, **changed)
    def finalize(_):
        controller.attention_note("t-lock:finalize", fresh, "finalize", reason="new_owner_error", attempts=2)
        return {"retryable": True, "kind": "wait", "reason": "git_index_lock", "budgeted": False}
    with patch.object(controller, "get_task", return_value=fresh), patch.object(controller, "workflow_closed", return_value=False), patch.object(controller, "finalize_completed_task", side_effect=finalize):
        controller._check_finalize_retry(old, "completed", 1000)
    episode = controller.attention_get("t-lock:finalize")
    assert episode["reason"] == "new_owner_error"
    assert episode["attempts"] == 2


@pytest.mark.parametrize("relative", [False, True])
def test_alternate_index_lock_uses_native_index_path(locked_repo, tmp_path, monkeypatch, relative):
    import importlib.machinery
    repo, _ = locked_repo
    index = repo / "alternate-index" if relative else tmp_path / "alternate-index"
    index.write_bytes((repo / ".git/index").read_bytes())
    lock = Path(str(index) + ".lock")
    lock.write_bytes(b"alternate index owner")
    monkeypatch.setenv("GIT_INDEX_FILE", "alternate-index" if relative else str(index))
    spec = importlib.util.spec_from_loader("task_alternate_index", importlib.machinery.SourceFileLoader("task_alternate_index", str(ROOT / "bin/herdr-task")))
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    with pytest.raises(SystemExit) as raised:
        cli._run_commit_index_command("t-lock", str(repo), ["git", "-C", str(repo), "add", "-u"])
    assert raised.value.code == 75
    assert lock.read_bytes() == b"alternate index owner"


def test_disappeared_commit_lock_keeps_failure_result(locked_repo, monkeypatch):
    import importlib.machinery
    repo, _ = locked_repo
    lock = repo / ".git/index.lock"
    lock.write_bytes(b"temporary owner")
    spec = importlib.util.spec_from_loader("task_disappeared_lock", importlib.machinery.SourceFileLoader("task_disappeared_lock", str(ROOT / "bin/herdr-task")))
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    native_run = subprocess.run
    def run(command, **kwargs):
        result = native_run(command, **kwargs)
        if len(command) > 3 and command[3] == "commit":
            lock.unlink()  # TEMP fixture release after native failure.
        return result
    monkeypatch.setattr(cli.subprocess, "run", run)
    result = cli._run_commit_index_command("t-lock", str(repo), ["git", "-C", str(repo), "commit", "-m", "blocked"])
    assert result.returncode != 0
    assert not lock.exists()


def test_clean_filter_cannot_forge_native_add_wait(locked_repo):
    import importlib.machinery
    import shlex
    repo, git = locked_repo
    lock = repo / ".git/index.lock"
    lock.unlink()
    script = repo / ".git/filterfail.sh"
    script.write_text("#!/bin/sh\necho \"fatal: Unable to create '" + str(lock) + "': File exists.\" >&2\nexit 1\n")
    (repo / ".git/info/attributes").write_text("owned.txt filter=refuse\n")
    assert git("config", "filter.refuse.clean", "sh " + shlex.quote(str(script))).returncode == 0
    assert git("config", "filter.refuse.required", "true").returncode == 0
    spec = importlib.util.spec_from_loader("task_filter_wait", importlib.machinery.SourceFileLoader("task_filter_wait", str(ROOT / "bin/herdr-task")))
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    result = cli._run_commit_index_command("t-lock", str(repo), ["git", "-C", str(repo), "add", "-u"])
    assert result.returncode != 0
    assert "clean filter" in result.stderr
    assert not lock.exists()


def test_wait_cas_preserves_cross_process_episode(controller, locked_repo):
    import os
    import sys
    repo, _ = locked_repo
    t = dict(task(repo), version=1)
    original_mutate = controller._attention_store.mutate
    path = os.environ["HERDR_ATTENTION_FILE"]
    def concurrent_mutate(key, updater):
        if updater.__name__ == "expire_owned_wait":
            return original_mutate(key, updater)
        code = "from herdr.liveness import EpisodeStore; import sys; from pathlib import Path; EpisodeStore(Path(sys.argv[1])).upsert('t-lock:finalize', {'reason':'new_run_error','attempts':3,'detail':{'run_id':'new-run'}})"
        subprocess.run([sys.executable, "-c", code, path], cwd=ROOT, check=True, capture_output=True)
        return original_mutate(key, updater)
    outcome = {"retryable": True, "kind": "wait", "reason": "git_index_lock", "budgeted": False}
    with patch.object(controller, "get_task", return_value=t), patch.object(controller, "workflow_closed", return_value=False), patch.object(controller, "finalize_completed_task", return_value=outcome), patch.object(controller._attention_store, "mutate", side_effect=concurrent_mutate):
        controller._check_finalize_retry(t, "completed", 1000)
    episode = controller.attention_get("t-lock:finalize")
    assert episode["reason"] == "new_run_error"
    assert episode["attempts"] == 3
    assert episode["detail"]["run_id"] == "new-run"


@pytest.mark.parametrize("change", ["run", "transition"])
def test_new_owner_expires_only_typed_old_wait(controller, locked_repo, change):
    repo, _ = locked_repo
    old = dict(task(repo), version=1, status_history=[{"from": "agent_done", "to": "completed", "at": 1}])
    fresh = dict(old, version=2)
    if change == "run":
        fresh["run_id"] = "new-run"
    else:
        fresh["status_history"] = old["status_history"] + [{"from": "completed", "to": "rework", "at": 2}, {"from": "agent_done", "to": "completed", "at": 3}]
    controller.attention_note("t-lock:finalize", old, "finalize", reason="git_index_lock", attempts=4, next_retry_at=2000, detail={"run_id": old["run_id"], "owner_episode": controller.blocker_queue_episode(old)})
    with patch.object(controller, "get_task", return_value=fresh), patch.object(controller, "workflow_closed", return_value=False), patch.object(controller, "finalize_completed_task", return_value={"retryable": True, "kind": "error"}) as finalize:
        controller._check_finalize_retry(fresh, "completed", 1000)
        finalize.assert_called_once()
    assert controller.attention_get("t-lock:finalize")["attempts"] == 1


def test_metadata_save_does_not_reset_typed_wait_budget(controller, locked_repo):
    repo, _ = locked_repo
    old = dict(task(repo), version=1, status_history=[{"from": "agent_done", "to": "completed", "at": 1}])
    fresh = dict(old, version=2)
    controller.attention_note("t-lock:finalize", old, "finalize", reason="git_index_lock", attempts=4, next_retry_at=2000, detail={"owner_episode": controller.blocker_queue_episode(old)})
    with patch.object(controller, "workflow_closed", return_value=False), patch.object(controller, "finalize_completed_task") as finalize:
        assert not controller._check_finalize_retry(fresh, "completed", 1000)
        finalize.assert_not_called()
    assert controller.attention_get("t-lock:finalize")["attempts"] == 4
