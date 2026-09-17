#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    from herdr.agent_binary import resolve_agent_binary
except ImportError:  # 直接以脚本方式运行: python3 herdr/preflight.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from herdr.agent_binary import resolve_agent_binary

HOME = Path.home()
ROOT = HOME / ".herdr-controller"
POOLS = ROOT / "agent-pools.json"
PROJECTS = ROOT / "projects.json"

KNOWN_AGENTS = ["opencode", "codex", "claude", "qodercli", "agy", "pi", "grok", "kimi"]

AUTH_HINTS = {
    "codex": [HOME / ".codex" / "auth.json"],
    "claude": [HOME / ".claude.json"],
    "pi": [HOME / ".pi" / "agent" / "auth.json"],
    "grok": [HOME / ".grok" / "auth.json"],
    "opencode": [HOME / ".config" / "opencode"],
    "qodercli": [HOME / ".qoder-cn"],
    "agy": [],
    "kimi": [HOME / ".kimi-code" / "credentials" / "kimi-code.json", HOME / ".kimi-code" / "config.toml"],
}

VERSION_ARGS = {
    "opencode": ["--version"],
    "codex": ["--version"],
    "claude": ["--version"],
    "qodercli": ["--version"],
    "agy": ["--version"],
    "pi": ["--version"],
    "grok": ["--version"],
    "kimi": ["--version"],
}

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
    projects = load_json(PROJECTS, {"projects": {}}).get("projects", {})
    if isinstance(projects, dict):
        for pid, p in projects.items():
            if p.get("project_root") == root:
                x = dict(p)
                x.setdefault("project_id", pid)
                return x
    return None

def current_project():
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            text=True, capture_output=True, timeout=5
        )
        if root.returncode == 0:
            return project_by_root(root.stdout.strip())
    except Exception:
        pass
    return None

def project_pool(project_id):
    pools = load_json(POOLS, {"projects": {}}).get("projects", {})
    return pools.get(project_id, {}) if isinstance(pools, dict) else {}

def probe_version(agent, binary):
    args = VERSION_ARGS.get(agent, ["--version"])
    try:
        r = subprocess.run(
            [binary] + args,
            text=True,
            capture_output=True,
            timeout=8,
        )
        text = (r.stdout or r.stderr or "").strip().splitlines()
        first = text[0] if text else ""
        return r.returncode == 0, first[:160]
    except subprocess.TimeoutExpired:
        return False, "version probe timeout"
    except Exception as e:
        return False, str(e)

def auth_hint(agent):
    paths = AUTH_HINTS.get(agent, [])
    if not paths:
        return "unknown", []
    existing = [str(p) for p in paths if p.exists()]
    return ("present" if existing else "missing"), existing

def inspect(project_id=None):
    pool = project_pool(project_id) if project_id else {}
    allowed = pool.get("allowed_agents", KNOWN_AGENTS)
    disabled = set(pool.get("disabled_agents", []))

    rows = []
    for agent in allowed:
        binary = resolve_agent_binary(agent)
        auth_state, auth_paths = auth_hint(agent)

        if not binary:
            status = "MISSING"
            version_ok = False
            version = ""
        else:
            version_ok, version = probe_version(agent, binary)
            if agent in disabled:
                status = "DISABLED"
            elif not version_ok:
                status = "WARN"
            elif auth_state == "missing":
                status = "WARN"
            else:
                status = "READY"

        rows.append({
            "agent": agent,
            "status": status,
            "binary": binary,
            "version_ok": version_ok,
            "version": version,
            "auth_hint": auth_state,
            "auth_paths": auth_paths,
            "disabled": agent in disabled,
        })
    return rows

def update_disabled(project_id, agent, disabled):
    if agent not in KNOWN_AGENTS:
        raise SystemExit(f"Unknown agent: {agent}")

    data = load_json(POOLS, {"projects": {}})
    projects = data.setdefault("projects", {})
    pool = projects.setdefault(project_id, {})
    current = set(pool.get("disabled_agents", []))

    if disabled:
        current.add(agent)
    else:
        current.discard(agent)

    pool["disabled_agents"] = sorted(current)
    save_json(POOLS, data)

def print_table(rows, project_id):
    print()
    print(f"Project: {project_id or '(global probe)'}")
    print("=" * 88)
    print(f"{'Agent':<12} {'Status':<10} {'Auth':<9} {'Version / Note'}")
    print("-" * 88)
    for r in rows:
        note = r["version"] or r["binary"] or "not installed"
        print(f"{r['agent']:<12} {r['status']:<10} {r['auth_hint']:<9} {note}")
    print()

    bad = [r for r in rows if r["status"] in {"MISSING", "WARN"}]
    disabled = [r for r in rows if r["status"] == "DISABLED"]
    ready = [r for r in rows if r["status"] == "READY"]

    print(f"READY={len(ready)}  WARN/MISSING={len(bad)}  DISABLED={len(disabled)}")
    print()
    print("注意：当前版本不会发送真实 LLM 请求，因此不能可靠判断“token/额度已耗尽”。")
    print("它负责检查：CLI 是否存在、是否能正常启动版本探针、已知认证文件、Factory disabled_agents。")

def main():
    ap = argparse.ArgumentParser(description="Herdr Factory Agent Preflight")
    ap.add_argument("--project-id")
    ap.add_argument("--project-root")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--disable", choices=KNOWN_AGENTS)
    ap.add_argument("--enable", choices=KNOWN_AGENTS)
    args = ap.parse_args()

    project = None
    if args.project_id:
        projects = load_json(PROJECTS, {"projects": {}}).get("projects", {})
        p = projects.get(args.project_id) if isinstance(projects, dict) else None
        if p:
            project = dict(p)
            project.setdefault("project_id", args.project_id)
    elif args.project_root:
        project = project_by_root(args.project_root)
    else:
        project = current_project()

    project_id = project.get("project_id") if project else None

    if args.disable or args.enable:
        if not project_id:
            raise SystemExit("Disable/enable requires a registered project. Run inside the project or pass --project-id.")
        update_disabled(project_id, args.disable or args.enable, bool(args.disable))
        print(f"{args.disable or args.enable}: {'DISABLED' if args.disable else 'ENABLED'} for {project_id}")

    rows = inspect(project_id)
    if args.json:
        print(json.dumps({
            "project_id": project_id,
            "agents": rows,
        }, ensure_ascii=False, indent=2))
    else:
        print_table(rows, project_id)

if __name__ == "__main__":
    main()
