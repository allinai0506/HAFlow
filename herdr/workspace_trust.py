"""Standardized workspace trust configuration for CLI agents (Grok, Claude, etc.).

Pre-seeds permissions to avoid background modal/TTY authorization popups.
"""

import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path


def _exact_case_path(path: str) -> str:
    """Rebuild an absolute POSIX path with real directory-entry casing.

    On case-insensitive filesystems Path.resolve()/os.path.realpath never
    fixes casing, but agy matches trustedWorkspaces exactly. Existing
    segments are re-found via os.scandir entry names; segments that do not
    exist yet (planned clone leaves) are kept verbatim.
    """
    if not path or not path.startswith("/"):
        return path
    segments = path[1:].split("/")
    out = ""
    for i, seg in enumerate(segments):
        try:
            with os.scandir(out or "/") as it:
                entries = {e.name: e.name.lower() for e in it}
        except OSError:
            entries = {}
        if seg in entries:
            out = out + "/" + seg
            continue
        folded = "/" + seg
        match = next((n for n, low in entries.items() if low == folded[1:].lower()), None)
        if match is None:
            return out + "/" + "/".join(segments[i:])
        out = out + "/" + match
    return out


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


def ensure_agy_workspace_trust(repo_path: Path | str) -> bool:
    """Register the case-canonical workspace path in antigravity-cli settings.json.

    agy blocks every first start in a new directory with an interactive
    "Do you trust the contents of this project?" dialog (issue #255).
    trustedWorkspaces is matched path-and-case exactly, so entries are
    written through _exact_case_path; other keys and entries are preserved.
    """
    try:
        canonical = _exact_case_path(os.path.realpath(str(Path(repo_path).expanduser().absolute())))
        config = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        import fcntl
        # Different workflows launch concurrently and the controller only
        # serializes within one workflow: serialize the whole read-modify-
        # write across processes (PR #259 review P1).
        with open(config.parent / (config.name + ".lock"), "a+") as lock:
            deadline = time.monotonic() + _AGY_SETTINGS_LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ValueError("agy settings lock timeout; original file preserved")
                    time.sleep(0.05)
            try:
                return _agy_trust_write_locked(config, canonical)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except (OSError, ValueError, TypeError) as exc:
        print(f"[WORKSPACE TRUST WARN] agy trust failed for {repo_path}: {exc}", file=sys.stderr)
        return False


_AGY_SETTINGS_LOCK_TIMEOUT = 5.0


def _agy_trust_write_locked(config: Path, canonical: str) -> bool:
    try:
        raw = config.read_text(encoding="utf-8")
    except FileNotFoundError:
        data = {}
    else:
        # An unreadable or malformed existing file must fail closed: treating
        # it as an empty config and overwriting would destroy unrelated user
        # settings (model, permissions) despite the preservation contract
        # (PR #259 review P1).
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("agy settings root is not an object; original file preserved")
    trusted = data.get("trustedWorkspaces")
    if trusted is None:
        trusted = []
        data["trustedWorkspaces"] = trusted
    elif not isinstance(trusted, list):
        raise ValueError("agy trustedWorkspaces is not a list; original file preserved")
    if canonical in trusted:
        return True
    trusted.append(canonical)
    # replace() carries the tmp file's mode, so a tmp created under
    # umask 022 would widen the user's real 0600 settings.json to 0644 on
    # the first agy dispatch (issue #255 review B-1).
    try:
        mode = stat.S_IMODE(config.stat().st_mode)
    except FileNotFoundError:
        mode = 0o600
    fd, tmp_name = tempfile.mkstemp(dir=str(config.parent), prefix=".settings-", suffix=".tmp")
    try:
        payload = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        # os.write may accept fewer bytes than requested (e.g. ENOSPC after a
        # partial accept); a short write left unchecked would let os.replace
        # publish truncated JSON and corrupt the user's settings.
        written = 0
        while written < len(payload):
            written += os.write(fd, payload[written:])
        os.fchmod(fd, mode)
        os.replace(tmp_name, str(config))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    finally:
        os.close(fd)
    return True


_TRUST_HANDLERS = {
    "grok": ensure_grok_workspace_trust,
    "claude": ensure_claude_workspace_trust,
    "agy": ensure_agy_workspace_trust,
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
