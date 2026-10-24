"""Experience Hub probe lifecycle domain logic (issue #328).

Sections (one reason to change per section):

``selectors``
    Parse and validate the ``start`` / ``status`` / ``stop`` selectors.
``registry``
    Read/write the on-disk attribution registry
    (``<CONTROLLER_DIR>/run/experience-hub/<pid>.json``).
``probe``
    Bounded external probes (``lsof`` / ``ps`` / ``launchctl``) plus the
    pure classifiers for their exit-code/stderr/timeout contract.
``identity``
    Hub cmdline feature detection and the three-way AND used before a
    signal is ever sent.
``redact``
    Token redaction for every value that may reach stdout, stderr or a
    registry file.
``classify``
    The single status/stop state matrix and exit-code mapping.
``actions``
    The only signal side-effect surface: recheck, SIGTERM, bounded wait,
    endpoint reprobe.

This module deliberately imports neither the HTTP server module nor its
storage class.  ``status``/``stop`` must never create a database (R9);
keeping the import graph clean is the structural guarantee, not a
convention.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from herdr.state.constants import CONTROLLER_DIR

# ---------------------------------------------------------------------------
# Exit codes and vocabulary (frozen, see requirements §4b)
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_ABSENT = 3
EXIT_AMBIGUOUS = 4
EXIT_UNVERIFIABLE = 5
EXIT_LAUNCHD_MANAGED = 6
EXIT_TIMEOUT = 7

MANAGER_LAUNCHD = "launchd"
MANAGER_AD_HOC = "ad_hoc"
MANAGER_UNKNOWN = "unknown"

STATE_RUNNING = "running"
STATE_ABSENT = "absent"
STATE_FOREIGN = "foreign"
STATE_AMBIGUOUS = "ambiguous"
STATE_DEGRADED = "degraded"
STATE_STALE = "stale"
STATE_UNKNOWN = "unknown"
STATE_UNVERIFIABLE = "unverifiable"

ATTRIBUTION_REGISTRY = "registry"
ATTRIBUTION_PORT = "port"
ATTRIBUTION_NONE = "none"

ACTION_NONE = "none"
ACTION_CLEANUP = "cleanup"
ACTION_CLEANUP_REFUSE = "cleanup_refuse"
ACTION_SIGNAL = "signal"
ACTION_REFUSE = "refuse"

COMMAND_START = "start"
COMMAND_STATUS = "status"
COMMAND_STOP = "stop"

HUB_SCRIPT_BASENAME = "herdr-experience-hub"
HUB_SUBCOMMAND = "start"
LAUNCHD_LABEL = "com.user.herdr-experience-hub"

REGISTRY_SCHEMA_VERSION = 1
REGISTRY_DIR_NAME = "experience-hub"

PROBE_TIMEOUT_SECONDS = 2.0
STOP_TIMEOUT_SECONDS = 5.0

REDACTED = "<redacted>"
SENSITIVE_OPTIONS = ("--admin-token", "--token")

_WILDCARD_HOSTS = frozenset({"*", "0.0.0.0", "::", "[::]", ""})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"})
_REGISTRY_FIELDS = (
    "schema_version",
    "pid",
    "host",
    "port",
    "db_path",
    "started_at",
    "script",
    "manager",
)


class UsageError(ValueError):
    """A selector or argument combination is a usage error (exit 2)."""


# ---------------------------------------------------------------------------
# selectors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Selector:
    """Normalized selector for one lifecycle command."""

    command: str
    port: int | None = None
    db_path: Path | None = None
    host: str | None = None
    pid: int | None = None


def normalize_db_path(value: os.PathLike | str) -> Path:
    """Expand ``~`` and make the path absolute; never require it to exist."""
    return Path(value).expanduser().resolve()


def _validate_port(command: str, port: int | None) -> int | None:
    if port is None:
        return None
    if isinstance(port, bool) or not isinstance(port, int):
        raise UsageError("--port must be an integer")
    if command == COMMAND_START:
        if not 0 <= port <= 65535:
            raise UsageError("--port for start must be between 0 and 65535")
    elif not 1 <= port <= 65535:
        raise UsageError(f"--port for {command} must be between 1 and 65535 (0 is not a target)")
    return port


def parse_selector(
    *,
    command: str,
    port: int | None = None,
    db_path: os.PathLike | str | None = None,
    host: str | None = None,
    pid: int | None = None,
) -> Selector:
    """Validate one command's selector.

    ``start`` accepts port 0 (ephemeral) so that AC-14 can bind an actual
    port; ``status``/``stop`` reject 0 because no process can listen on it.
    ``--host``/``--pid`` only narrow a ``--port`` target (AC-21/N-4).
    """
    if command not in (COMMAND_START, COMMAND_STATUS, COMMAND_STOP):
        raise UsageError(f"unknown lifecycle command: {command}")

    port = _validate_port(command, port)

    normalized_db: Path | None = None
    if db_path is not None:
        raw = str(db_path).strip()
        if not raw:
            raise UsageError("--db-path must not be empty")
        normalized_db = normalize_db_path(raw)

    if host is not None:
        host = str(host).strip()
        if not host:
            raise UsageError("--host must not be empty")
        if command in (COMMAND_STATUS, COMMAND_STOP) and port is None:
            raise UsageError(f"--host can only be combined with --port for {command}")

    if pid is not None:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise UsageError("--pid must be a positive integer")
        if command != COMMAND_STOP:
            raise UsageError(f"--pid is not supported by {command}")
        if port is None:
            raise UsageError("--pid can only be combined with --port for stop")

    if command in (COMMAND_STATUS, COMMAND_STOP) and port is None and normalized_db is None:
        raise UsageError(f"{command} requires --port or --db-path")

    return Selector(
        command=command,
        port=port,
        db_path=normalized_db,
        host=host,
        pid=pid,
    )


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegistryEntry:
    """One parsed registry file.

    ``corrupt`` entries are never used for attribution or signal decisions;
    they are only reported (``registry_state=corrupt``) and may be deleted
    when the endpoint has no listener.
    """

    path: Path
    pid: int | None = None
    host: str = "unknown"
    port: int | None = None
    db_path: str | None = None
    started_at: float | None = None
    script: str | None = None
    manager: str = MANAGER_UNKNOWN
    schema_version: int = 0
    corrupt: bool = False

    def as_payload(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "pid": self.pid,
            "host": self.host,
            "port": self.port,
            "db_path": self.db_path,
            "started_at": self.started_at,
            "script": self.script,
            "manager": self.manager,
        }


def registry_root(root: os.PathLike | str | None = None) -> Path:
    """Registry root; explicit ``root`` wins so tests never touch ``$HOME``."""
    if root is not None:
        return Path(root)
    return Path(CONTROLLER_DIR) / "run" / REGISTRY_DIR_NAME


def registry_path(pid: int, root: os.PathLike | str | None = None) -> Path:
    return registry_root(root) / f"{int(pid)}.json"


def _best_effort_port(text: str) -> int | None:
    match = re.search(r'"port"\s*:\s*(\d+)', text)
    return int(match.group(1)) if match else None


def _parse_registry_file(path: Path) -> RegistryEntry:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return RegistryEntry(path=path, corrupt=True)
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return RegistryEntry(path=path, pid=_pid_from_name(path), port=_best_effort_port(raw), corrupt=True)
    if not isinstance(data, dict):
        return RegistryEntry(path=path, pid=_pid_from_name(path), port=_best_effort_port(raw), corrupt=True)

    pid = data.get("pid")
    port = data.get("port")
    host = data.get("host")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65535
        or not isinstance(host, str)
        or not host.strip()
    ):
        return RegistryEntry(
            path=path,
            pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else _pid_from_name(path),
            port=port if isinstance(port, int) and not isinstance(port, bool) else _best_effort_port(raw),
            corrupt=True,
        )
    if _pid_from_name(path) not in (None, pid):
        # The registry key is the pid (B19); a mismatched file name is not
        # trustworthy attribution.
        return RegistryEntry(path=path, pid=pid, port=port, host=host, corrupt=True)

    db_path = data.get("db_path")
    started_at = data.get("started_at")
    schema_version = data.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        schema_version = 0
    return RegistryEntry(
        path=path,
        pid=pid,
        host=str(host),
        port=int(port),
        db_path=str(db_path) if isinstance(db_path, str) else None,
        started_at=float(started_at) if isinstance(started_at, (int, float)) and not isinstance(started_at, bool) else None,
        script=data.get("script") if isinstance(data.get("script"), str) else None,
        manager=data.get("manager") if isinstance(data.get("manager"), str) else MANAGER_UNKNOWN,
        schema_version=schema_version,
    )


def _pid_from_name(path: Path) -> int | None:
    stem = path.stem
    return int(stem) if stem.isdigit() else None


def scan_registry(root: os.PathLike | str | None = None) -> tuple[RegistryEntry, ...]:
    """Read every ``*.json`` registry entry.

    A missing registry root is an empty registry, not an error, and reading
    never creates the directory (BD-3).
    """
    base = registry_root(root)
    if not base.exists() or not base.is_dir():
        return ()
    entries = [_parse_registry_file(path) for path in sorted(base.glob("*.json"))]
    return tuple(entries)


def _ensure_private_dir(path: Path) -> None:
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)


def write_registry(
    *,
    pid: int,
    host: str,
    port: int,
    db_path: os.PathLike | str,
    script: os.PathLike | str | None = None,
    manager: str = MANAGER_UNKNOWN,
    started_at: float | None = None,
    root: os.PathLike | str | None = None,
) -> Path:
    """Atomically write one registry entry (0600 file inside 0700 dirs)."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("registry pid must be a positive integer")
    if not 1 <= int(port) <= 65535:
        raise ValueError("registry port must be between 1 and 65535")

    base = registry_root(root)
    _ensure_private_dir(base.parent)
    _ensure_private_dir(base)

    payload = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "pid": int(pid),
        "host": str(host),
        "port": int(port),
        "db_path": str(normalize_db_path(db_path)),
        "started_at": float(started_at if started_at is not None else time.time()),
        "script": str(script) if script is not None else None,
        "manager": str(manager),
    }

    target = base / f"{int(pid)}.json"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{pid}.", suffix=".tmp", dir=base)
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
        os.chmod(target, 0o600)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return target


def remove_registry_entry(entry: RegistryEntry) -> bool:
    """Delete one entry, re-checking that it still describes the same instance."""
    try:
        current = _parse_registry_file(entry.path)
    except OSError:
        return False
    if not entry.corrupt:
        if current.corrupt:
            return False
        if (current.pid, current.port, current.db_path) != (entry.pid, entry.port, entry.db_path):
            return False
    try:
        entry.path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def paths_equal(left: os.PathLike | str | None, right: os.PathLike | str | None) -> bool:
    if left is None or right is None:
        return False
    try:
        return str(normalize_db_path(left)) == str(normalize_db_path(right))
    except (OSError, RuntimeError, ValueError):
        return False


@dataclass(frozen=True)
class RegistryResolution:
    """Outcome of turning a selector into a concrete port."""

    port: int | None
    entries: tuple[RegistryEntry, ...] = ()
    exit_code: int = EXIT_OK
    message: str = ""


def resolve_registry_port(
    selector: Selector,
    entries: Sequence[RegistryEntry],
) -> RegistryResolution:
    """Resolve ``--db-path`` (and its intersection with ``--port``).

    B23/Q10: zero db-path matches is ``ambiguous`` (exit 4), never
    ``absent``/0.  Two or more matches are ambiguous too; exactly one turns
    into port discovery.
    """
    valid = tuple(entry for entry in entries if not entry.corrupt)

    if selector.port is not None:
        matches = tuple(entry for entry in valid if entry.port == selector.port)
        if selector.db_path is not None:
            matches = tuple(entry for entry in matches if paths_equal(entry.db_path, selector.db_path))
            if not matches:
                return RegistryResolution(
                    port=None,
                    entries=(),
                    exit_code=EXIT_AMBIGUOUS,
                    message="no registry entry matches both --port and --db-path",
                )
        return RegistryResolution(port=selector.port, entries=matches)

    matches = tuple(entry for entry in valid if paths_equal(entry.db_path, selector.db_path))
    if not matches:
        return RegistryResolution(
            port=None,
            entries=(),
            exit_code=EXIT_AMBIGUOUS,
            message=f"no registry entry matches --db-path {selector.db_path}",
        )
    if len(matches) > 1:
        ports = ", ".join(str(entry.port) for entry in matches)
        return RegistryResolution(
            port=None,
            entries=matches,
            exit_code=EXIT_AMBIGUOUS,
            message=f"multiple registry entries match --db-path; narrow with --port: {ports}",
        )
    return RegistryResolution(port=matches[0].port, entries=matches)


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Listener:
    """One TCP LISTEN socket owner."""

    pid: int
    host: str
    port: int
    wildcard: bool = False
    is_hub: bool | None = None
    argv: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProcessInfo:
    """Result of one ``ps`` probe.

    ``status`` is ``alive`` / ``dead`` / ``unverifiable``.  ``unverifiable``
    means the probe itself failed (missing tool, timeout, unparsable output,
    permission denied) and must fail closed.
    """

    pid: int
    status: str
    ppid: int | None = None
    started_at: float | None = None
    argv: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class ProbeOutcome:
    """Three-way lsof classification (B26)."""

    kind: str  # data | empty | unverifiable
    listeners: tuple[Listener, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class ManagerProbe:
    manager: str
    target_pid: int | None = None
    detail: str = ""


def parse_listener_address(value: str) -> tuple[str, int, bool]:
    """Parse ``host:port`` / ``[v6]:port`` / ``*:port`` from lsof/ps."""
    text = (value or "").strip()
    if not text:
        raise ValueError("empty listener address")
    if text.startswith("["):
        end = text.rfind("]")
        if end <= 0:
            raise ValueError(f"malformed listener address: {value}")
        host = text[1:end]
        rest = text[end + 1:]
        if not rest.startswith(":"):
            raise ValueError(f"malformed listener address: {value}")
        port_text = rest[1:]
    else:
        host, sep, port_text = text.rpartition(":")
        if not sep:
            raise ValueError(f"malformed listener address: {value}")
    if not host:
        raise ValueError(f"malformed listener address: {value}")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError(f"listener port out of range: {value}")
    wildcard = host.lower() in _WILDCARD_HOSTS
    return host, port, wildcard


def _listeners_from_field_output(lines: Sequence[str]) -> tuple[Listener, ...]:
    records: list[Listener] = []
    current_pid: int | None = None
    current_name: str | None = None

    def flush() -> None:
        nonlocal current_pid, current_name
        if current_pid is not None and current_name:
            host, port, wildcard = parse_listener_address(current_name)
            records.append(Listener(pid=current_pid, host=host, port=port, wildcard=wildcard))
        current_pid = None
        current_name = None

    for line in lines:
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            flush()
            current_pid = int(value)
        elif tag == "n":
            current_name = value
    flush()
    return tuple(records)


def _listeners_from_table_output(lines: Sequence[str]) -> tuple[Listener, ...]:
    records: list[Listener] = []
    for line in lines:
        tokens = line.split()
        if not tokens or tokens[0] == "COMMAND":
            continue
        if "TCP" not in tokens:
            continue
        index = tokens.index("TCP")
        if index + 1 >= len(tokens) or len(tokens) < 2:
            continue
        host, port, wildcard = parse_listener_address(tokens[index + 1])
        records.append(Listener(pid=int(tokens[1]), host=host, port=port, wildcard=wildcard))
    return tuple(records)


def parse_lsof_output(stdout: str) -> tuple[Listener, ...]:
    """Parse both ``lsof -F pcn`` and the default column output."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise ValueError("empty lsof output")
    if any(line[0] in {"p", "n", "c", "f"} for line in lines):
        listeners = _listeners_from_field_output(lines)
    else:
        listeners = _listeners_from_table_output(lines)
    if not listeners:
        raise ValueError("unparsable lsof output")
    return listeners


def classify_probe_result(
    returncode: int,
    stdout: str,
    stderr: str = "",
    *,
    timed_out: bool = False,
    missing: bool = False,
) -> ProbeOutcome:
    """B26: rc==0 data; rc==1 + empty stderr = empty; anything else = unverifiable."""
    if missing:
        return ProbeOutcome(kind="unverifiable", detail="lsof is not available")
    if timed_out:
        return ProbeOutcome(kind="unverifiable", detail="lsof timed out")
    if returncode == 0:
        try:
            return ProbeOutcome(kind="data", listeners=parse_lsof_output(stdout))
        except (ValueError, TypeError) as exc:
            return ProbeOutcome(kind="unverifiable", detail=f"unparsable lsof output: {exc}")
    if returncode == 1 and not stderr.strip():
        return ProbeOutcome(kind="empty", detail="no listener")
    return ProbeOutcome(kind="unverifiable", detail=redact_text(stderr.strip() or f"lsof rc={returncode}"))


def run_lsof(port: int, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> ProbeOutcome:
    """Run the bounded lsof probe used by status/stop/start preflight."""
    command = ["lsof", "-nP", "-Fpcn", f"-iTCP:{int(port)}", "-sTCP:LISTEN"]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return ProbeOutcome(kind="unverifiable", detail="lsof is not available")
    except subprocess.TimeoutExpired:
        return ProbeOutcome(kind="unverifiable", detail="lsof timed out")
    except OSError as exc:
        return ProbeOutcome(kind="unverifiable", detail=redact_text(str(exc)))
    return classify_probe_result(completed.returncode, completed.stdout, completed.stderr)


_PS_LSTART_RE = re.compile(
    r"^\s*(?P<pid>\d+)\s+(?P<ppid>\d+)\s+"
    r"(?P<lstart>[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s+"
    r"(?P<command>.*)$"
)


def parse_ps_output(stdout: str, *, expected_pid: int | None = None) -> ProcessInfo:
    """Parse ``ps -ww -p <pid> -o pid=,ppid=,lstart=,command=``."""
    text = stdout.strip()
    if not text:
        raise ValueError("empty ps output")
    match = _PS_LSTART_RE.match(text.splitlines()[0])
    if not match:
        raise ValueError("unparsable ps output")
    pid = int(match.group("pid"))
    if expected_pid is not None and pid != int(expected_pid):
        raise ValueError("ps returned a different pid")
    try:
        # ``ps lstart`` is local time and carries no zone offset.
        started_at = datetime.strptime(  # noqa: DTZ007
            match.group("lstart"), "%a %b %d %H:%M:%S %Y"
        ).timestamp()
    except ValueError:
        started_at = None
    command = match.group("command").strip()
    try:
        argv = tuple(shlex.split(command)) if command else ()
    except ValueError:
        raise ValueError("unparsable ps command line")
    status = "dead" if command.startswith("<defunct>") or "<defunct>" in argv else "alive"
    return ProcessInfo(
        pid=pid,
        status=status,
        ppid=int(match.group("ppid")),
        started_at=started_at,
        argv=argv,
    )


def classify_ps_result(
    pid: int,
    returncode: int,
    stdout: str,
    stderr: str = "",
    *,
    timed_out: bool = False,
    missing: bool = False,
) -> ProcessInfo:
    if missing:
        return ProcessInfo(pid=pid, status="unverifiable", detail="ps is not available")
    if timed_out:
        return ProcessInfo(pid=pid, status="unverifiable", detail="ps timed out")
    if returncode == 1 and not stderr.strip():
        return ProcessInfo(pid=pid, status="dead")
    if returncode != 0:
        return ProcessInfo(pid=pid, status="unverifiable",
                           detail=redact_text(stderr.strip() or f"ps rc={returncode}"))
    try:
        return parse_ps_output(stdout, expected_pid=pid)
    except (ValueError, TypeError) as exc:
        return ProcessInfo(pid=pid, status="unverifiable", detail=f"unparsable ps output: {exc}")


def run_ps(pid: int, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> ProcessInfo:
    command = ["ps", "-ww", "-p", str(int(pid)), "-o", "pid=,ppid=,lstart=,command="]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return ProcessInfo(pid=pid, status="unverifiable", detail="ps is not available")
    except subprocess.TimeoutExpired:
        return ProcessInfo(pid=pid, status="unverifiable", detail="ps timed out")
    except OSError as exc:
        return ProcessInfo(pid=pid, status="unverifiable", detail=redact_text(str(exc)))
    return classify_ps_result(pid, completed.returncode, completed.stdout, completed.stderr)


_LAUNCHCTL_PID_RES = (
    re.compile(r'"PID"\s*=\s*(\d+)\s*;'),
    re.compile(r"^\s*pid\s*=\s*(\d+)\s*$", re.MULTILINE),
)


def parse_launchctl_pid(stdout: str) -> int | None:
    """Extract the PID from ``launchctl list <label>`` or ``launchctl print``."""
    for pattern in _LAUNCHCTL_PID_RES:
        match = pattern.search(stdout or "")
        if match:
            return int(match.group(1))
    return None


def classify_manager(
    stdout: str,
    *,
    returncode: int = 0,
    target_pid: int | None = None,
    timed_out: bool = False,
    missing: bool = False,
    stderr: str = "",
) -> ManagerProbe:
    """B20 three-way manager classification.

    ``launchd`` only when the label lookup succeeds and its PID equals the
    target pid.  A successful lookup with a different/absent PID is
    ``ad_hoc`` (issue orphans).  Any probe failure is ``unknown`` and must
    never lead to a signal.
    """
    if missing:
        return ManagerProbe(MANAGER_UNKNOWN, target_pid, "launchctl is not available")
    if timed_out:
        return ManagerProbe(MANAGER_UNKNOWN, target_pid, "launchctl timed out")
    if returncode != 0:
        return ManagerProbe(
            MANAGER_UNKNOWN,
            target_pid,
            redact_text(stderr.strip() or f"launchctl rc={returncode}"),
        )
    if not (stdout or "").strip():
        return ManagerProbe(MANAGER_UNKNOWN, target_pid, "empty launchctl output")
    pid = parse_launchctl_pid(stdout)
    if pid is None:
        return ManagerProbe(MANAGER_AD_HOC, target_pid, "label loaded but no PID")
    if target_pid is not None and pid == int(target_pid):
        return ManagerProbe(MANAGER_LAUNCHD, target_pid, f"launchctl reports pid {pid}")
    return ManagerProbe(MANAGER_AD_HOC, target_pid, f"launchctl reports a different pid {pid}")


def run_launchctl(
    target_pid: int | None,
    *,
    label: str = LAUNCHD_LABEL,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ManagerProbe:
    """``launchctl list <label>`` (P0-4: no ``gui/<uid>`` prefix for list)."""
    try:
        completed = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return ManagerProbe(MANAGER_UNKNOWN, target_pid, "launchctl is not available")
    except subprocess.TimeoutExpired:
        return ManagerProbe(MANAGER_UNKNOWN, target_pid, "launchctl timed out")
    except OSError as exc:
        return ManagerProbe(MANAGER_UNKNOWN, target_pid, redact_text(str(exc)))
    return classify_manager(
        completed.stdout,
        returncode=completed.returncode,
        target_pid=target_pid,
        stderr=completed.stderr,
    )


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------


def is_hub_argv(argv: Sequence[str]) -> bool:
    """BD-14/B25: some token is the hub script and the next token is ``start``.

    Accepts relative, absolute and release-snapshot paths because only the
    basename is compared; a basename without the adjacent ``start`` is not
    enough.
    """
    tokens = list(argv)
    for index, token in enumerate(tokens[:-1]):
        if os.path.basename(str(token).rstrip("/")) == HUB_SCRIPT_BASENAME and tokens[index + 1] == HUB_SUBCOMMAND:
            return True
    return False


def _host_kind(host: str) -> str:
    value = (host or "").strip().lower()
    if value in _WILDCARD_HOSTS:
        return "wildcard"
    if value in _LOOPBACK_HOSTS:
        return "loopback"
    return "other"


def host_matches(request_host: str | None, listener_host: str) -> bool:
    """BD-7/P2-1: a wildcard listener covers the requested family."""
    if request_host is None:
        return True
    request_kind = _host_kind(request_host)
    listener_kind = _host_kind(listener_host)
    if listener_kind == "wildcard":
        return True
    if request_kind == "wildcard":
        return listener_kind == "wildcard"
    if request_kind == "loopback":
        return listener_kind == "loopback"
    return (request_host or "").strip().lower() == (listener_host or "").strip().lower()


def is_wildcard_host(host: str | None) -> bool:
    return _host_kind(host or "") == "wildcard"


def select_listeners(
    listeners: Sequence[Listener],
    host: str | None,
) -> tuple[Listener, ...]:
    """Narrow listeners by ``--host``; exact bindings win over wildcards."""
    if host is None:
        return tuple(listeners)
    exact = tuple(
        listener for listener in listeners
        if not listener.wildcard and host_matches(host, listener.host)
    )
    if exact:
        return exact
    return tuple(
        listener for listener in listeners
        if listener.wildcard and host_matches(host, listener.host)
    )


@dataclass(frozen=True)
class IdentityCheck:
    """The three AND terms, each independently assertable (B6)."""

    holds_endpoint: bool
    holds_cmdline: bool
    holds_registry: bool
    reasons: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.holds_endpoint and self.holds_cmdline and self.holds_registry


def check_identity(
    *,
    target_pid: int,
    listeners: Sequence[Listener],
    process: ProcessInfo | None,
    registry: RegistryEntry | None,
    selector: Selector,
) -> IdentityCheck:
    """Evaluate the triple AND; any unverifiable term fails closed."""
    reasons: list[str] = []

    holds_endpoint = any(
        listener.pid == target_pid and (selector.port is None or listener.port == selector.port)
        for listener in listeners
    )
    if not holds_endpoint:
        reasons.append("pid does not hold the selected endpoint")

    holds_cmdline = bool(
        process is not None
        and process.status == "alive"
        and process.pid == target_pid
        and is_hub_argv(process.argv)
    )
    if not holds_cmdline:
        reasons.append("cmdline is not a hub start invocation")

    holds_registry = True
    if registry is not None and not registry.corrupt:
        if registry.pid != target_pid:
            # A stale entry must not vouch for a process that replaced it on
            # the same (host, port); ownership is verified against the pid we
            # are about to signal.
            holds_registry = False
            reasons.append("registry pid disagrees with the signaled process")
        if selector.port is not None and registry.port != selector.port:
            holds_registry = False
        if selector.db_path is not None and not paths_equal(registry.db_path, selector.db_path):
            holds_registry = False
        if not any(host_matches(registry.host, listener.host) for listener in listeners):
            holds_registry = False
        if not holds_registry:
            reasons.append("registry entry disagrees with the selector or listener")

    return IdentityCheck(
        holds_endpoint=holds_endpoint,
        holds_cmdline=holds_cmdline,
        holds_registry=holds_registry,
        reasons=tuple(reasons),
    )


# ---------------------------------------------------------------------------
# redact
# ---------------------------------------------------------------------------


def redact_text(text: str, *, secrets: Iterable[str] = ()) -> str:
    """Replace known secret values in a free-form string."""
    result = text or ""
    for secret in secrets:
        if secret:
            result = result.replace(str(secret), REDACTED)
    return result


def redact_argv(argv: Sequence[str], *, secrets: Iterable[str] = ()) -> tuple[str, ...]:
    """Mask sensitive options for display (B8).

    Covers ``--admin-token X``, ``--admin-token=X``, ``--token`` and any
    token value equal to an injected secret (``HERDR_HUB_ADMIN_TOKEN``).
    """
    secret_values = {str(secret) for secret in secrets if secret}
    result: list[str] = []
    redact_next = False
    for token in argv:
        text = str(token)
        if redact_next:
            result.append(REDACTED)
            redact_next = False
            continue
        if text in SENSITIVE_OPTIONS:
            result.append(text)
            redact_next = True
            continue
        matched_option = next(
            (option for option in SENSITIVE_OPTIONS if text.startswith(option + "=")),
            None,
        )
        if matched_option is not None:
            result.append(f"{matched_option}={REDACTED}")
            continue
        if text in secret_values:
            result.append(REDACTED)
            continue
        result.append(text)
    return tuple(result)


def extract_option(argv: Sequence[str], option: str) -> str | None:
    """Read ``--opt value`` or ``--opt=value`` from an argv snapshot."""
    tokens = list(argv)
    for index, token in enumerate(tokens):
        text = str(token)
        if text == option and index + 1 < len(tokens):
            return str(tokens[index + 1])
        if text.startswith(option + "="):
            return text[len(option) + 1:]
    return None


def extract_db_path(argv: Sequence[str]) -> str | None:
    """DB path from argv, without falling back to ``DEFAULT_HUB_DB_PATH`` (BD-10)."""
    value = extract_option(argv, "--db-path")
    if not value:
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        # The process cwd is not observable from ps; do not invent one.
        return None
    return str(candidate.resolve())


def admin_state_readonly(db_path: os.PathLike | str) -> str:
    """Read-only admin check: ``initialized`` / ``no_admin`` / ``missing`` / ``corrupt``.

    Never creates the directory, the database or a token (B4/P1-5).
    """
    path = Path(db_path).expanduser()
    if not path.exists():
        return "missing"
    try:
        uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            row = connection.execute(
                "SELECT count(*) FROM auth_tokens WHERE role = 'admin'"
            ).fetchone()
        finally:
            connection.close()
    except (sqlite3.Error, OSError, ValueError):
        return "corrupt"
    return "initialized" if row and row[0] else "no_admin"


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Classification:
    """One matrix row evaluated for exactly one operation."""

    state: str
    attribution: str
    manager: str
    action: str
    exit_code: int
    registry_state: str = "absent"
    detail: str = ""


def _running_exit(operation: str, manager: str) -> tuple[str, int]:
    if operation == COMMAND_STATUS:
        return ACTION_NONE, EXIT_OK
    if manager == MANAGER_LAUNCHD:
        return ACTION_REFUSE, EXIT_LAUNCHD_MANAGED
    if manager == MANAGER_UNKNOWN:
        return ACTION_REFUSE, EXIT_UNVERIFIABLE
    return ACTION_SIGNAL, EXIT_OK


def _running_classification(
    *,
    operation: str,
    attribution: str,
    manager: str,
    registry_state: str,
    detail: str,
) -> Classification:
    action, exit_code = _running_exit(operation, manager)
    return Classification(
        state=STATE_RUNNING,
        attribution=attribution,
        manager=manager,
        action=action,
        exit_code=exit_code,
        registry_state=registry_state,
        detail=detail,
    )


def classify(
    *,
    operation: str,
    selector: Selector,
    listeners: Sequence[Listener] = (),
    registry: RegistryEntry | None = None,
    registry_state: str = "absent",
    process: ProcessInfo | None = None,
    manager: str = MANAGER_UNKNOWN,
    probe_status: str = "data",
    registry_ambiguous: bool = False,
) -> Classification:
    """Evaluate the 13-row state matrix for ``status`` or ``stop``.

    The operation is an explicit input because the same state has different
    exit codes for the two commands (P0-1).
    """
    if operation not in (COMMAND_STATUS, COMMAND_STOP):
        raise ValueError(f"unknown operation: {operation}")

    listeners = tuple(listeners)

    if registry_ambiguous:
        return Classification(
            state=STATE_AMBIGUOUS,
            attribution=ATTRIBUTION_NONE,
            manager=manager,
            action=ACTION_REFUSE,
            exit_code=EXIT_AMBIGUOUS,
            registry_state=registry_state,
            detail="multiple registry entries match the selector",
        )

    if probe_status == "unverifiable":
        return Classification(
            state=STATE_UNVERIFIABLE,
            attribution=ATTRIBUTION_NONE,
            manager=MANAGER_UNKNOWN,
            action=ACTION_REFUSE,
            exit_code=EXIT_UNVERIFIABLE,
            registry_state=registry_state,
            detail="lsof/ps cannot be verified; refusing to guess",
        )

    if listeners:
        if any(listener.is_hub is None for listener in listeners):
            return Classification(
                state=STATE_UNVERIFIABLE,
                attribution=ATTRIBUTION_NONE,
                manager=MANAGER_UNKNOWN,
                action=ACTION_REFUSE,
                exit_code=EXIT_UNVERIFIABLE,
                registry_state=registry_state,
                detail="listener identity cannot be read",
            )
        if len(listeners) > 1:
            return Classification(
                state=STATE_AMBIGUOUS,
                attribution=ATTRIBUTION_NONE,
                manager=manager,
                action=ACTION_REFUSE,
                exit_code=EXIT_AMBIGUOUS,
                registry_state=registry_state,
                detail="multiple listeners on the endpoint; narrow with --host",
            )

        sole = listeners[0]
        if sole.is_hub:
            attribution = ATTRIBUTION_PORT
            if registry is not None and not registry.corrupt and registry.pid == sole.pid:
                attribution = ATTRIBUTION_REGISTRY
            return _running_classification(
                operation=operation,
                attribution=attribution,
                manager=manager,
                registry_state=registry_state,
                detail="hub listener present",
            )
        return Classification(
            state=STATE_FOREIGN,
            attribution=ATTRIBUTION_NONE,
            manager=manager,
            action=ACTION_REFUSE,
            exit_code=EXIT_AMBIGUOUS,
            registry_state=registry_state,
            detail="endpoint is held by a non-hub process",
        )

    valid_registry = registry is not None and not registry.corrupt
    if valid_registry:
        if process is None or process.status == "unverifiable":
            return Classification(
                state=STATE_UNVERIFIABLE,
                attribution=ATTRIBUTION_NONE,
                manager=MANAGER_UNKNOWN,
                action=ACTION_REFUSE,
                exit_code=EXIT_UNVERIFIABLE,
                registry_state=registry_state,
                detail="registry process cannot be verified",
            )
        if process.status == "dead":
            return Classification(
                state=STATE_ABSENT,
                attribution=ATTRIBUTION_NONE,
                manager=MANAGER_UNKNOWN,
                action=ACTION_CLEANUP,
                exit_code=EXIT_ABSENT if operation == COMMAND_STATUS else EXIT_OK,
                registry_state="stale",
                detail="registry points at a dead process and the endpoint is free",
            )
        if is_hub_argv(process.argv):
            return Classification(
                state=STATE_DEGRADED,
                attribution=ATTRIBUTION_REGISTRY,
                manager=manager,
                action=ACTION_REFUSE,
                exit_code=EXIT_AMBIGUOUS,
                registry_state=registry_state,
                detail="hub process is alive but does not hold the endpoint",
            )
        return Classification(
            state=STATE_STALE,
            attribution=ATTRIBUTION_NONE,
            manager=MANAGER_UNKNOWN,
            action=ACTION_CLEANUP,
            exit_code=EXIT_ABSENT if operation == COMMAND_STATUS else EXIT_OK,
            registry_state="stale",
            detail="registry points at a live non-hub process",
        )

    if registry_state == "corrupt":
        return Classification(
            state=STATE_UNKNOWN,
            attribution=ATTRIBUTION_NONE,
            manager=MANAGER_UNKNOWN,
            action=ACTION_CLEANUP_REFUSE,
            exit_code=EXIT_AMBIGUOUS,
            registry_state="corrupt",
            detail="corrupt registry entry; the endpoint is free",
        )

    return Classification(
        state=STATE_ABSENT,
        attribution=ATTRIBUTION_NONE,
        manager=MANAGER_UNKNOWN,
        action=ACTION_NONE,
        exit_code=EXIT_ABSENT if operation == COMMAND_STATUS else EXIT_OK,
        registry_state="absent",
        detail="no listener and no registry entry",
    )


def classify_reoccupied(*, manager: str, is_hub: bool | None = None) -> tuple[str, int]:
    """B22: a re-occupied endpoint is never a success.

    Confirmable launchd -> 6; otherwise 4 (known foreign or unverifiable
    ownership both fail closed).
    """
    if manager == MANAGER_LAUNCHD:
        return STATE_RUNNING, EXIT_LAUNCHD_MANAGED
    if is_hub is False:
        return STATE_FOREIGN, EXIT_AMBIGUOUS
    return STATE_UNKNOWN, EXIT_AMBIGUOUS


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StopOutcome:
    exit_code: int
    state: str
    message: str
    signaled: bool
    timed_out: bool = False
    reoccupied: bool = False
    reoccupied_pid: int | None = None


def stop_endpoint(
    *,
    target_pid: int,
    verify: Callable[[], IdentityCheck],
    probe_listeners: Callable[[], Sequence[Listener] | None],
    pid_alive: Callable[[int], bool | str],
    manager_for_pid: Callable[[int], str] | None = None,
    send_signal: Callable[[int, int], None] | None = None,
    timeout_seconds: float = STOP_TIMEOUT_SECONDS,
    poll_interval: float = 0.05,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> StopOutcome:
    """SIGTERM one verified hub pid, wait bounded, then reprobe the endpoint.

    Success means the selected endpoint has **no** listener at all (B22).
    A re-occupied endpoint after the target exits is classified as launchd
    (6) or foreign/unverifiable (4), never 0.  A timeout keeps the registry
    (the caller must not clean it) and returns 7 without escalating.
    """
    check = verify()
    if not getattr(check, "ok", False):
        reasons = ", ".join(getattr(check, "reasons", ()) or ())
        return StopOutcome(
            exit_code=EXIT_AMBIGUOUS,
            state=STATE_FOREIGN,
            message=f"identity recheck failed; no signal sent ({reasons or 'unknown'})",
            signaled=False,
        )

    send = send_signal or os.kill
    try:
        send(target_pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        return StopOutcome(
            exit_code=EXIT_UNVERIFIABLE,
            state=STATE_UNVERIFIABLE,
            message="permission denied while sending SIGTERM",
            signaled=False,
        )

    deadline = clock() + max(0.0, float(timeout_seconds))
    empty_streak = 0
    while True:
        raw_listeners = probe_listeners()
        if raw_listeners is None or getattr(raw_listeners, "kind", None) == "unverifiable":
            return StopOutcome(
                exit_code=EXIT_UNVERIFIABLE,
                state=STATE_UNVERIFIABLE,
                message="endpoint listener probe cannot be verified; registry preserved",
                signaled=True,
                reoccupied=False,
                timed_out=False,
            )

        alive_status = pid_alive(target_pid)
        if alive_status == "unverifiable":
            return StopOutcome(
                exit_code=EXIT_UNVERIFIABLE,
                state=STATE_UNVERIFIABLE,
                message="process alive status cannot be verified; registry preserved",
                signaled=True,
                reoccupied=False,
                timed_out=False,
            )

        listeners = tuple(raw_listeners)
        if any(listener.pid <= 0 for listener in listeners):
            return StopOutcome(
                exit_code=EXIT_UNVERIFIABLE,
                state=STATE_UNVERIFIABLE,
                message="endpoint listener probe cannot be verified; registry preserved",
                signaled=True,
                reoccupied=False,
                timed_out=False,
            )

        alive = bool(alive_status in (True, "alive"))
        if not listeners:
            # B22: success is defined on the endpoint.  Require it to stay
            # empty for two probes so an immediate re-occupier is observed;
            # a zombie still reported by ps counts as dead.
            empty_streak += 1
            if empty_streak >= 2 and not alive:
                return StopOutcome(
                    exit_code=EXIT_OK,
                    state=STATE_ABSENT,
                    message="target exited; endpoint has no listener",
                    signaled=True,
                )
        else:
            empty_streak = 0
            if not alive:
                new = listeners[0]
                new_manager = manager_for_pid(new.pid) if manager_for_pid is not None else MANAGER_UNKNOWN
                state, exit_code = classify_reoccupied(manager=new_manager, is_hub=new.is_hub)
                return StopOutcome(
                    exit_code=exit_code,
                    state=state,
                    message=(
                        f"target exited but endpoint was re-occupied by pid {new.pid} "
                        f"(manager={new_manager})"
                    ),
                    signaled=True,
                    reoccupied=True,
                    reoccupied_pid=new.pid,
                )
        if clock() >= deadline:
            return StopOutcome(
                exit_code=EXIT_TIMEOUT,
                state=STATE_RUNNING,
                message="timed out waiting for the target to exit; registry preserved, no SIGKILL",
                signaled=True,
                timed_out=True,
            )
        sleep(poll_interval)


# ---------------------------------------------------------------------------
# report helpers
# ---------------------------------------------------------------------------


def build_status_report(
    classification: Classification,
    *,
    selector: Selector,
    listeners: Sequence[Listener],
    registry: RegistryEntry | None = None,
    process: ProcessInfo | None = None,
) -> dict:
    """Build the fixed ``--json`` schema (Q6); unknown values stay null."""
    listeners = tuple(listeners)
    pid: int | None = None
    if classification.state == STATE_RUNNING:
        if classification.attribution == ATTRIBUTION_REGISTRY and registry is not None:
            pid = registry.pid
        elif len(listeners) == 1:
            pid = listeners[0].pid
    elif registry is not None and not registry.corrupt:
        pid = registry.pid
    elif len(listeners) == 1:
        pid = listeners[0].pid

    # P2 (PR #338 round 2): a stale registry must not lend its metadata to
    # the process actually being reported. Registry data is reported only
    # when the entry is owned by the reported pid (hub A's leftover entry,
    # hub B reusing the port); otherwise fall back to the live process's own
    # argv (or the single listener's argv) and leave unknown fields null.
    registry_owned = (
        registry is not None
        and not registry.corrupt
        and pid is not None
        and registry.pid == pid
    )

    db_path: str | None = None
    if registry_owned:
        db_path = registry.db_path
    if db_path is None:
        if process is not None and process.status == "alive" and (pid is None or process.pid == pid):
            db_path = extract_db_path(process.argv)
        elif len(listeners) == 1 and listeners[0].argv:
            db_path = extract_db_path(listeners[0].argv)

    started_at: float | None = None
    if registry_owned:
        started_at = registry.started_at
    elif process is not None and (pid is None or process.pid == pid):
        started_at = process.started_at

    host: str | None = selector.host
    if len(listeners) == 1:
        host = listeners[0].host
    elif host is None and registry_owned:
        host = registry.host

    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "state": classification.state,
        "manager": classification.manager,
        "attribution": classification.attribution,
        "pid": pid,
        "host": host,
        "port": selector.port,
        "db_path": db_path,
        "db_exists": Path(db_path).exists() if db_path else None,
        "started_at": started_at,
        "listeners": [
            {
                "pid": listener.pid,
                "host": listener.host,
                "port": listener.port,
                "wildcard": listener.wildcard,
            }
            for listener in sorted(listeners, key=lambda item: item.pid)
        ],
        "registry_state": classification.registry_state,
    }


def format_status_text(report: dict) -> str:
    """Human-readable rendering of the status report (no argv/token)."""
    lines = [
        f"state: {report.get('state')}",
        f"manager: {report.get('manager')}",
        f"attribution: {report.get('attribution')}",
        f"pid: {_display(report.get('pid'))}",
        f"host: {_display(report.get('host'))}",
        f"port: {_display(report.get('port'))}",
        f"db_path: {_display(report.get('db_path'))}",
        f"db_exists: {_display(report.get('db_exists'))}",
        f"started_at: {_display(report.get('started_at'))}",
        f"registry_state: {report.get('registry_state')}",
    ]
    listeners = report.get("listeners") or []
    if listeners:
        for listener in listeners:
            marker = " (wildcard)" if listener.get("wildcard") else ""
            lines.append(f"  listener: pid={listener['pid']} {listener['host']}:{listener['port']}{marker}")
    else:
        lines.append("  listener: none")
    return "\n".join(lines)


def _display(value: object) -> str:
    return "null" if value is None else str(value)


__all__ = [
    "ACTION_CLEANUP",
    "ACTION_CLEANUP_REFUSE",
    "ACTION_NONE",
    "ACTION_REFUSE",
    "ACTION_SIGNAL",
    "ATTRIBUTION_NONE",
    "ATTRIBUTION_PORT",
    "ATTRIBUTION_REGISTRY",
    "EXIT_ABSENT",
    "EXIT_AMBIGUOUS",
    "EXIT_ERROR",
    "EXIT_LAUNCHD_MANAGED",
    "EXIT_OK",
    "EXIT_TIMEOUT",
    "EXIT_UNVERIFIABLE",
    "EXIT_USAGE",
    "LAUNCHD_LABEL",
    "MANAGER_AD_HOC",
    "MANAGER_LAUNCHD",
    "MANAGER_UNKNOWN",
    "REDACTED",
    "REGISTRY_SCHEMA_VERSION",
    "STATE_ABSENT",
    "STATE_AMBIGUOUS",
    "STATE_DEGRADED",
    "STATE_FOREIGN",
    "STATE_RUNNING",
    "STATE_STALE",
    "STATE_UNKNOWN",
    "STATE_UNVERIFIABLE",
    "STOP_TIMEOUT_SECONDS",
    "Classification",
    "IdentityCheck",
    "Listener",
    "ManagerProbe",
    "ProbeOutcome",
    "ProcessInfo",
    "RegistryEntry",
    "RegistryResolution",
    "Selector",
    "StopOutcome",
    "UsageError",
    "admin_state_readonly",
    "build_status_report",
    "check_identity",
    "classify",
    "classify_manager",
    "classify_probe_result",
    "classify_ps_result",
    "classify_reoccupied",
    "extract_db_path",
    "extract_option",
    "format_status_text",
    "host_matches",
    "is_hub_argv",
    "is_wildcard_host",
    "normalize_db_path",
    "parse_launchctl_pid",
    "parse_listener_address",
    "parse_lsof_output",
    "parse_ps_output",
    "parse_selector",
    "paths_equal",
    "redact_argv",
    "redact_text",
    "registry_root",
    "remove_registry_entry",
    "resolve_registry_port",
    "run_launchctl",
    "run_lsof",
    "run_ps",
    "scan_registry",
    "select_listeners",
    "stop_endpoint",
    "write_registry",
]
