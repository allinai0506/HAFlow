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

import herdr.review_closure  # noqa: F401
from herdr.state_store import SQLiteStateStore

ROOT = Path(__file__).resolve().parents[1]
PR_URL = "https://github.com/owner/repo/pull/1"

FAKE_GH_SCRIPT = '''#!/usr/bin/env python3
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
args = sys.argv[1:]


def _read(name, default=""):
    path = HERE / name
    return path.read_text().strip() if path.exists() else default


def _api_host_pinned(tokens):
    return ("--hostname" in tokens
            and tokens[tokens.index("--hostname") + 1] == "github.com")


if args and args[0] == "api":
    if not _api_host_pinned(args):
        print("fake gh: api call missing --hostname github.com", file=sys.stderr)
        sys.exit(1)

if args and args[0] == "api" and "graphql" in args:
    if (HERE / "graphql_errors").exists():
        print(json.dumps({"errors": [{"message": "boom"}], "data": None}))
        sys.exit(0)
    typed_pr = False
    for i, token in enumerate(args[:-1]):
        if args[i + 1].startswith("pr="):
            typed_pr = token == "-F"
    if not typed_pr:
        # Real gh sends raw fields as strings; the query declares $pr: Int!,
        # so GitHub answers HTTP-200 with an errors payload.
        print(json.dumps({
            "errors": [{"message": "Variable $pr of type Int! was provided invalid value"}],
            "data": None,
        }))
        sys.exit(0)
    threads = HERE / "threads.json"
    payload = {"data": {"repository": {"pullRequest": {"reviewThreads": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": json.loads(threads.read_text()) if threads.exists() else [],
    }}}}}
    print(json.dumps(payload))
    sys.exit(0)

if (args and args[0] == "api"
        and any(t.startswith("repos/") and "/issues/" in t and t.endswith("/comments")
                for t in args)):
    mode = _read("review_state", "completed")
    if mode == "missing":
        print("[]")
        sys.exit(0)
    sha = "e" * 40 if mode in ("wrong_head", "multi_round_earlier_completed", "running_then_completed_different_head") else _read("head.txt")
    if mode == "forged_author":
        author = "evil-user"
    elif mode == "evil_suffix_author":
        author = "chatgpt-codex-connector-evil"
    elif mode == "exact_login_nobot":
        author = "chatgpt-codex-connector"
    else:
        author = "chatgpt-codex-connector[bot]"


    def marker(status, created):
        body = ('<!-- codex-review-state:v1 '
                '{"code_review":{"commit_sha":"%s","status":"%s"}} -->'
                % (sha, status))
        return {"login": author, "body": body, "created_at": created}

    if mode == "completed_then_running":
        items = [marker("completed", "2026-10-16T05:00:00Z"),
                 marker("running", "2026-10-16T05:10:00Z")]
    elif mode in ("running_then_completed", "running_then_completed_different_head"):
        items = [marker("running", "2026-10-16T05:00:00Z"),
                 marker("completed", "2026-10-16T05:10:00Z")]
    elif mode == "multi_round_earlier_completed":
        items = [marker("completed", "2026-10-16T04:00:00Z"),
                 marker("completed", "2026-10-16T05:00:00Z")]
    elif mode == "running":
        items = [marker("running", "2026-10-16T05:00:00Z")]
    else:
        items = [marker("completed", "2026-10-16T05:00:00Z")]
    print(json.dumps(items))
    sys.exit(0)

if (args and args[0] == "api"
        and any(t.startswith("repos/") and "/pulls/" in t for t in args)):
    advance = _read("advance_clone")
    if advance:
        subprocess.run(["git", "-C", advance, "commit", "--allow-empty",
                        "-m", "race"], check=True, capture_output=True)
    print(json.dumps({
        "head": _read("head.txt"),
        "state": _read("pr_state", "open") or "open",
        "merged_at": _read("pr_merged_at") or None,
    }))
    sys.exit(0)

if args[:2] == ["pr", "checks"]:
    repo_idx = args.index("--repo") if "--repo" in args else -1
    if repo_idx < 0 or not args[repo_idx + 1].startswith("github.com/"):
        print("fake gh: pr checks missing --repo github.com/<owner>/<repo>",
              file=sys.stderr)
        sys.exit(1)
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


def run_cli(store, *argv, env_extra=None):
    env = os.environ.copy()
    env["HERDR_STATE_DB"] = str(store.db_path)
    env.pop("TASKS_FILE", None)
    env.pop("WORKFLOWS_FILE", None)
    if env_extra:
        env.update(env_extra)
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
    fakebin = Path(store.db_path).parent / "fakebin"
    if fakebin.is_dir():
        env["PATH"] = f"{fakebin}{os.pathsep}{env.get('PATH', '')}"
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
    subprocess.run(
        ["git", "-C", str(clone), "remote", "add", "origin",
         "https://github.com/owner/repo.git"],
        check=True, capture_output=True, text=True,
    )
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
        "source_repo": str(clone),
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
    assert "unrecognized PR URL" in (bad_url.stdout + bad_url.stderr)

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


def test_unresolved_p2_threads_on_forge_rejected_when_declared_zero(scene):
    """PR with unresolved P2 thread on forge cannot declare --unresolved-p2 0 (Issue #228 Item 3)."""
    (scene.fakebin / "threads.json").write_text(json.dumps([
        {"id": "t_p2", "isResolved": False, "isOutdated": False,
         "comments": {"nodes": [
             {"author": {"login": "chatgpt-codex-connector"},
              "body": "**P2 Badge** unresolved minor issue"},
         ]}},
    ]))
    refused = record_valid(scene.store, scene.head, unresolved_p2=0, findings="clean")
    assert refused.returncode == 2
    assert "unresolved Codex P2" in (refused.stdout + refused.stderr)


def test_unresolved_p2_threads_on_forge_accepted_with_declared_count_and_disposition(scene):
    """PR with unresolved P2 thread on forge passes when count and disposition are provided (Issue #228 Item 3)."""
    (scene.fakebin / "threads.json").write_text(json.dumps([
        {"id": "t_p2", "isResolved": False, "isOutdated": False,
         "comments": {"nodes": [
             {"author": {"login": "chatgpt-codex-connector"},
              "body": "**P2 Badge** unresolved minor issue"},
         ]}},
    ]))
    ok = record_valid(scene.store, scene.head, unresolved_p2=1, findings="P2 disposed via follow-up issue #9")
    assert ok.returncode == 0, ok.stdout + ok.stderr


def test_backlogged_p2_thread_on_forge_allows_zero_unresolved(scene):
    """P2 thread backlogged in PR comment is considered disposed on forge, allowing --unresolved-p2 0."""
    (scene.fakebin / "threads.json").write_text(json.dumps([
        {"id": "t_p2", "isResolved": False, "isOutdated": False,
         "comments": {"nodes": [
             {"author": {"login": "chatgpt-codex-connector"},
              "body": "**P2 Badge** minor issue"},
             {"author": {"login": "developer"},
              "body": "deferred to backlog in issue #123"},
         ]}},
    ]))
    ok = record_valid(scene.store, scene.head, unresolved_p2=0, findings="clean")
    assert ok.returncode == 0, ok.stdout + ok.stderr



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
    assert record_valid(scene.store, scene.head).returncode == 0

    task = scene.store.get_task("wrapup-task")
    task["clone_path"] = str(scene.clone.parent / "gone-clone")
    scene.store.save_task(task)

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
    from herdr import state_db
    state_db.save_user_token("ops-lead", "operator", "rework_token_777", db_path=scene.store.db_path)
    fresh = run_cli(
        scene.store, "set", "wrapup-task", "completed", "--skip-review-closure",
        "--operator", "ops-lead",
        "--operator-token", "rework_token_777",
        "--note", "audited emergency after rework",
        env_extra={"HERDR_OPERATOR": "1"},
    )
    assert fresh.returncode == 0, fresh.stdout + fresh.stderr
    assert scene.store.get_task("wrapup-task")["status"] == "completed"



def test_deferred_receipt_requires_reason_then_allows_completion(scene):
    subprocess.run(
        ["git", "-C", str(scene.clone), "remote", "remove", "origin"],
        check=True, capture_output=True, text=True,
    )
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


def test_non_github_pr_host_is_not_machine_verifiable(scene):
    refused = record_valid(
        scene.store, scene.head,
        pr_url="https://github.example.com/owner/repo/pull/7",
    )
    assert refused.returncode == 2
    assert "not machine-verifiable" in (refused.stdout + refused.stderr)


def test_deferred_requires_machine_evidence(scene):
    # The scene's GitHub origin means the PR path is verifiable: refused.
    refused = run_cli(
        scene.store, "record-review-closure", "wrapup-task", "--deferred",
        "--reason", "trust me", "--head-sha", scene.head,
    )
    assert refused.returncode == 2
    assert "not justified" in (refused.stdout + refused.stderr)


def test_deferred_records_unsupported_forge_evidence(tmp_path):
    scene = make_scene(tmp_path)
    subprocess.run(
        ["git", "-C", str(scene.clone), "remote", "remove", "origin"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(scene.clone), "remote", "add", "origin",
         "file:///nonexistent/repo.git"],
        check=True, capture_output=True, text=True,
    )
    recorded = run_cli(
        scene.store, "record-review-closure", "wrapup-task", "--deferred",
        "--reason", "gitee mirror only; unsupported forge",
        "--head-sha", scene.head,
    )
    assert recorded.returncode == 0, recorded.stdout + recorded.stderr
    assert run_cli(scene.store, "set", "wrapup-task", "completed").returncode == 0


def test_graphql_errors_fail_closed(scene):
    """HTTP-200 GraphQL errors must never read as 'no unresolved threads'."""
    (scene.fakebin / "graphql_errors").write_text("1")
    refused = record_valid(scene.store, scene.head)
    assert refused.returncode == 2
    assert "could not read review threads" in (refused.stdout + refused.stderr)


def test_receipt_must_match_delivery_identity(scene):
    refused = record_valid(
        scene.store, scene.head,
        pr_url="https://github.com/attacker/fork/pull/1",
    )
    assert refused.returncode == 2
    assert "delivery identity" in (refused.stdout + refused.stderr)


def test_completion_revalidates_forge_state(scene):
    assert record_valid(scene.store, scene.head).returncode == 0

    (scene.fakebin / "checks_rc").write_text("8")  # CI went red after recording
    refused = run_cli(scene.store, "set", "wrapup-task", "completed")
    assert refused.returncode == 2
    assert "no longer holds" in (refused.stdout + refused.stderr)
    assert scene.store.get_task("wrapup-task")["status"] == "agent_done"

    (scene.fakebin / "checks_rc").write_text("0")
    allowed = run_cli(scene.store, "set", "wrapup-task", "completed")
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert scene.store.get_task("wrapup-task")["status"] == "completed"


def test_closed_unmerged_pr_is_rejected(scene):
    (scene.fakebin / "pr_state").write_text("closed")
    refused = record_valid(scene.store, scene.head)
    assert refused.returncode == 2
    assert "not merged" in (refused.stdout + refused.stderr)

    # A merged PR (closed with merged_at set) is acceptable.
    (scene.fakebin / "pr_merged_at").write_text("2026-10-16T00:00:00Z")
    merged = record_valid(scene.store, scene.head)
    assert merged.returncode == 0, merged.stdout + merged.stderr


def test_bypass_requires_operator_identity_and_capability(scene):
    no_operator = run_cli(
        scene.store, "set", "wrapup-task", "completed", "--skip-review-closure",
        "--note", "emergency",
    )
    assert no_operator.returncode == 2
    assert "--operator" in (no_operator.stdout + no_operator.stderr)

    no_capability = run_cli(
        scene.store, "set", "wrapup-task", "completed", "--skip-review-closure",
        "--operator", "ops-lead", "--note", "emergency",
    )
    assert no_capability.returncode == 2
    assert "HERDR_OPERATOR" in (no_capability.stdout + no_capability.stderr)

    worker_operator = run_cli(
        scene.store, "set", "wrapup-task", "completed", "--skip-review-closure",
        "--operator", "qodercli", "--note", "emergency",
        env_extra={"HERDR_OPERATOR": "1"},
    )
    assert worker_operator.returncode == 2
    assert "independent" in (worker_operator.stdout + worker_operator.stderr)

    from herdr import state_db
    state_db.save_user_token("ops-lead", "operator", "valid_token_lead", db_path=scene.store.db_path)
    allowed = run_cli(
        scene.store, "set", "wrapup-task", "completed", "--skip-review-closure",
        "--operator", "ops-lead", "--note", "emergency",
        "--operator-token", "valid_token_lead",
        env_extra={"HERDR_OPERATOR": "1"},
    )
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert scene.store.get_task("wrapup-task")["status"] == "completed"


def test_bypass_rejects_worker_self_set_env_without_credentials(scene):
    """Worker self-setting HERDR_OPERATOR=1 with fake operator without credentials must be rejected (Issue #228 Item 1)."""
    refused = run_cli(
        scene.store, "set", "wrapup-task", "completed", "--skip-review-closure",
        "--operator", "ops-lead", "--note", "fake worker bypass",
        env_extra={"HERDR_OPERATOR": "1"},
    )
    assert refused.returncode == 2
    assert "authentication" in (refused.stdout + refused.stderr).lower()


def test_bypass_rejects_credentials_file_inside_worker_clone(scene):
    """Operator credentials file cannot reside in worker clone (Issue #228 Item 1)."""
    inside_cred = scene.clone / "fake_operator.token"
    inside_cred.write_text("secret123")
    inside_cred.chmod(0o600)

    # Register token on server
    from herdr import state_db
    state_db.save_user_token("ops-lead", "operator", "secret123", db_path=scene.store.db_path)

    refused = run_cli(
        scene.store, "set", "wrapup-task", "completed", "--skip-review-closure",
        "--operator", "ops-lead", "--note", "inside clone cred bypass",
        "--operator-credentials", str(inside_cred),
        env_extra={"HERDR_OPERATOR": "1"},
    )
    assert refused.returncode == 2
    assert "cannot reside in worker clone" in (refused.stdout + refused.stderr)


def test_bypass_rejects_invalid_token(scene):
    """Supplied operator token must match registered token (Issue #228 Item 1)."""
    from herdr import state_db
    state_db.save_user_token("ops-lead", "operator", "valid_secret_123", db_path=scene.store.db_path)

    refused = run_cli(
        scene.store, "set", "wrapup-task", "completed", "--skip-review-closure",
        "--operator", "ops-lead", "--note", "wrong token bypass",
        "--operator-token", "wrong_token",
        env_extra={"HERDR_OPERATOR": "1"},
    )
    assert refused.returncode == 2
    assert "invalid operator token" in (refused.stdout + refused.stderr)


def test_bypass_accepts_authenticated_token(scene, tmp_path):
    """Bypass succeeds when authenticated operator token or credentials file outside clone is provided."""
    from herdr import state_db
    token = "valid_secret_123"
    state_db.save_user_token("ops-lead", "operator", token, db_path=scene.store.db_path)

    # Valid token via --operator-token
    allowed = run_cli(
        scene.store, "set", "wrapup-task", "completed", "--skip-review-closure",
        "--operator", "ops-lead", "--note", "authenticated token emergency",
        "--operator-token", token,
        env_extra={"HERDR_OPERATOR": "1"},
    )
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert scene.store.get_task("wrapup-task")["status"] == "completed"

    # Reset task status to agent_done for credentials file test
    task = scene.store.get_task("wrapup-task")
    task["status"] = "agent_done"
    scene.store.save_task(task)

    # Valid token via credentials file outside clone
    cred_file = tmp_path / "server_credentials" / "ops-lead.token"
    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_text(token)
    cred_file.chmod(0o600)

    allowed_file = run_cli(
        scene.store, "set", "wrapup-task", "completed", "--skip-review-closure",
        "--operator", "ops-lead", "--note", "authenticated file emergency",
        "--operator-credentials", str(cred_file),
        env_extra={"HERDR_OPERATOR": "1"},
    )
    assert allowed_file.returncode == 0, allowed_file.stdout + allowed_file.stderr
    assert scene.store.get_task("wrapup-task")["status"] == "completed"



def test_race_commit_between_second_head_read_and_cas_rejected(scene, monkeypatch):
    """A commit inserted between second HEAD read and CAS transition must be rejected (Issue #228 Item 4)."""
    monkeypatch.setenv("PATH", f"{scene.fakebin}{os.pathsep}{os.environ.get('PATH', '')}")
    assert record_valid(scene.store, scene.head).returncode == 0

    orig_cas = scene.store.transition_task

    def cas_with_interleaved_commit(*args, **kwargs):
        # Insert commit right before the CAS DB update
        commit_more(scene.clone, "race commit between check and cas\n")
        return orig_cas(*args, **kwargs)

    scene.store.transition_task = cas_with_interleaved_commit

    from herdr import kernel
    with pytest.raises((ValueError, RuntimeError)) as excinfo:
        kernel.transition_task("wrapup-task", "completed", "race-test", "test", store=scene.store)

    assert "HEAD" in str(excinfo.value) or "advanced" in str(excinfo.value)
    assert scene.store.get_task("wrapup-task")["status"] == "agent_done"



def test_receipt_requires_completed_review_on_head(scene):

    (scene.fakebin / "review_state").write_text("missing")
    missing = record_valid(scene.store, scene.head)
    assert missing.returncode == 2
    assert "completed Codex review" in (missing.stdout + missing.stderr)

    (scene.fakebin / "review_state").write_text("running")
    assert record_valid(scene.store, scene.head).returncode == 2

    (scene.fakebin / "review_state").write_text("wrong_head")
    assert record_valid(scene.store, scene.head).returncode == 2

    (scene.fakebin / "review_state").write_text("completed")
    ok = record_valid(scene.store, scene.head)
    assert ok.returncode == 0, ok.stdout + ok.stderr


def test_deferred_is_revalidated_at_completion(scene):
    subprocess.run(
        ["git", "-C", str(scene.clone), "remote", "remove", "origin"],
        check=True, capture_output=True, text=True,
    )
    recorded = run_cli(
        scene.store, "record-review-closure", "wrapup-task", "--deferred",
        "--reason", "no remote configured", "--head-sha", scene.head,
    )
    assert recorded.returncode == 0, recorded.stdout + recorded.stderr

    subprocess.run(
        ["git", "-C", str(scene.clone), "remote", "add", "origin",
         "https://github.com/owner/repo.git"],
        check=True, capture_output=True, text=True,
    )
    refused = run_cli(scene.store, "set", "wrapup-task", "completed")
    assert refused.returncode == 2
    assert "no longer justified" in (refused.stdout + refused.stderr)

    subprocess.run(
        ["git", "-C", str(scene.clone), "remote", "remove", "origin"],
        check=True, capture_output=True, text=True,
    )
    allowed = run_cli(scene.store, "set", "wrapup-task", "completed")
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert scene.store.get_task("wrapup-task")["status"] == "completed"


def test_deferred_refused_when_delivery_record_exists(scene):
    """A recorded delivery PR beats an empty remote list (Codex review P1)."""
    subprocess.run(
        ["git", "-C", str(scene.clone), "remote", "remove", "origin"],
        check=True, capture_output=True, text=True,
    )
    scene.store.record_event(
        "pull_request_created",
        {"number": 7, "url": "https://github.com/owner/repo/pull/7",
         "candidate_sha": scene.head, "branch": "b", "base": "main",
         "state": "open", "provider": "github"},
        task_id="wrapup-task", workflow_id="wf-closure", source="herdr-task",
    )
    refused = run_cli(
        scene.store, "record-review-closure", "wrapup-task", "--deferred",
        "--reason", "no remote", "--head-sha", scene.head,
    )
    assert refused.returncode == 2
    assert "not justified" in (refused.stdout + refused.stderr)


def test_review_state_marker_requires_trusted_author(scene):
    (scene.fakebin / "review_state").write_text("forged_author")
    refused = record_valid(scene.store, scene.head)
    assert refused.returncode == 2
    assert "completed Codex review" in (refused.stdout + refused.stderr)


def test_review_state_marker_rejects_evil_suffix_author(scene):
    """Substrings like chatgpt-codex-connector-evil must not forge review state (Issue #228 Item 2)."""
    (scene.fakebin / "review_state").write_text("evil_suffix_author")
    refused = record_valid(scene.store, scene.head)
    assert refused.returncode == 2
    assert "completed Codex review" in (refused.stdout + refused.stderr)


def test_review_state_marker_accepts_exact_bot_logins(scene):
    """Exact logins 'chatgpt-codex-connector' and 'chatgpt-codex-connector[bot]' are accepted."""
    (scene.fakebin / "review_state").write_text("exact_login_nobot")
    ok = record_valid(scene.store, scene.head)
    assert ok.returncode == 0, ok.stdout + ok.stderr

    (scene.fakebin / "review_state").write_text("completed")
    ok_bot = record_valid(scene.store, scene.head)
    assert ok_bot.returncode == 0, ok_bot.stdout + ok_bot.stderr



def test_completion_rechecks_head_after_forge_validation(scene):
    assert record_valid(scene.store, scene.head).returncode == 0
    (scene.fakebin / "advance_clone").write_text(str(scene.clone))
    refused = run_cli(scene.store, "set", "wrapup-task", "completed")
    assert refused.returncode == 2
    assert "advanced during validation" in (refused.stdout + refused.stderr)
    assert scene.store.get_task("wrapup-task")["status"] == "agent_done"


def test_latest_review_state_must_be_completed(scene):
    (scene.fakebin / "review_state").write_text("completed_then_running")
    refused = record_valid(scene.store, scene.head)
    assert refused.returncode == 2
    assert "latest Codex review" in (refused.stdout + refused.stderr)

    (scene.fakebin / "review_state").write_text("running_then_completed")
    ok = record_valid(scene.store, scene.head)
    assert ok.returncode == 0, ok.stdout + ok.stderr


def test_receipt_must_match_recorded_delivery_pr_number(scene):
    scene.store.record_event(
        "pull_request_created",
        {"number": 1, "url": "https://github.com/owner/repo/pull/1",
         "candidate_sha": scene.head, "branch": "b", "base": "main",
         "state": "open", "provider": "github"},
        task_id="wrapup-task", workflow_id="wf-closure", source="herdr-task",
    )
    other_pr = record_valid(
        scene.store, scene.head,
        pr_url="https://github.com/owner/repo/pull/999",
    )
    assert other_pr.returncode == 2
    assert "recorded delivery PR" in (other_pr.stdout + other_pr.stderr)

    matching = record_valid(
        scene.store, scene.head,
        pr_url="https://github.com/owner/repo/pull/1",
    )
    assert matching.returncode == 0, matching.stdout + matching.stderr


def test_fork_and_upstream_remotes_accept_target_pr(scene):
    """Fork fallback (origin=fork, upstream=target) must not block the PR."""
    subprocess.run(
        ["git", "-C", str(scene.clone), "remote", "set-url", "origin",
         "git@github.com:forkuser/repo.git"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(scene.clone), "remote", "add", "upstream",
         "https://github.com/owner/repo.git"],
        check=True, capture_output=True, text=True,
    )
    # PR_URL (owner/repo) is the upstream target; it must be accepted.
    ok = record_valid(scene.store, scene.head)
    assert ok.returncode == 0, ok.stdout + ok.stderr

    unknown = record_valid(
        scene.store, scene.head,
        pr_url="https://github.com/third/repo/pull/1",
    )
    assert unknown.returncode == 2
    assert "delivery identity" in (unknown.stdout + unknown.stderr)


def test_stop_line_passes_after_two_completed_rounds(scene):
    (scene.fakebin / "review_state").write_text("multi_round_earlier_completed")
    ok = record_valid(scene.store, scene.head)
    assert ok.returncode == 0, ok.stdout + ok.stderr


def test_stop_line_does_not_pass_with_only_one_completed_round(scene):
    (scene.fakebin / "review_state").write_text("running_then_completed_different_head")
    refused = record_valid(scene.store, scene.head)
    assert refused.returncode == 2
    assert "no completed Codex review found" in (refused.stdout + refused.stderr)


def test_rogue_remote_in_clone_is_rejected_as_delivery_identity(scene):
    """Rogue remote added by worker in clone must not become a delivery identity (issue #227)."""
    subprocess.run(
        ["git", "-C", str(scene.clone), "remote", "add", "evil",
         "https://github.com/attacker/repo.git"],
        check=True, capture_output=True, text=True,
    )
    refused = record_valid(
        scene.store, scene.head,
        pr_url="https://github.com/attacker/repo/pull/1",
    )
    assert refused.returncode == 2
    assert "delivery identity" in (refused.stdout + refused.stderr)


def test_source_repo_is_authoritative_over_clone_remotes(scene, tmp_path):
    """source_repo remotes are authoritative; clone remotes cannot expand them (issue #227)."""
    source = tmp_path / "source_repo"
    source.mkdir()
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "remote", "add", "origin",
         "https://github.com/owner/repo.git"],
        check=True,
    )

    task = scene.store.get_task("wrapup-task")
    task["source_repo"] = str(source)
    scene.store.save_task(task)

    # Worker injects rogue remotes into task clone
    subprocess.run(
        ["git", "-C", str(scene.clone), "remote", "add", "evil",
         "https://github.com/attacker/repo.git"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(scene.clone), "remote", "add", "upstream",
         "https://github.com/attacker/repo.git"],
        check=True,
    )

    refused = record_valid(
        scene.store, scene.head,
        pr_url="https://github.com/attacker/repo/pull/1",
    )
    assert refused.returncode == 2
    assert "delivery identity" in (refused.stdout + refused.stderr)

    # The legitimate owner/repo PR remains accepted
    ok = record_valid(
        scene.store, scene.head,
        pr_url="https://github.com/owner/repo/pull/1",
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr


def test_fork_source_repo_accepts_upstream_and_ignores_clone_evil(scene, tmp_path):
    """Fork in source_repo accepts upstream target PR and ignores clone evil (issue #227)."""
    source = tmp_path / "fork_source_repo"
    source.mkdir()
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "remote", "add", "origin",
         "git@github.com:forkuser/repo.git"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "remote", "add", "upstream",
         "https://github.com/owner/repo.git"],
        check=True,
    )

    task = scene.store.get_task("wrapup-task")
    task["source_repo"] = str(source)
    scene.store.save_task(task)

    subprocess.run(
        ["git", "-C", str(scene.clone), "remote", "add", "evil",
         "https://github.com/attacker/repo.git"],
        check=True,
    )

    ok = record_valid(
        scene.store, scene.head,
        pr_url="https://github.com/owner/repo/pull/1",
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr

    refused = record_valid(
        scene.store, scene.head,
        pr_url="https://github.com/attacker/repo/pull/1",
    )
    assert refused.returncode == 2
    assert "delivery identity" in (refused.stdout + refused.stderr)


def test_worker_clone_origin_cannot_forge_delivery_identity_when_source_repo_missing(scene):
    """When source_repo is absent, worker setting origin in clone must fail closed (Codex P1 on PR #265)."""
    task = scene.store.get_task("wrapup-task")
    task["source_repo"] = None
    scene.store.save_task(task)

    # Worker runs git remote set-url origin attacker in its clone
    subprocess.run(
        ["git", "-C", str(scene.clone), "remote", "set-url", "origin",
         "https://github.com/attacker/repo.git"],
        check=True,
    )

    refused = record_valid(
        scene.store, scene.head,
        pr_url="https://github.com/attacker/repo/pull/1",
    )
    assert refused.returncode == 2
    assert "delivery identity" in (refused.stdout + refused.stderr)


def test_worker_clone_origin_cannot_forge_delivery_identity_when_source_repo_non_github(scene, tmp_path):
    """When source_repo has no GitHub remote, clone remotes cannot forge identity (Codex P1 on PR #265)."""
    source = tmp_path / "nongithub_source"
    source.mkdir()
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "remote", "add", "origin",
         "https://gitlab.example.com/team/repo.git"],
        check=True,
    )

    task = scene.store.get_task("wrapup-task")
    task["source_repo"] = str(source)
    scene.store.save_task(task)

    # Worker in clone sets origin to attacker GitHub repo
    subprocess.run(
        ["git", "-C", str(scene.clone), "remote", "set-url", "origin",
         "https://github.com/attacker/repo.git"],
        check=True,
    )

    refused = record_valid(
        scene.store, scene.head,
        pr_url="https://github.com/attacker/repo/pull/1",
    )
    assert refused.returncode == 2
    assert "delivery identity" in (refused.stdout + refused.stderr)


def test_immutable_launch_delivery_identities_respected(scene):
    """Immutable delivery_identities captured at launch take precedence when source_repo is absent."""
    task = scene.store.get_task("wrapup-task")
    task["source_repo"] = None
    task["delivery_identities"] = ["launch-owner/repo"]
    scene.store.save_task(task)

    # Attacker PR is rejected
    refused = record_valid(
        scene.store, scene.head,
        pr_url="https://github.com/attacker/repo/pull/1",
    )
    assert refused.returncode == 2
    assert "delivery identity" in (refused.stdout + refused.stderr)

    # Launch delivery identity PR is accepted
    ok = record_valid(
        scene.store, scene.head,
        pr_url="https://github.com/launch-owner/repo/pull/1",
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr
