#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import shlex
import sys
import time
import tempfile
from pathlib import Path

HERDR_ROOT = Path(__file__).resolve().parent.parent
if str(HERDR_ROOT) not in sys.path:
    sys.path.insert(0, str(HERDR_ROOT))

try:
    from herdr.git_coordination import ensure_branch_available, pinned_local_onto_matches
except ImportError:
    from herdr_git_coordination import ensure_branch_available, pinned_local_onto_matches


CLONE_ROOT = Path(
    os.environ.get("HERDR_CLONES_DIR")
    or (Path.home() / ".herdr-controller" / "clones")
)


def run_json(cmd, timeout=125):
    result = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout,
    )

    if result.returncode != 0:
        from herdr.supervisor.state import redact_text
        raise RuntimeError(redact_text(result.stderr.strip() or result.stdout.strip()))

    if len(result.stdout) > 65536:
        raise RuntimeError("Native response exceeds worker budget")
    return json.loads(result.stdout)


def is_task_active_in_registry(task_id: str) -> bool:
    """Check if task_id has already been registered and is in active lifecycle."""
    try:
        from herdr.state_store import get_state_store
        t = get_state_store().get_task(task_id)
        if t and t.get("status") not in ("pending", "failed"):
            return True
    except Exception:
        pass
    tasks_file = Path.home() / ".herdr-controller" / "tasks.json"
    if tasks_file.exists():
        try:
            with open(tasks_file, "r", encoding="utf-8") as f:
                for t in json.load(f).get("tasks", []):
                    if t.get("task_id") == task_id and t.get("status") not in ("pending", "failed"):
                        return True
        except Exception:
            pass
    return False


def _registered_tasks():
    """Read task ownership without making branch creation depend on one store."""
    try:
        from herdr.state_store import get_state_store
        return get_state_store().list_tasks()
    except Exception:
        tasks_file = Path.home() / ".herdr-controller" / "tasks.json"
        try:
            return json.loads(tasks_file.read_text(encoding="utf-8")).get("tasks", [])
        except Exception:
            return []


def create_context_task_workspace(task_id):
    """execution.mode=context:任务独立可执行工作目录(纯目录,无 Git clone/branch)。

    与 CoW clone 同根同级(clones/<task_id>),使 cleanup/retention 生命周期复用。
    """
    workspace = CLONE_ROOT / task_id

    if workspace.exists():
        if not is_task_active_in_registry(task_id):
            print(
                f"[WORKSPACE HEAL] Removing stale unmanaged context workspace: {workspace}",
                file=sys.stderr
            )
            shutil.rmtree(workspace, ignore_errors=True)
        else:
            raise RuntimeError(
                f"Task workspace already exists: {workspace}"
            )

    CLONE_ROOT.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True)
    return workspace


def sanitize_clone_sandbox(clone):
    """Purge uncommitted working tree edits and untracked files copied into the clone sandbox.

    Since the clone sandbox is an isolated CoW copy, resetting it does not touch the source repo,
    ensuring that subsequent branch switches never collide with developer WIP in the source repo.
    """
    subprocess.run(["git", "-C", str(clone), "reset", "--hard", "HEAD"], capture_output=True)
    subprocess.run(
        ["git", "-C", str(clone), "clean", "-fd", "-e", ".herdr-launch-identity.json"],
        capture_output=True,
    )


def create_clone(source, task_id):
    source = Path(source).expanduser().resolve()
    clone = CLONE_ROOT / task_id

    if clone.exists():
        if not is_task_active_in_registry(task_id):
            print(
                f"[CLONE HEAL] Removing stale unmanaged clone: {clone}",
                file=sys.stderr
            )
            shutil.rmtree(clone, ignore_errors=True)
        else:
            raise RuntimeError(
                f"Clone already exists: {clone}"
            )

    CLONE_ROOT.mkdir(
        parents=True,
        exist_ok=True
    )

    result = subprocess.run(
        [
            "cp",
            "-cR",
            str(source),
            str(clone)
        ],
        text=True,
        capture_output=True
    )

    # macOS cp 可能因为 repo 内的 socket 给出警告，
    # 但真正的仓库 Clone 已经成功创建。
    if not (clone / ".git").exists():
        raise RuntimeError(
            result.stderr.strip()
            or "Clone failed"
        )

    # A linked worktree's .git file points back to the source HEAD/index.
    # Materialize independent metadata before any reset or branch operation.
    if (clone / ".git").is_file():
        with tempfile.TemporaryDirectory(prefix="herdr-git-") as temporary:
            independent = Path(temporary) / "repo"
            subprocess.run(
                ["git", "clone", "--no-hardlinks", "--no-checkout", str(source), str(independent)],
                check=True, capture_output=True, text=True,
            )
            # Keep source local branch identities (e.g. a pinned dev baseline).
            # clone otherwise maps every non-current local branch to origin/*.
            subprocess.run(
                ["git", "-C", str(independent), "fetch", "--update-head-ok", "--no-tags", str(source),
                 "refs/heads/*:refs/heads/*"],
                check=True, capture_output=True, text=True,
            )
            origin = subprocess.run(
                ["git", "-C", str(source), "remote", "get-url", "origin"],
                capture_output=True, text=True,
            )
            if origin.returncode == 0:
                subprocess.run(
                    ["git", "-C", str(independent), "remote", "set-url", "origin", origin.stdout.strip()],
                    check=True, capture_output=True, text=True,
                )
            hooks = subprocess.run(
                ["git", "-C", str(source), "config", "--get", "core.hooksPath"],
                capture_output=True, text=True,
            )
            if hooks.returncode == 0:
                subprocess.run(
                    ["git", "-C", str(independent), "config", "core.hooksPath", hooks.stdout.strip()],
                    check=True, capture_output=True, text=True,
                )
            (clone / ".git").unlink()
            shutil.move(str(independent / ".git"), str(clone / ".git"))

    if result.stderr.strip():
        print(
            f"[CLONE WARNING] {result.stderr.strip()}",
            file=sys.stderr
        )

    return clone


def create_task_branch(clone, task_id, agent, task_type, base_branch):
    slug = task_id.lower().replace("_", "-")
    branch = f"agent/{agent}/{task_type}-{slug}"
    ensure_branch_available(branch, _registered_tasks(), task_id=task_id)

    fetch = subprocess.run(
        [
            "git", "-C", str(clone),
            "fetch", "origin", base_branch
        ],
        text=True,
        capture_output=True
    )

    remote_exists = subprocess.run(
        [
            "git", "-C", str(clone),
            "show-ref", "--verify", "--quiet",
            f"refs/remotes/origin/{base_branch}"
        ]
    ).returncode == 0

    local_exists = subprocess.run(
        [
            "git", "-C", str(clone),
            "show-ref", "--verify", "--quiet",
            f"refs/heads/{base_branch}"
        ]
    ).returncode == 0

    if fetch.returncode == 0 and remote_exists:
        base_ref = f"origin/{base_branch}"
    elif local_exists:
        base_ref = base_branch
    else:
        raise RuntimeError(
            fetch.stderr.strip()
            or f"Base branch not found: {base_branch}"
        )

    sanitize_clone_sandbox(clone)

    result = subprocess.run(
        [
            "git", "-C", str(clone),
            "switch", "-c", branch, base_ref
        ],
        text=True,
        capture_output=True
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or result.stdout.strip()
        )

    return branch


def checkout_onto_branch(clone, onto_branch, *, candidate_sha=None):
    """检出既有分支(fix-loop 续接:commit 直落开放中的 PR 分支)。

    基线指纹在调用方紧随其后执行。普通续接完成 origin 同步；显式完整
    candidate SHA 则核对独立本地分支，保证既有提交不属于本任务变更。
    """
    ensure_branch_available(onto_branch, _registered_tasks())

    if pinned_local_onto_matches(clone, onto_branch, candidate_sha):
        sanitize_clone_sandbox(clone)
        result = subprocess.run(
            ["git", "-C", str(clone), "switch", onto_branch],
            text=True, capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        # Recheck after checkout; never advertise a moved branch as pinned.
        if not pinned_local_onto_matches(clone, onto_branch, candidate_sha):
            raise RuntimeError("Local candidate branch disappeared during checkout")
        return onto_branch

    fetch = subprocess.run(
        [
            "git", "-C", str(clone),
            "fetch", "origin", onto_branch
        ],
        text=True,
        capture_output=True
    )

    remote_exists = subprocess.run(
        [
            "git", "-C", str(clone),
            "rev-parse", "--verify", "--quiet",
            f"refs/remotes/origin/{onto_branch}"
        ]
    ).returncode == 0

    if fetch.returncode != 0 or not remote_exists:
        detail = fetch.stderr.strip() or fetch.stdout.strip()
        raise RuntimeError(
            f"Onto branch not found on origin: {onto_branch}"
            + (f"\n{detail}" if detail else "")
        )

    sanitize_clone_sandbox(clone)

    local_exists = subprocess.run(
        [
            "git", "-C", str(clone),
            "show-ref", "--verify", "--quiet",
            f"refs/heads/{onto_branch}"
        ]
    ).returncode == 0

    if local_exists:
        # 本地分支仅允许"领先"origin(未推送的续接提交);
        # 与 origin 分叉的陈旧本地分支会让任务落在错误基线上,fail-fast。
        ancestor = subprocess.run(
            [
                "git", "-C", str(clone),
                "merge-base", "--is-ancestor",
                f"origin/{onto_branch}", onto_branch,
            ]
        ).returncode == 0

        if not ancestor:
            raise RuntimeError(
                f"Local branch {onto_branch} diverged from "
                f"origin/{onto_branch}; delete or reset the local branch "
                "before launching onto it"
            )

        result = subprocess.run(
            [
                "git", "-C", str(clone),
                "switch", onto_branch
            ],
            text=True,
            capture_output=True
        )
    else:
        result = subprocess.run(
            [
                "git", "-C", str(clone),
                "switch", "-c", onto_branch,
                f"origin/{onto_branch}"
            ],
            text=True,
            capture_output=True
        )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or result.stdout.strip()
        )

    return onto_branch


def measure_complexity_baseline(clone):
    # HERDR_OPTIONAL_COMPLEXITY_GATE
    # Complexity gate is a per-repository capability, not a Herdr requirement.
    _herdr_repo = Path(clone).expanduser().resolve()
    _herdr_gate = _herdr_repo / "scripts" / "complexity-gate.cjs"
    if not _herdr_gate.is_file():
        return "disabled"
    result = subprocess.run(
        [
            "node",
            str(Path(clone) / "scripts" / "complexity-gate.cjs"),
            "--baseline-total",
            "999999",
            "--ignore-head-snapshot"
        ],
        cwd=str(clone),
        text=True,
        capture_output=True
    )

    output = result.stdout + "\n" + result.stderr

    import re

    match = re.search(
        r"当前实测:\s*(\d+)\s*条",
        output
    )

    if not match:
        raise RuntimeError(
            "无法取得 complexity baseline:\n"
            + output[-2000:]
        )

    return int(match.group(1))


def file_fingerprint(repo, relpath):
    path = Path(repo) / relpath

    if not path.exists() and not path.is_symlink():
        return "__MISSING__"

    if path.is_symlink():
        return "symlink:" + os.readlink(path)

    if not path.is_file():
        return "__NON_FILE__"

    h = hashlib.sha256()

    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)

            if not chunk:
                break

            h.update(chunk)

    return h.hexdigest()


def list_dirty_tracked(repo):
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "diff",
            "--name-only",
            "-z",
            "HEAD",
            "--"
        ],
        text=True,
        capture_output=True
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or result.stdout.strip()
        )

    return [
        item
        for item in result.stdout.split("\0")
        if item
    ]


# 不可变锚点契约:必须是 full 40-hex,禁用 `--short`(短 sha 无法与提交对象一一对应)。
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def capture_head_sha(clone):
    """采集基线锚点:分支检出完成那一刻的 HEAD sha。

    与 build_baseline_fingerprint 并列而不合并 —— 工作区指纹与 commit sha
    是两种证据,前者回答"改了什么",后者回答"从哪个提交起算"。
    调用方必须保证本函数执行于分支检出之后、`.agent-task-context` 落盘之前,
    否则 `--onto` 分支的既有提交会被误算进本任务区间。
    """
    result = subprocess.run(
        [
            "git", "-C", str(clone),
            "rev-parse", "HEAD"
        ],
        text=True,
        capture_output=True,
        check=False
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or result.stdout.strip()
            or f"Failed to resolve HEAD for clone: {clone}"
        )

    sha = result.stdout.strip()

    if not FULL_SHA_RE.match(sha):
        raise RuntimeError(
            f"HEAD sha is not a full 40-hex object id: {sha!r} (clone={clone})"
        )

    return sha


def build_baseline_fingerprint(repo):
    tracked = {}

    for relpath in list_dirty_tracked(repo):
        tracked[relpath] = file_fingerprint(
            repo,
            relpath
        )

    untracked = {}
    from herdr.repo_hygiene import is_internal_untracked

    for relpath in list_untracked(repo):
        # Use the same internal artifact contract as adoption/evaluation.
        if is_internal_untracked(relpath):
            continue

        untracked[relpath] = file_fingerprint(
            repo,
            relpath
        )

    return {
        "tracked": tracked,
        "untracked": untracked
    }


def _rev_parse_head(repo):
    """Baseline anchor: HEAD sha right after branch checkout, before any work."""
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or result.stdout.strip() or "rev-parse HEAD failed"
        )
    return result.stdout.strip()


def list_untracked(repo):
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z"
        ],
        text=True,
        capture_output=True
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or result.stdout.strip()
        )

    return [
        item
        for item in result.stdout.split("\0")
        if item
    ]


def write_task_context(clone, agent, branch, shared_docs=None, mode="git", context=None):
    complexity_baseline = (
        "disabled"
        if mode == "context"
        else measure_complexity_baseline(clone)
    )

    ctx = Path(clone) / ".agent-task-context"

    lines = []
    if mode == "context":
        lines.append("mode=context")
    lines.extend([
        f"agent={agent}",
        f"branch={branch or '-'}",
        f"worktree={clone}",
        f"complexity_baseline={complexity_baseline}",
    ])
    for ctx_id in sorted(context or {}):
        lines.append(f"context.{ctx_id}={context[ctx_id]}")
    if shared_docs:
        lines.append(f"shared_docs={shared_docs}")

    ctx.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8"
    )

    return ctx, complexity_baseline


def create_pane(parent_pane, clone):
    data = run_json([
        "herdr",
        "pane",
        "split",
        parent_pane,
        "--direction",
        "right",
        "--cwd",
        str(clone),
        "--no-focus"
    ])

    return data["result"]["pane"]["pane_id"]


def prepare_existing_pane(pane_id, clone):
    result = subprocess.run(
        ["herdr", "pane", "run", pane_id, f"cd {shlex.quote(str(clone))}"],
        text=True,
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or result.stdout.strip() or
            f"Failed to prepare persistent pane: {pane_id}"
        )
    time.sleep(0.5)




def unique_agent_name(task_id, pane_id):
    base = re.sub(
        r"[^a-z0-9_-]+",
        "-",
        task_id.lower()
    ).strip("-_")

    if not base or not base[0].isalpha():
        base = "task-" + base

    suffix = hashlib.sha1(
        pane_id.encode("utf-8")
    ).hexdigest()[:6]

    max_base = 32 - 1 - len(suffix)

    return f"{base[:max_base]}-{suffix}"


def start_agent(task_id, agent_kind, pane_id, retries=10, delay=0.5):
    if agent_kind == "opencode" and (Path.home() / ".herdr-controller" / "opencode-disabled").exists():
        raise RuntimeError("opencode disabled locally; reroute before worker launch")
    last_err = None
    for attempt in range(retries):
        try:
            agent_name = unique_agent_name(
                task_id,
                pane_id
            )

            cmd = [
                "herdr",
                "agent",
                "start",
                agent_name,
                "--kind",
                agent_kind,
                "--pane",
                pane_id,
                "--timeout",
                "120000"
            ]

            if agent_kind in ("opencode", "kimi"):
                cmd += ["--", "--auto"]
            elif agent_kind in ("qodercli", "claude", "agy"):
                cmd += ["--", "--dangerously-skip-permissions"]
            elif agent_kind == "grok":
                cmd += ["--", "--always-approve"]

            data = run_json(cmd)
            return data["result"]["agent"]
        except RuntimeError as e:
            last_err = e
            if "agent_pane_busy" in str(e) and attempt < retries - 1:
                time.sleep(delay)
                continue
            raise
    raise last_err



def verify_request_preflight(agent_kind, cwd):
    """Real minimal request in the actual launch workspace, before Pane/start."""
    from herdr.deep_preflight import inspect
    rows = inspect({"project_root": str(cwd)}, deep=True, target_agents=[agent_kind])
    if len(rows) != 1 or rows[0].get("agent") != agent_kind:
        raise RuntimeError("Worker Deep Preflight UNKNOWN: unsupported agent")
    row = rows[0]
    if not row.get("request_verified") or not row.get("preflight_identity", {}).get("verifiable"):
        raise RuntimeError(f"Worker Deep Preflight {row.get('final_status', 'UNKNOWN')}: request not verified")
    return row


def wait_startup_ready(agent_kind, pane_id, started, attempts=6, total_timeout=20):
    """Bounded native identity -> owned transcript -> identity recheck gate."""
    from herdr.agent_adapter import startup_readiness
    deadline = time.monotonic() + total_timeout
    verdict = {"status": "UNKNOWN", "interactive_ready": False, "reason": "probe_failed"}
    for attempt in range(attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            runtime = run_json(["herdr", "agent", "get", pane_id], timeout=min(3, remaining))["result"]["agent"]
            verdict = startup_readiness(agent_kind, started, runtime, None)
            if verdict["reason"] != "transcript_unavailable":
                return verdict
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            transcript = subprocess.run(
                ["herdr", "pane", "read", pane_id, "--source", "visible", "--lines", "64"],
                text=True, capture_output=True, timeout=min(3, remaining),
            )
            if transcript.returncode != 0 or len(transcript.stdout) > 32768:
                verdict = {"status": "UNKNOWN", "interactive_ready": False, "reason": "transcript_unavailable"}
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                runtime = run_json(["herdr", "agent", "get", pane_id], timeout=min(3, remaining))["result"]["agent"]
                verdict = startup_readiness(agent_kind, started, runtime, transcript.stdout)
                if verdict["status"] != "UNKNOWN":
                    return verdict
        except (RuntimeError, ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired):
            verdict = {"status": "UNKNOWN", "interactive_ready": False, "reason": "probe_failed"}
        if attempt + 1 < attempts:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(.5, remaining))
    return verdict

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--launch-intent-id', default=None)
    parser.add_argument("--run-id", default=os.environ.get("HERDR_RUN_ID"),
                        help="Caller-owned run identity for startup failure evidence.")

    parser.add_argument(
        "--task-id",
        required=True
    )

    parser.add_argument(
        "--source",
        required=True
    )

    parser.add_argument(
        "--parent-pane"
    )

    parser.add_argument(
        "--pane-id"
    )

    parser.add_argument(
        "--agent",
        required=True
    )

    parser.add_argument(
        "--task-type",
        choices=[
            "fix",
            "feat",
            "refactor",
            "docs",
            "test",
            "chore",
            "perf",
            "ci"
        ],
        default="feat"
    )

    parser.add_argument(
        "--base-branch",
        required=False,
        default=None
    )

    parser.add_argument(
        "--execution-mode",
        choices=["git", "context"],
        default="git",
        help="git: CoW clone + branch (default); context: plain task workspace, no Git."
    )

    parser.add_argument(
        "--context-json",
        default=None,
        help="JSON object of context bindings {id: absolute_path} (context mode)."
    )

    parser.add_argument(
        "--shared-docs",
        default=None,
        help="Workflow shared document directory (outside the clone)."
    )

    parser.add_argument(
        "--onto",
        default=None,
        help="Checkout this existing branch instead of creating a task branch."
    )

    parser.add_argument("--candidate-sha", default=None,
                        help="Exact candidate pin for an unpublished local --onto branch.")

    args = parser.parse_args()

    context_bindings = {}
    if args.execution_mode == "context":
        if args.onto:
            # 无分支语义下静默丢弃 onto 会让调用方误以为续接成功。
            raise RuntimeError(
                f"--onto is not supported in context execution mode: {args.onto}"
            )
        context_bindings = json.loads(args.context_json or "{}")
    elif not args.base_branch:
        raise RuntimeError("--base-branch is required in git execution mode")

    clone = None
    agent_started = False
    agent_start_attempted = False
    agent = None
    pane_id = None
    readiness = None
    launch_identity = None
    if args.launch_intent_id:
        if not args.run_id or Path(args.task_id).name != args.task_id or args.task_id in {'.', '..'}:
            raise ValueError('launch intent requires bounded task/run identity')
        # New launches never heal/delete an unregistered prior resource. Its
        # durable intent must be reconciled first, including untagged crashes.
        if os.path.lexists(CLONE_ROOT / args.task_id):
            raise RuntimeError('launch workspace already exists; reconcile prior intent')
        launch_identity = {'intent_id': args.launch_intent_id, 'task_id': args.task_id,
                           'run_id': args.run_id, 'phase': 'workspace_created'}
    try:
        if args.execution_mode == "context":
            clone = create_context_task_workspace(
                args.task_id
            )
            if launch_identity:
                from herdr.task_resources import write_worker_launch_identity
                write_worker_launch_identity(clone, launch_identity, initial=True)
            print(f"[WORKSPACE] {clone}")
            branch = None
            # 无 Git 语义 ⇒ 无锚点;onto_branch 在 context 模式已被前置拒绝。
            baseline_commit = None
            baseline_fingerprint = {
                "tracked": {},
                "untracked": {}
            }
            baseline_untracked = []
            baseline_commit = None
        else:
            clone = create_clone(
                args.source,
                args.task_id
            )
            if launch_identity:
                from herdr.task_resources import write_worker_launch_identity
                write_worker_launch_identity(clone, launch_identity, initial=True)

            print(f"[CLONE] {clone}")

            if args.onto:
                # 必须先于 build_baseline_fingerprint:
                # PR 分支的既有提交不能被记入本任务的基线变更。
                branch = checkout_onto_branch(clone, args.onto, candidate_sha=args.candidate_sha)
            else:
                branch = create_task_branch(
                    clone,
                    args.task_id,
                    args.agent,
                    args.task_type,
                    args.base_branch
                )

            print(f"[BRANCH] {branch}")

            # 不可变锚点:分支检出(新建或 --onto)完成后的 HEAD。
            # 顺序铁律:必须在 sanitize/切换之后,否则会把 --onto 的既有提交
            # 算进本任务区间;必须在 write_task_context 之前,否则
            # .agent-task-context 会被记入锚点区间。
            baseline_commit = capture_head_sha(clone)

            print(f"[BASELINE COMMIT] {baseline_commit}")

            # 在写入 .agent-task-context 之前记录完整工作区基线。
            # 包括：
            # - Clone 创建时已经存在的 tracked 修改
            # - Clone 创建时已经存在的 untracked 文件
            baseline_fingerprint = build_baseline_fingerprint(
                clone
            )

            baseline_untracked = sorted(
                baseline_fingerprint["untracked"].keys()
            )

            print(
                f"[BASELINE] "
                f"tracked={len(baseline_fingerprint['tracked'])} "
                f"untracked={len(baseline_fingerprint['untracked'])}"
            )

            baseline_commit = _rev_parse_head(clone)

            print(f"[BASELINE COMMIT] {baseline_commit}")

        ctx, complexity_baseline = write_task_context(
            clone,
            args.agent,
            branch,
            shared_docs=args.shared_docs,
            mode=args.execution_mode,
            context=context_bindings
        )

        print(f"[CONTEXT] {ctx}")
        print(f"[COMPLEXITY BASELINE] {complexity_baseline}")

        preflight = verify_request_preflight(args.agent, clone)

        if args.pane_id:
            pane_id = args.pane_id
            prepare_existing_pane(pane_id, clone)
            pane_source = "prebuilt"
        else:
            if not args.parent_pane:
                raise RuntimeError(
                    "--parent-pane is required when --pane-id is not provided"
                )
            pane_id = create_pane(args.parent_pane, clone)
            pane_source = "dynamic"

        print(f"[PANE] {pane_id}")
        print(f"[PANE SOURCE] {pane_source}")
        if launch_identity:
            launch_identity.update(pane_id=pane_id, pane_source=pane_source, phase='agent_start_requested')
            write_worker_launch_identity(clone, launch_identity)

        agent_start_attempted = True
        agent = start_agent(
            args.task_id,
            args.agent,
            pane_id
        )

        agent_started = True
        if launch_identity:
            session = agent.get('agent_session')
            launch_identity.update(agent_session_id=session.get('value') if isinstance(session, dict) else session,
                                   agent_name=agent.get('name'), phase='agent_started')
            write_worker_launch_identity(clone, launch_identity)
        readiness = wait_startup_ready(args.agent, pane_id, agent)
        if not readiness.get("interactive_ready"):
            raise RuntimeError(f"Worker startup {readiness.get('status', 'UNKNOWN')}: {readiness.get('reason', 'unknown')}")

        session = agent.get("agent_session")
        agent_session_id = (
            session.get("value")
            if isinstance(session, dict)
            else session
        )
        if launch_identity:
            launch_identity.update(agent_session_id=agent_session_id, agent_name=agent.get('name'), phase='interactive_ready')
            write_worker_launch_identity(clone, launch_identity)

        result = {
            "task_id": args.task_id,
            "clone": str(clone),
            "branch": branch,
            "baseline_commit": baseline_commit,
            "onto_branch": getattr(args, "onto", None),
            "baseline_untracked": baseline_untracked,
            "baseline_fingerprint": baseline_fingerprint,
            "baseline_commit": baseline_commit if args.execution_mode != "context" else None,
            "onto_branch": args.onto,
            "pane_id": pane_id,
            "pane_source": pane_source,
            "agent": agent.get("agent"),
            "agent_name": agent.get("name"),
            "agent_session_id": agent_session_id,
            "status": "idle",
            "launch_intent_id": args.launch_intent_id,
            "request_verified": True,
            "interactive_ready": True,
            "preflight_identity": preflight.get("preflight_identity"),
            "startup_readiness": readiness
        }

        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2
            )
        )

        print(
            "HERDR_WORKER_RESULT="
            + json.dumps(
                result,
                ensure_ascii=False
            )
        )
    except Exception as exc:
        session = (agent or {}).get("agent_session")
        failure = {
            "launch_intent_id": args.launch_intent_id,
            "task_id": args.task_id, "run_id": args.run_id,
            "agent_name": (agent or {}).get("name"),
            "agent_session_id": session.get("value") if isinstance(session, dict) else session,
            "pane_id": pane_id, "clone": str(clone.resolve()) if clone else None,
            "agent_started": agent_started if agent_started else (None if agent_start_attempted else False),
            "agent_start_attempted": agent_start_attempted, "disposition": "unknown",
            "recovery_required": agent_start_attempted,
            "startup_status": (readiness or {}).get("status", "UNKNOWN"),
            "failure_type": type(exc).__name__,
        }
        # Expected identity is the start receipt, never the foreign queried instance.
        print("HERDR_WORKER_FAILURE=" + json.dumps(failure, sort_keys=True), file=sys.stderr, flush=True)
        if clone and clone.exists() and not agent_start_attempted and not is_task_active_in_registry(args.task_id):
            print(
                f"[WORKER ROLLBACK] Cleaning up incomplete clone: {clone}",
                file=sys.stderr
            )
            shutil.rmtree(clone, ignore_errors=True)
        raise


if __name__ == "__main__":
    main()
