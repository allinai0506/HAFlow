#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    from herdr.agent_binary import AGENT_BINARIES, resolve_binary
except ImportError:  # 直接以脚本方式运行: python3 herdr/deep_preflight.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from herdr.agent_binary import AGENT_BINARIES, resolve_binary

HOME = Path.home()
HERDR_DIR = HOME / "herdr"
ROOT = HOME / ".herdr-controller"
PROJECTS = ROOT / "projects.json"
POOLS = ROOT / "agent-pools.json"

AGENTS = list(AGENT_BINARIES)

# Known local config hints. Missing config is never treated as definitive auth failure
# unless the provider has a stable, known local auth file.
AUTH_HINTS = {
    "opencode": [HOME / ".config" / "opencode"],
    "codex": [HOME / ".codex" / "auth.json"],
    "claude": [HOME / ".claude.json"],
    "qodercli": [HOME / ".qoder-cn"],
    "agy": [],
    "pi": [HOME / ".pi" / "agent" / "auth.json"],
    "grok": [HOME / ".grok" / "auth.json"],
    "kimi": [HOME / ".kimi-code" / "credentials" / "kimi-code.json", HOME / ".kimi-code" / "config.toml"],
}

# These patterns are intentionally conservative. We only classify an error when
# the CLI itself reports a clear blocker.
TOKEN_PATTERNS = [
    r"token.*(exhaust|limit|quota)",
    r"(quota|credits?).*(exhaust|limit|insufficient)",
    r"insufficient.*(quota|credits?)",
    r"rate.?limit",
    r"usage limit",
    r"no.*tokens?",
    r"premium.*(limit|quota|request)",
    r"free\W*(tier|plan)?.*(limit|exhaust)",
    r"out of credits?",
    r"credit.*(balance|exhaust|insufficient|depleted)",
    r"billing.*(limit|exhaust|issue)",
    r"payment required",
    r"\b402\b",
]
AUTH_PATTERNS = [
    r"not logged in",
    r"authentication required",
    r"unauthorized",
    r"unauthenticated",
    r"invalid api key",
    r"invalid.*token",
    r"login required",
    r"please log in",
    r"api key.*(missing|not found|invalid|expired)",
    r"expired.*(key|token|credential)",
    r"access denied",
    r"forbidden",
    r"no auth",
    r"use /login to sign in",
    r"no model configured",
    r"\b401\b",
    r"\b403\b",
]
PROVIDER_PATTERNS = [
    r"overloaded",
    r"server error",
    r"internal server error",
    r"temporarily unavailable",
    r"service unavailable",
    r"bad gateway",
    r"gateway timeout",
    r"\b50[023]\b",
    r"model.*not found",
    r"provider.*error",
    r"connection (refused|reset|timed out|failed)",
    r"network.*(error|unreachable|timeout)",
    r"econn(refused|reset|timedout)",
]
# CLI-local infrastructure failures (never a model/auth/quota verdict).
# NOTE: bare "skill conflict" warnings also appear in SUCCESSFUL runs, so only
# fatal startup signatures are matched here.
LOCAL_PATTERNS = [
    r"watcher did not become ready",
    r"unexpected critical error",
    r"\benoent\b",
    r"\beacces\b",
    r"\beperm\b",
]
TRUST_PATTERNS = [
    r"do you trust",
    r"project you created or one you trust",
    r"workspace trust",
]
UPDATE_PATTERNS = [
    r"update available",
    r"upgrade available",
]


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def project_by_root(root):
    root = str(Path(root).expanduser().resolve())
    ps = load_json(PROJECTS, {"projects": {}}).get("projects", {})
    if isinstance(ps, dict):
        for pid, p in ps.items():
            if p.get("project_root") == root:
                row = dict(p)
                row.setdefault("project_id", pid)
                return row
    return None


def current_project():
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            timeout=5,
        )
        if r.returncode == 0:
            return project_by_root(r.stdout.strip())
    except Exception:
        pass
    return None


def project_by_id(project_id):
    ps = load_json(PROJECTS, {"projects": {}}).get("projects", {})

    if isinstance(ps, dict):
        if project_id in ps and isinstance(ps[project_id], dict):
            p = dict(ps[project_id])
            p.setdefault("project_id", project_id)
            return p

        for key, value in ps.items():
            if not isinstance(value, dict):
                continue
            if value.get("project_id") == project_id:
                p = dict(value)
                p.setdefault("project_id", key)
                return p

    if isinstance(ps, list):
        for value in ps:
            if isinstance(value, dict) and value.get("project_id") == project_id:
                return dict(value)

    return None


def project_pool(project_id):
    pools = load_json(POOLS, {"projects": {}}).get("projects", {})
    return pools.get(project_id, {}) if isinstance(pools, dict) else {}


def set_disabled(project_id, agent, disabled):
    data = load_json(POOLS, {"projects": {}})
    pool = data.setdefault("projects", {}).setdefault(project_id, {})
    current = set(pool.get("disabled_agents", []))
    if disabled:
        current.add(agent)
    else:
        current.discard(agent)
    pool["disabled_agents"] = sorted(current)
    save_json(POOLS, data)


def run(cmd, timeout=12, cwd=None, stdin=None):
    try:
        return subprocess.run(
            cmd,
            text=True,
            input=stdin if stdin is not None else "",
            capture_output=True,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return {
            "timeout": True,
            "stdout": stdout,
            "stderr": stderr,
            "returncode": None,
        }


def normalize_result(result):
    if isinstance(result, dict):
        stdout = result.get("stdout")
        stderr = result.get("stderr")
        stdout = stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes) else (stdout or "")
        stderr = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else (stderr or "")
        return {
            "timeout": bool(result.get("timeout")),
            "stdout": stdout,
            "stderr": stderr,
            "returncode": result.get("returncode"),
        }
    stdout = result.stdout.decode("utf-8", errors="replace") if isinstance(result.stdout, bytes) else (result.stdout or "")
    stderr = result.stderr.decode("utf-8", errors="replace") if isinstance(result.stderr, bytes) else (result.stderr or "")
    return {
        "timeout": False,
        "stdout": stdout,
        "stderr": stderr,
        "returncode": result.returncode,
    }


def classify_text(text):
    low = text.lower()
    checks = [
        ("TOKEN_EXHAUSTED", TOKEN_PATTERNS),
        ("AUTH_REQUIRED", AUTH_PATTERNS),
        ("PROVIDER_ERROR", PROVIDER_PATTERNS),
        ("LOCAL_ERROR", LOCAL_PATTERNS),
        ("TRUST_REQUIRED", TRUST_PATTERNS),
        ("UPDATE_BLOCKED", UPDATE_PATTERNS),
    ]
    for status, patterns in checks:
        if any(re.search(p, low, re.I) for p in patterns):
            return status
    return None


# Per-agent smoke timeouts (seconds). Claude cold start routinely needs 35-60s
# (measured 36.9s success, occasional >60s flake), so a flat 35s timeout
# deterministically misreports healthy-but-slow as TIMEOUT.
SMOKE_TIMEOUTS = {
    "claude": 90,
}
DEFAULT_SMOKE_TIMEOUT = 40


def auth_hint(agent):
    paths = AUTH_HINTS.get(agent, [])
    if not paths:
        return "unknown"
    return "present" if any(p.exists() for p in paths) else "missing"


def version_probe(agent, binary):
    res = normalize_result(run([binary, "--version"], timeout=8))
    text = (res["stdout"] or res["stderr"]).strip()
    if res["timeout"]:
        return False, "timeout"
    return res["returncode"] == 0, (text.splitlines()[0][:160] if text else "")


def help_probe(binary):
    for args in (["--help"], ["help"]):
        res = normalize_result(run([binary] + args, timeout=8))
        text = (res["stdout"] + "\n" + res["stderr"]).strip()
        if text:
            return text
    return ""


def choose_smoke_command(agent, binary, cwd):
    """
    Return a conservative non-interactive smoke command only when we know the
    installed CLI exposes a supported non-interactive mode.

    This deliberately avoids inventing flags for unknown CLIs.
    """
    help_text = help_probe(binary).lower()
    prompt = "Reply with exactly HERDR_PREFLIGHT_OK and nothing else."

    if agent == "codex":
        # Codex CLI supports `exec` for non-interactive execution.
        if re.search(r"\bexec\b", help_text):
            return [binary, "exec", "--skip-git-repo-check", prompt], "codex exec"

    if agent == "claude":
        # Claude Code commonly exposes --print / -p.
        cmd = None
        if "--print" in help_text:
            cmd = [binary, "--print"]
        elif re.search(r"(^|\s)-p([,\s]|$)", help_text):
            cmd = [binary, "-p"]

        if cmd:
            if "--permission-mode" in help_text:
                cmd.extend(["--permission-mode", "bypassPermissions"])
            elif "--dangerously-skip-permissions" in help_text:
                cmd.append("--dangerously-skip-permissions")
            if "--no-session-persistence" in help_text:
                cmd.append("--no-session-persistence")
            cmd.append(prompt)
            return cmd, "claude --print"

    if agent == "opencode":
        # OpenCode exposes `run` in current CLI builds.
        if re.search(r"\brun\b", help_text):
            return [binary, "run", prompt], "opencode run"

    if agent == "qodercli":
        # Qoder CN explicitly documents -p/--print as non-interactive.
        # Keep the probe minimal; extra tool flags can trigger config/skill side effects.
        if "--print" in help_text:
            return [
                binary,
                "--print",
                "--no-session-persistence",
                prompt,
            ], "qodercn --print"

    if agent == "agy":
        # AGY explicitly documents -p/--print as non-interactive.
        # Minimal print mode is sufficient for auth/quota/readiness validation.
        if "--print" in help_text:
            return [
                binary,
                "--print",
                prompt,
            ], "agy --print"

    if agent == "pi":
        # Pi documents `-p/--print` as non-interactive mode; `--no-session`
        # keeps the probe ephemeral (no session persistence side effects).
        if "--print" in help_text:
            return [
                binary,
                "--print",
                "--no-session",
                prompt,
            ], "pi --print"

    if agent == "grok":
        # Grok CLI exposes `-p/--single` as single-turn non-interactive mode.
        if re.search(r"(^|\s)-p([,\s]|$)", help_text) or "--single" in help_text:
            return [
                binary,
                "-p",
                prompt,
            ], "grok -p"

    if agent == "kimi":
        # Kimi CLI exposes `-p/--prompt` as single-prompt non-interactive mode.
        if re.search(r"(^|\s)-p([,\s]|$)", help_text) or "--prompt" in help_text:
            return [
                binary,
                "-p",
                prompt,
            ], "kimi -p"

    return None, "no safe non-interactive adapter"


def smoke_probe(agent, binary, cwd, timeout=None):
    cmd, adapter = choose_smoke_command(agent, binary, cwd)
    if not cmd:
        return {
            "attempted": False,
            "adapter": adapter,
            "status": "UNKNOWN",
            "note": "未执行真实请求：当前版本没有确认安全的非交互调用方式",
            "output": "",
        }

    limit = timeout or SMOKE_TIMEOUTS.get(agent, DEFAULT_SMOKE_TIMEOUT)
    # Retry policy (single sample must not condemn a flaky-but-healthy CLI):
    # - claude TIMEOUT: cold start routinely needs 35-60s, always allow 1 retry;
    # - fast PROVIDER_ERROR (elapsed <= 15s): overload/503 blips reject fast,
    #   a cheap retry distinguishes blip from sustained outage.
    # Generic ERROR and slow failures stay single-sample: ERROR has unknown
    # cost/cause, and slow attempts already spent the time budget.
    FAST_RETRY_BUDGET = 15
    attempt = 0
    while True:
        attempt += 1
        started = time.time()
        res = normalize_result(run(cmd, timeout=limit, cwd=cwd))
        elapsed = round(time.time() - started, 2)
        combined = (res["stdout"] + "\n" + res["stderr"]).strip()
        classification = classify_text(combined)

        if res["timeout"]:
            if agent in SMOKE_TIMEOUTS and attempt == 1:
                time.sleep(2)
                continue
            note = f"真实最小调用超时 ({elapsed}s, 超时阈值 {limit}s)"
            if agent in SMOKE_TIMEOUTS:
                note += "；已重试 1 次仍超时，可能是慢而非不可用"
            return {
                "attempted": True,
                "adapter": adapter,
                "status": "TIMEOUT",
                "note": note,
                "output": combined[-1200:],
            }

        if classification == "PROVIDER_ERROR" and attempt == 1 and elapsed <= FAST_RETRY_BUDGET:
            time.sleep(2)
            continue

        if classification:
            note = f"CLI 报告阻塞 ({elapsed}s)"
            if attempt > 1:
                note += "；第 2 次重试仍阻塞"
            return {
                "attempted": True,
                "adapter": adapter,
                "status": classification,
                "note": note,
                "output": combined[-1200:],
            }

        if res["returncode"] == 0:
            exact_marker = any(
                line.strip() == "HERDR_PREFLIGHT_OK"
                for line in combined.splitlines()
            )
            note = (
                f"真实最小调用成功，协议标记精确 ({elapsed}s)"
                if exact_marker
                else f"真实最小调用成功，输出受项目规则影响 ({elapsed}s)"
            )
            if attempt > 1:
                note += "；第 2 次重试成功"
            return {
                "attempted": True,
                "adapter": adapter,
                "status": "READY",
                "note": note,
                "output": combined[-1200:],
            }

        return {
            "attempted": True,
            "adapter": adapter,
            "status": "ERROR",
            "note": f"真实最小调用失败 code={res['returncode']} ({elapsed}s)",
            "output": combined[-1200:],
        }


def inspect(project, deep=False, target_agents=None):
    project_id = project.get("project_id") if project else None
    project_root = project.get("project_root") if project else os.getcwd()
    pool = project_pool(project_id) if project_id else {}
    allowed = pool.get("allowed_agents", AGENTS)
    disabled = set(pool.get("disabled_agents", []))

    if target_agents:
        target_set = set(target_agents)
        filtered = [a for a in allowed if a in target_set]
        allowed = filtered if filtered else [a for a in target_agents if a in AGENTS]

    rows = []
    for agent in allowed:
        binary_name = AGENT_BINARIES.get(agent, agent)
        binary = resolve_binary(binary_name)
        row = {
            "agent": agent,
            "binary_name": binary_name,
            "binary": binary,
            "disabled": agent in disabled,
            "auth_hint": auth_hint(agent),
            "version": "",
            "shallow_status": "",
            "deep": None,
        }

        if not binary:
            row["shallow_status"] = "MISSING"
            row["final_status"] = "DISABLED" if agent in disabled else "MISSING"
            rows.append(row)
            continue

        ok, version = version_probe(agent, binary)
        row["version"] = version
        if agent in disabled:
            row["shallow_status"] = "DISABLED"
        elif not ok:
            row["shallow_status"] = "WARN"
        elif row["auth_hint"] == "missing":
            row["shallow_status"] = "WARN"
        else:
            row["shallow_status"] = "READY"

        if deep and agent not in disabled and binary:
            row["deep"] = smoke_probe(agent, binary, project_root)

        if agent in disabled:
            row["final_status"] = "DISABLED"
        elif row["deep"] and row["deep"]["attempted"]:
            row["final_status"] = row["deep"]["status"]
        elif row["shallow_status"] in {"MISSING", "WARN"}:
            row["final_status"] = row["shallow_status"]
        else:
            row["final_status"] = "READY" if not deep else "UNKNOWN"

        rows.append(row)

    return rows


def print_table(rows, project_id, deep):
    print()
    print(f"Project: {project_id or '(unregistered)'}")
    print(f"Mode: {'DEEP' if deep else 'SHALLOW'}")
    print("=" * 112)
    print(f"{'Agent':<12} {'Final':<17} {'CLI':<8} {'Auth':<9} {'Adapter / Version'}")
    print("-" * 112)
    for r in rows:
        deep_info = r.get("deep")
        adapter = deep_info.get("adapter") if deep_info else ""
        note = r.get("version") or r.get("binary") or "not installed"
        if deep_info:
            note = f"{adapter} | {deep_info.get('note','')}"
        cli = "OK" if r.get("binary") else "MISS"
        print(f"{r['agent']:<12} {r['final_status']:<17} {cli:<8} {r['auth_hint']:<9} {note[:64]}")
    print()

    if deep:
        unknown = [r["agent"] for r in rows if r["final_status"] == "UNKNOWN"]
        if unknown:
            print("未做真实请求的 Agent:", ", ".join(unknown))
            print("原因：当前版本没有确认安全的非交互调用适配器，不会猜测 CLI 参数。")
            print()

        bad = [
            r for r in rows
            if r["final_status"] in {
                "TOKEN_EXHAUSTED", "AUTH_REQUIRED", "PROVIDER_ERROR",
                "LOCAL_ERROR", "TRUST_REQUIRED",
                "UPDATE_BLOCKED", "TIMEOUT", "ERROR", "MISSING"
            }
        ]
        if bad:
            print("建议从 Router 候选中暂时禁用:")
            print("  " + ", ".join(r["agent"] for r in bad))
        else:
            print("没有检测到明确的运行阻塞。")


def main():
    ap = argparse.ArgumentParser(description="Herdr Factory Deep Agent Preflight")
    ap.add_argument("--project-id")
    ap.add_argument("--project-root")
    ap.add_argument("--agent", action="append", dest="agents", help="Check specific agent(s) only")
    ap.add_argument("--deep", action="store_true", help="Perform minimal real provider calls where a safe adapter is known")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--auto-disable", action="store_true", help="Disable only agents with explicit hard failures from deep probes")
    args = ap.parse_args()

    if args.project_id:
        project = project_by_id(args.project_id)
    elif args.project_root:
        project = project_by_root(args.project_root)
    else:
        project = current_project()

    if not project:
        raise SystemExit("当前目录不是已注册 Factory 项目；请进入项目目录或传 --project-id/--project-root")

    target_agents = []
    if args.agents:
        for item in args.agents:
            target_agents.extend([x.strip() for x in item.split(",") if x.strip()])

    rows = inspect(project, deep=args.deep, target_agents=target_agents or None)

    if args.auto_disable and args.deep:
        hard = {
            "TOKEN_EXHAUSTED", "AUTH_REQUIRED", "PROVIDER_ERROR",
            "TRUST_REQUIRED",
            "UPDATE_BLOCKED", "ERROR", "MISSING"
        }
        changed = []
        for r in rows:
            if r["final_status"] in hard and not r["disabled"]:
                set_disabled(project["project_id"], r["agent"], True)
                changed.append(r["agent"])
        if changed:
            print("AUTO_DISABLED:", ", ".join(changed))

    if args.json:
        print(json.dumps({
            "project_id": project["project_id"],
            "project_root": project["project_root"],
            "mode": "deep" if args.deep else "shallow",
            "agents": rows,
        }, ensure_ascii=False, indent=2))
    else:
        print_table(rows, project["project_id"], args.deep)


if __name__ == "__main__":
    main()
