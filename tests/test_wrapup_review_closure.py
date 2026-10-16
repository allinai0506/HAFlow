"""Wrapup completion requires an acceptable, head-bound, forge-verified receipt.

Codex review P1s (wf-haflow-1015-01), pinned behaviourally:

1. the invariant is enforced in the **shared transition gateway**
   (``kernel.transition_task``) and in the **bypass-gate direct write** — the
   controller's CAS auto-accept path and ``bypass-gate`` are covered too;
2. receipt content is structured and machine-checked: non-deferred needs
   PR URL + ``ci_status=pass`` + unresolved P1 = 0 + P2 count + disposition
   **plus forge verification evidence** (the CLI only writes it after
   querying ``gh``: PR head matches, checks all green, no unresolved Codex
   P1/P0 threads); deferred needs a reason — always plus the 40-hex head SHA;
3. the receipt is **bound to the reviewed head**: a later commit invalidates
   it, and an unverifiable head fails closed; the audited
   ``--skip-review-closure --note`` bypass is likewise bound to the head and
   expires on rework;
4. the contract is **opt-in** via the pinned workflow configuration
   (``review_closure_required: true`` on the wrapup node; the authoritative
   snapshot is SQLite ``config_json``, with the pinned file as fallback):
   legacy workflows and custom templates keep their previous behaviour,
   while an existing-but-unreadable configuration fails closed.

These tests run a fake ``gh`` (written into ``<tmp>/fakebin``) so forge
verification is hermetic; the CLI prepends that directory to ``PATH``.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest
from herdr.state_store import SQLiteStateStore

ROOT = Path(__file__).resolve().parents[1]
PR_URL = "https://github.com/owner/repo/pull/1"

FAKE_GH_SCRIPT = '''#!/usr/bin/env python3
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
args = sys.argv[1:]


def _read(name, default=""):
    path = HERE / name
    return path.read_text().strip() if path.exists() else default


if len(args) >= 2 and args[0] == "api" and args[1] == "graphql":
    threads = HERE / "threads.json"
    payload = {"data": {"repository": {"pullRequest": {"reviewThreads": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": json.loads(threads.read_text()) if threads.exists() else [],
    }}}}}
    print(json.dumps(payload))
    sys.exit(0)

if (len(args) >= 2 and args[0] == "api" and args[1].startswith("repos/")
        and "/pulls/" in args[1]):
    print(json.dumps({"head": _read("head.txt"), "state": "open"}))
    sys.exit(0)

if args[:2] == ["pr", "checks"]:
    sys.exit(int(_read("checks_rc", "0") or 0))

print("fake gh: unsupported args: " + " ".join(args), file=sys.stderr)
sys.exit(1)
'''

OPTED_NODES = [
    {"id": "implementation"},
    {"id": "wrapup", "review_closure_required": True},
]
PLAIN_NODES = [
    {"id": "implementation"},
    {"id": "wrapup"},
]


def run_cli(store, *argv):
    env = os.environ.copy()
    env["HERDR_STATE_DB"] = str(store.db_path)
    env.pop("TASKS_FILE", None)
    env.pop("WORKFLOWS_FILE", None)
    fakebin = Path(store.db_path).parent / "fakebin"
    if fakebin.is_dir():
        env["PATH"] = f"{fakebin}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        [sys.executable, str(ROOT / "bin/herdr-task"), *argv],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
    )


def run_kernel_transition(store, task_id, to_status="completed"):
    env = os.environ.copy()
    env["HERDR_STATE_DB"] = str(store.db_path)
    code = (
        "from herdr import kernel; "
        f"kernel.transition_task({task_id!r}, {to_status!r}, 'shared-path-test', 'test')"
    )
    return subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=15,
    )


def make_git_clone(root: Path) -> Path:
    clone = root / "clone"
    clone.mkdir(parents=True)

    def git(*args):
        subprocess.run(["git", "-C", str(clone), *args], check=True,
                       capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@example.test")
    git("config", "user.name", "t")
    (clone / "f.txt").write_text("one\n")
    git("add", "f.txt")
    git("commit", "-qm", "one")
    return clone


def clone_head(clone: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def commit_more(clone: Path, content: str) -> None:
    (clone / "f.txt").write_text(content)
    subprocess.run(["git", "-C", str(clone), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-qm", "more"],
                   check=True, capture_output=True, text=True)


def make_scene(root: Path) -> SimpleNamespace:
    clone = make_git_clone(root)
    fakebin = root / "fakebin"
    fakebin.mkdir()
    gh = fakebin / "gh"
    gh.write_text(FAKE_GH_SCRIPT)
    gh.chmod(0o755)
    head = clone_head(clone)
    (fakebin / "head.txt").write_text(head)

    store = SQLiteStateStore(root / "state.db")
    store.save_workflow({
        "workflow_id": "wf-closure", "status": "running",
        "config": {"nodes": OPTED_NODES},
    })
    store.save_workflow({"workflow_id": "wf-legacy", "status": "running"})
    store.save_workflow({
        "workflow_id": "wf-custom", "status": "running",
        "config": {"nodes": PLAIN_NODES},
    })
    store.save_task({
        "task_id": "wrapup-task", "workflow_id": "wf-closure",
        "status": "agent_done", "stage": "wrapup", "node": "wrapup",
        "agent": "qodercli", "clone_path": str(clone),
    })
    store.save_task({
        "task_id": "legacy-wrapup", "workflow_id": "wf-legacy",
        "status": "agent_done", "stage": "wrapup", "node": "wrapup",
        "agent": "qodercli",
    })
    store.save_task({
        "task_id": "custom-wrapup", "workflow_id": "wf-custom",
        "status": "agent_done", "stage": "wrapup", "node": "wrapup",
        "agent": "qodercli",
    })
    store.save_task({
        "task_id": "impl-task", "workflow_id": "wf-closure",
        "status": "agent_done", "stage": "implementation",
        "node": "implementation", "agent": "opencode",
    })
    return SimpleNamespace(store=store, clone=clone, head=head, fakebin=fakebin)


@pytest.fixture
def scene(tmp_path):
    return make_scene(tmp_path)


def set_fake_head(scene, sha: str) -> None:
    (scene.fakebin / "head.txt").write_text(sha)


def record_valid(store, head, task_id="wrapup-task", **overrides):
    values = {
        "pr_url": PR_URL,
        "ci_status": "pass",
        "unresolved_p1": "0",
        "unresolved_p2": "1",
        "findings": "P2 disposed via follow-up issue #9",
        "head_sha": head,
    }
    values.update({key: str(value) for key, value in overrides.items()})
    argv = ["record-review-closure", task_id]
    for key in ("pr_url", "ci_status", "unresolved_p1", "unresolved_p2",
                "findings", "head_sha"):
        argv += [f"--{key.replace('_', '-')}", values[key]]
    return run_cli(store, *argv)


def test_wrapup_completion_requires_receipt(scene):
    result = run_cli(scene.store, "set", "wrapup-task", "completed")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "REVIEW CLOSURE REQUIRED" in (result.stdout + result.stderr)
    assert scene.store.get_task("wrapup-task")["status"] == "agent_done"


def test_shared_transition_gateway_enforces_invariant(scene):
    """The controller's CAS auto-accept path must be gated too (kernel funnel)."""
    refused = run_kernel_transition(scene.store, "wrapup-task")
    assert refused.returncode != 0
    assert "REVIEW CLOSURE REQUIRED" in refused.stderr
    assert scene.store.get_task("wrapup-task")["status"] == "agent_done"

    assert record_valid(scene.store, scene.head).returncode == 0
    allowed = run_kernel_transition(scene.store, "wrapup-task")
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert scene.store.get_task("wrapup-task")["status"] == "completed"


def test_bypass_gate_completion_requires_receipt(scene):
    """bypass-gate persists `completed` directly; it must enforce the receipt."""
    task = scene.store.get_task("wrapup-task")
    task["status"] = "blocked"
    scene.store.save_task(task)

    refused = run_cli(
        scene.store, "bypass-gate", "wrapup-task", "--reason", "operator review",
    )
    assert refused.returncode != 0
    assert "REVIEW CLOSURE REQUIRED" in (refused.stdout + refused.stderr)
    assert scene.store.get_task("wrapup-task")["status"] == "blocked"

    assert record_valid(scene.store, scene.head).returncode == 0
    allowed = run_cli(
        scene.store, "bypass-gate", "wrapup-task", "--reason", "operator review",
    )
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert scene.store.get_task("wrapup-task")["status"] == "completed"


@pytest.mark.parametrize("ci_args", [
    ["--ci-status", "fail"],
    ["--ci-status", "pending"],
    [],
])
def test_receipt_requires_passing_ci(scene, ci_args):
    argv = ["record-review-closure", "wrapup-task",
            "--pr-url", PR_URL,
            "--unresolved-p1", "0", "--unresolved-p2", "0",
            "--findings", "clean", "--head-sha", scene.head, *ci_args]
    result = run_cli(scene.store, *argv)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "REVIEW CLOSURE REFUSED" in (result.stdout + result.stderr)


def test_receipt_rejects_unresolved_p1_and_requires_structured_counts(scene):
    unresolved = record_valid(scene.store, scene.head, unresolved_p1=3)
    assert unresolved.returncode == 2
    assert "unresolved P1" in (unresolved.stdout + unresolved.stderr)

    missing_p1 = run_cli(
        scene.store, "record-review-closure", "wrapup-task",
        "--pr-url", PR_URL, "--ci-status", "pass",
        "--unresolved-p2", "0", "--findings", "x", "--head-sha", scene.head,
    )
    assert missing_p1.returncode == 2
    assert "unresolved-p1" in (missing_p1.stdout + missing_p1.stderr)

    missing_findings = run_cli(
        scene.store, "record-review-closure", "wrapup-task",
        "--pr-url", PR_URL, "--ci-status", "pass",
        "--unresolved-p1", "0", "--unresolved-p2", "0",
        "--head-sha", scene.head,
    )
    assert missing_findings.returncode == 2

    missing_head = run_cli(
        scene.store, "record-review-closure", "wrapup-task",
        "--pr-url", PR_URL, "--ci-status", "pass",
        "--unresolved-p1", "0", "--unresolved-p2", "0", "--findings", "x",
    )
    assert missing_head.returncode == 2
    assert "head-sha" in (missing_head.stdout + missing_head.stderr)


def test_record_rejects_head_that_does_not_match_clone(scene):
    result = record_valid(scene.store, "b" * 40)
    assert result.returncode == 2
    assert "clone HEAD" in (result.stdout + result.stderr)


def test_record_requires_forge_verification(scene):
    bad_url = record_valid(scene.store, scene.head, pr_url="not-a-pr")
    assert bad_url.returncode == 2
    assert "not a recognizable" in (bad_url.stdout + bad_url.stderr)

    set_fake_head(scene, "c" * 40)  # forge reports a different PR head
    mismatched = record_valid(scene.store, scene.head)
    assert mismatched.returncode == 2
    assert "does not match" in (mismatched.stdout + mismatched.stderr)
    set_fake_head(scene, scene.head)

    (scene.fakebin / "checks_rc").write_text("8")  # checks pending
    pending = record_valid(scene.store, scene.head)
    assert pending.returncode == 2
    assert "not all passing" in (pending.stdout + pending.stderr)
    (scene.fakebin / "checks_rc").write_text("0")

    (scene.fakebin / "threads.json").write_text(json.dumps([
        {"id": "t1", "isResolved": False, "isOutdated": False,
         "comments": {"nodes": [
             {"author": {"login": "chatgpt-codex-connector"},
              "body": "**P1 Badge** unresolved issue"},
         ]}},
    ]))
    blocked = record_valid(scene.store, scene.head)
    assert blocked.returncode == 2
    assert "unresolved Codex P1" in (blocked.stdout + blocked.stderr)
    (scene.fakebin / "threads.json").write_text("[]")

    good = record_valid(scene.store, scene.head)
    assert good.returncode == 0, good.stdout + good.stderr
    assert run_cli(scene.store, "set", "wrapup-task", "completed").returncode == 0


def test_stale_receipt_is_rejected_then_fresh_receipt_completes(scene):
    assert record_valid(scene.store, scene.head).returncode == 0
    commit_more(scene.clone, "two\n")  # a commit after the reviewed head

    stale = run_cli(scene.store, "set", "wrapup-task", "completed")
    assert stale.returncode == 2
    assert "stale" in (stale.stdout + stale.stderr)
    assert scene.store.get_task("wrapup-task")["status"] == "agent_done"

    fresh_head = clone_head(scene.clone)
    set_fake_head(scene, fresh_head)
    assert record_valid(scene.store, fresh_head).returncode == 0
    fresh = run_cli(scene.store, "set", "wrapup-task", "completed")
    assert fresh.returncode == 0, fresh.stdout + fresh.stderr
    assert scene.store.get_task("wrapup-task")["status"] == "completed"


def test_unverifiable_head_fails_closed(scene):
    task = scene.store.get_task("wrapup-task")
    task["clone_path"] = str(scene.clone.parent / "gone-clone")
    scene.store.save_task(task)

    assert record_valid(scene.store, scene.head).returncode == 0
    refused = run_cli(scene.store, "set", "wrapup-task", "completed")
    assert refused.returncode == 2
    assert "cannot verify" in (refused.stdout + refused.stderr)
    assert scene.store.get_task("wrapup-task")["status"] == "agent_done"


def test_bypass_is_bound_to_head_and_expires_on_rework(scene):
    refused = run_cli(
        scene.store, "set", "wrapup-task", "completed", "--skip-review-closure",
    )
    assert refused.returncode == 2
    assert "SKIP REFUSED" in (refused.stdout + refused.stderr)

    # A bypass recorded for an older head must not survive rework.
    scene.store.record_event(
        "review_closure_skipped",
        {"task_id": "wrapup-task", "reason": "old waiver", "head_sha": scene.head},
        task_id="wrapup-task", workflow_id="wf-closure",
        node_id="wrapup", source="herdr-task",
    )
    commit_more(scene.clone, "rework\n")
    expired = run_cli(scene.store, "set", "wrapup-task", "completed")
    assert expired.returncode == 2
    assert "expired" in (expired.stdout + expired.stderr)
    assert scene.store.get_task("wrapup-task")["status"] == "agent_done"

    # A fresh audited bypass binds the current head and completes.
    fresh = run_cli(
        scene.store, "set", "wrapup-task", "completed", "--skip-review-closure",
        "--note", "audited emergency after rework",
    )
    assert fresh.returncode == 0, fresh.stdout + fresh.stderr
    assert scene.store.get_task("wrapup-task")["status"] == "completed"


def test_deferred_receipt_requires_reason_then_allows_completion(scene):
    refused = run_cli(
        scene.store, "record-review-closure", "wrapup-task",
        "--deferred", "--head-sha", scene.head,
    )
    assert refused.returncode == 2
    assert "REVIEW CLOSURE REFUSED" in (refused.stdout + refused.stderr)

    deferred = run_cli(
        scene.store, "record-review-closure", "wrapup-task", "--deferred",
        "--reason", "no remote configured: git remote -v empty",
        "--head-sha", scene.head,
    )
    assert deferred.returncode == 0, deferred.stdout + deferred.stderr
    assert run_cli(scene.store, "set", "wrapup-task", "completed").returncode == 0


def test_non_wrapup_completion_unaffected(scene):
    result = run_cli(scene.store, "set", "impl-task", "completed")
    assert result.returncode == 0, result.stdout + result.stderr
    assert scene.store.get_task("impl-task")["status"] == "completed"


def test_legacy_and_custom_wrapup_without_capability_are_not_gated(scene):
    """Upgrades must not brick pinned configurations that never opted in."""
    legacy = run_cli(scene.store, "set", "legacy-wrapup", "completed")
    assert legacy.returncode == 0, legacy.stdout + legacy.stderr
    custom = run_cli(scene.store, "set", "custom-wrapup", "completed")
    assert custom.returncode == 0, custom.stdout + custom.stderr
    assert scene.store.get_task("legacy-wrapup")["status"] == "completed"
    assert scene.store.get_task("custom-wrapup")["status"] == "completed"


def test_file_fallback_gates_and_unreadable_config_fails_closed(scene, tmp_path):
    # No config snapshot on the workflow row: the task's pinned file is the
    # fallback and still opts in.
    config = tmp_path / "run" / "workflow.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"nodes": OPTED_NODES}))
    scene.store.save_workflow({"workflow_id": "wf-file", "status": "running"})
    scene.store.save_task({
        "task_id": "file-wrapup", "workflow_id": "wf-file",
        "status": "agent_done", "stage": "wrapup", "node": "wrapup",
        "agent": "qodercli", "clone_path": str(scene.clone),
        "workflow_config": str(config),
    })
    refused = run_cli(scene.store, "set", "file-wrapup", "completed")
    assert refused.returncode == 2
    assert "REVIEW CLOSURE REQUIRED" in (refused.stdout + refused.stderr)

    # An existing-but-unreadable pinned file fails closed, not silently off.
    broken = tmp_path / "run" / "broken.json"
    broken.write_text("{not json")
    scene.store.save_workflow({"workflow_id": "wf-broken", "status": "running"})
    scene.store.save_task({
        "task_id": "broken-wrapup", "workflow_id": "wf-broken",
        "status": "agent_done", "stage": "wrapup", "node": "wrapup",
        "agent": "qodercli", "workflow_config": str(broken),
    })
    broken_run = run_cli(scene.store, "set", "broken-wrapup", "completed")
    assert broken_run.returncode == 2
    assert scene.store.get_task("broken-wrapup")["status"] == "agent_done"


def test_corrupt_authoritative_snapshot_fails_closed(scene):
    conn = sqlite3.connect(scene.store.db_path)
    conn.execute(
        "UPDATE workflows SET config_json=? WHERE workflow_id=?",
        ("{bad json", "wf-closure"),
    )
    conn.commit()
    conn.close()

    refused = run_cli(scene.store, "set", "wrapup-task", "completed")
    assert refused.returncode == 2
    assert scene.store.get_task("wrapup-task")["status"] == "agent_done"
