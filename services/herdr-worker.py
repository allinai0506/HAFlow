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
from pathlib import Path

try:
    from herdr.git_coordination import ensure_branch_available
except ImportError:
    from herdr_git_coordination import ensure_branch_available


CLONE_ROOT = Path(
    os.environ.get("HERDR_CLONES_DIR")
    or (Path.home() / ".herdr-controller" / "clones")
)


def run_json(cmd):
    result = subprocess.run(
        cmd,
        text=True,
        capture_output=True
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or result.stdout.strip()
        )

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
    subprocess.run(["git", "-C", str(clone), "clean", "-fd"], capture_output=True)


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


def checkout_onto_branch(clone, onto_branch):
    """检出既有分支(fix-loop 续接:commit 直落开放中的 PR 分支)。

    基线指纹在调用方紧随其后执行,因此本函数必须完成 origin 同步,
    保证 PR 分支的既有提交不属于本任务基线。
    """
    ensure_branch_available(onto_branch, _registered_tasks())

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


def build_baseline_fingerprint(repo):
    tracked = {}

    for relpath in list_dirty_tracked(repo):
        tracked[relpath] = file_fingerprint(
            repo,
            relpath
        )

    untracked = {}

    for relpath in list_untracked(repo):
        # Controller 自己的上下文文件不属于业务变化
        if relpath == ".agent-task-context":
            continue

        untracked[relpath] = file_fingerprint(
            repo,
            relpath
        )

    return {
        "tracked": tracked,
        "untracked": untracked
    }


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
    )
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or result.stdout.strip() or
            f"Failed to prepare persistent pane: {pane_id}"
        )
    time.sleep(0.5)



def ensure_claude_workspace_trust(repo):
    repo = str(Path(repo).expanduser().resolve())

    config = Path.home() / ".claude.json"

    if config.exists():
        data = json.loads(
            config.read_text(encoding="utf-8")
        )
    else:
        data = {}

    projects = data.setdefault(
        "projects",
        {}
    )

    project = projects.setdefault(
        repo,
        {}
    )

    project["hasTrustDialogAccepted"] = True

    tmp = config.with_suffix(".json.tmp")

    tmp.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ) + "\n",
        encoding="utf-8"
    )

    tmp.replace(config)

    print(
        f"[CLAUDE PREFLIGHT] trusted={repo}"
    )


def ensure_grok_workspace_trust(repo):
    repo = str(Path(repo).expanduser().resolve())
    config = Path.home() / ".grok" / "trusted_folders.toml"
    try:
        content = config.read_text(encoding="utf-8") if config.exists() else ""
        header = f'[folders."{repo}"]'
        if header not in content:
            entry = f'\n[folders."{repo}"]\ntrusted = true\ndecided_at = {int(time.time())}\n'
            config.parent.mkdir(parents=True, exist_ok=True)
            tmp = config.with_suffix(".toml.tmp")
            tmp.write_text((content.rstrip() + "\n" + entry).lstrip(), encoding="utf-8")
            tmp.replace(config)
        print(f"[GROK PREFLIGHT] trusted={repo}")
    except Exception as e:
        print(f"[GROK PREFLIGHT ERROR] failed to trust {repo}: {e}")


def ensure_kimi_workspace_trust(repo):
    repo = str(Path(repo).expanduser().resolve())
    trust_dir = Path.home() / ".kimi-code" / "workspace-trust"
    try:
        trust_dir.mkdir(parents=True, exist_ok=True)
        user = os.environ.get("USER", "user")
        h = hashlib.sha256(repo.encode("utf-8")).hexdigest()[:12]
        filename = f"wd_{user}_{h}"
        target = trust_dir / filename
        data = {"root": repo, "trustedAt": int(time.time() * 1000)}
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
        print(f"[KIMI PREFLIGHT] trusted={repo}")
    except Exception as e:
        print(f"[KIMI PREFLIGHT ERROR] failed to trust {repo}: {e}")


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
    # Temporary safety valve: OpenCode/Bun has crashed on this machine.
    # Remove ~/.herdr-controller/opencode-disabled to re-enable OpenCode Workers.
    if (
        agent_kind == "opencode"
        and (Path.home() / ".herdr-controller" / "opencode-disabled").exists()
    ):
        agent_kind = "pi"
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


def main():
    parser = argparse.ArgumentParser()

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
    try:
        if args.execution_mode == "context":
            clone = create_context_task_workspace(
                args.task_id
            )
            print(f"[WORKSPACE] {clone}")
            branch = None
            baseline_fingerprint = {
                "tracked": {},
                "untracked": {}
            }
            baseline_untracked = []
        else:
            clone = create_clone(
                args.source,
                args.task_id
            )

            print(f"[CLONE] {clone}")

            if args.onto:
                # 必须先于 build_baseline_fingerprint:
                # PR 分支的既有提交不能被记入本任务的基线变更。
                branch = checkout_onto_branch(clone, args.onto)
            else:
                branch = create_task_branch(
                    clone,
                    args.task_id,
                    args.agent,
                    args.task_type,
                    args.base_branch
                )

            print(f"[BRANCH] {branch}")

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

        if args.agent == "claude":
            ensure_claude_workspace_trust(
                clone
            )
        elif args.agent == "grok":
            ensure_grok_workspace_trust(
                clone
            )
        elif args.agent == "kimi":
            ensure_kimi_workspace_trust(
                clone
            )

        agent = start_agent(
            args.task_id,
            args.agent,
            pane_id
        )

        session = agent.get("agent_session")
        agent_session_id = (
            session.get("value")
            if isinstance(session, dict)
            else session
        )

        result = {
            "task_id": args.task_id,
            "clone": str(clone),
            "branch": branch,
            "baseline_untracked": baseline_untracked,
            "baseline_fingerprint": baseline_fingerprint,
            "pane_id": pane_id,
            "pane_source": pane_source,
            "agent": agent.get("agent"),
            "agent_name": agent.get("name"),
            "agent_session_id": agent_session_id,
            "status": agent.get("agent_status")
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
    except Exception:
        if clone and clone.exists() and not is_task_active_in_registry(args.task_id):
            print(
                f"[WORKER ROLLBACK] Cleaning up incomplete clone: {clone}",
                file=sys.stderr
            )
            shutil.rmtree(clone, ignore_errors=True)
        raise


if __name__ == "__main__":
    main()
