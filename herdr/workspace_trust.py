"""Standardized workspace trust configuration for CLI agents (Grok, Claude, etc.).

Pre-seeds permissions to avoid background modal/TTY authorization popups.
"""

import json
import os
import sys
import time
from pathlib import Path


def ensure_grok_workspace_trust(repo_path: Path | str) -> bool:
    """Register workspace path in ~/.grok/trusted_folders.toml."""
    try:
        resolved = str(Path(repo_path).expanduser().resolve())
        config = Path.home() / ".grok" / "trusted_folders.toml"
        content = config.read_text(encoding="utf-8") if config.exists() else ""
        header = f'[folders."{resolved}"]'
        if header not in content:
            entry = f'\n[folders."{resolved}"]\ntrusted = true\ndecided_at = {int(time.time())}\n'
            config.parent.mkdir(parents=True, exist_ok=True)
            tmp = config.with_suffix(".toml.tmp")
            tmp.write_text((content.rstrip() + "\n" + entry).lstrip(), encoding="utf-8")
            tmp.replace(config)
        return True
    except Exception as exc:
        print(f"[WORKSPACE TRUST WARN] grok trust failed for {repo_path}: {exc}", file=sys.stderr)
        return False


def ensure_claude_workspace_trust(repo_path: Path | str) -> bool:
    """Register workspace path in ~/.claude.json."""
    try:
        resolved = str(Path(repo_path).expanduser().resolve())
        config = Path.home() / ".claude.json"
        data = {}
        if config.exists():
            try:
                data = json.loads(config.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        trusted = data.setdefault("trustedDirectories", [])
        if resolved not in trusted:
            trusted.append(resolved)
            config.parent.mkdir(parents=True, exist_ok=True)
            tmp = config.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            tmp.replace(config)
        return True
    except Exception as exc:
        print(f"[WORKSPACE TRUST WARN] claude trust failed for {repo_path}: {exc}", file=sys.stderr)
        return False


_TRUST_HANDLERS = {
    "grok": ensure_grok_workspace_trust,
    "claude": ensure_claude_workspace_trust,
}


def ensure_workspace_trust(repo_path: Path | str, agents: list[str] | None = None) -> bool:
    """Ensure workspace trust for specified agents or all supported agents."""
    targets = agents if agents is not None else list(_TRUST_HANDLERS.keys())
    success = True
    for agent in targets:
        handler = _TRUST_HANDLERS.get(agent)
        if handler:
            if not handler(repo_path):
                success = False
    return success


def preseed_trust_target(clone_path, agent):
    """Decide whether launch should pre-seed trust for this clone+agent.

    Mode-independent: any agent start in a fresh clone needs trust, so the
    integration mode must not gate this. Agents without a trust handler
    (e.g. codex) and empty paths skip.
    """
    if not clone_path or agent not in _TRUST_HANDLERS:
        return None
    return str(clone_path)


TRUST_REQUIRED = "TRUST_REQUIRED"


def is_trust_required_failure(text) -> bool:
    """Detect a worker startup failure caused by workspace trust."""
    return bool(text) and TRUST_REQUIRED in str(text)


def merge_trust_failure_unhealthy(record, agent, status: str = TRUST_REQUIRED):
    """Pure merge: mark agent unhealthy so auto routing skips it next time."""
    record = record or {}
    unhealthy = dict(record.get("unhealthy_agents") or {})
    unhealthy[agent] = status
    healthy = [item for item in (record.get("healthy_agents") or []) if item != agent]
    return unhealthy, healthy


def ensure_controller_env_trust(project_root: Path | str | None = None, clone_root: Path | str | None = None) -> bool:
    """Pre-seed trust for project root, clone root, and common controller directories."""
    default_clone_root = Path(os.environ.get("HERDR_CLONES_DIR") or Path.home() / ".herdr-controller" / "clones")
    active_clone_root = Path(clone_root).expanduser().resolve() if clone_root else default_clone_root
    
    roots_to_trust = [active_clone_root]
    if project_root:
        roots_to_trust.append(Path(project_root).expanduser().resolve())

    all_ok = True
    for root in roots_to_trust:
        if not ensure_workspace_trust(root):
            all_ok = False
    return all_ok
