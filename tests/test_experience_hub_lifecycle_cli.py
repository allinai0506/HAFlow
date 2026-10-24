"""CLI integration tests for the Experience Hub lifecycle channel (T-B).

Every case runs with ``HOME=<tmp>`` and a temporary DB so the production
state root is never touched.  The external capabilities under test (real
socket bind, real ``lsof``/``ps``, real SIGTERM, real registry files) are
never replaced; only ``launchctl`` output is injected where a real launchd
service cannot be created in CI.

Case -> acceptance map::

    test_stop_absent_free_port                  AC-1
    test_status_json_after_start                AC-2
    test_status_without_registry_uses_argv_db_path  BD-10 / §3.2.3
    test_start_second_same_hostport             AC-3 / AC-9
    test_start_stop_roundtrip                   AC-4 / AC-8
    test_foreign_listener_fail_closed           AC-5
    test_registry_points_to_live_non_hub        AC-5
    test_compile_and_help                       AC-6
    test_real_chain_start_status_stop           AC-8
    test_class_bind_failure_zero_side_effect    AC-9 (class layer)
    test_security_failure_zero_side_effect      AC-9b
    test_check_init_only_readonly               AC-9c
    test_launchd_refuse_cli_fixture             AC-10
    test_manager_unknown_cli_fixture            AC-10
    test_token_never_echoed                     AC-11
    test_dual_listener_ambiguous_and_narrowing  AC-12
    test_degraded_process_alive_no_listen       AC-13
    test_start_port_zero                        AC-14
    test_status_stop_do_not_create_db           AC-16
    test_stop_by_db_path_after_db_deleted       AC-17
    test_registry_contract                      AC-18
    test_concurrent_start_same_port             AC-19
    test_db_path_zero_match_is_exit_4           AC-20 / B23
    test_pid_and_host_alone_usage_error         AC-21
    test_stop_reoccupied_real_socket_not_zero   AC-22
    test_start_probe_unavailable_still_binds    AC-23
    test_start_foreground_ctrlc_returns         AC-24
    test_storage_prestart_compat                AC-25
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from herdr.experience import hub_lifecycle as lifecycle

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "herdr-experience-hub"
ADMIN_TOKEN = "sentinel-" + uuid.uuid4().hex
REGISTRY_RELATIVE = Path(".herdr-controller") / "run" / "experience-hub"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def base_env(home: Path, db_path: Path, *, path: str | None = None) -> dict:
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["HERDR_HUB_DB_PATH"] = str(db_path)
    env.pop("HERDR_HUB_ADMIN_TOKEN", None)
    env.pop("HERDR_HUB_HOST", None)
    env.pop("HERDR_HUB_PORT", None)
    if path is not None:
        env["PATH"] = path
    return env


def run_cli(args, env, *, timeout=30):
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )


def wait_until(predicate, *, timeout=15.0, interval=0.05, message="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {message}")


class Hub:
    def __init__(self, proc, port, db_path, registry_path, output_reader):
        self.proc = proc
        self.port = port
        self.db_path = db_path
        self.registry_path = registry_path
        self._output_reader = output_reader

    @property
    def pid(self):
        return self.proc.pid

    def output(self):
        return self._output_reader(self.proc)


class Sandbox:
    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir(parents=True)
        self.registry_root = self.home / REGISTRY_RELATIVE
        self.db_path = tmp_path / "hub.db"
        self.env = base_env(self.home, self.db_path)
        self.processes = []
        self.hubs = []
        self.log_dir = tmp_path / "logs"
        self.log_dir.mkdir()
        self._logs = {}
        self._handles = []
        self._sequence = 0

    def popen(self, argv, *, env=None):
        self._sequence += 1
        out_path = self.log_dir / f"proc-{self._sequence}.out"
        err_path = self.log_dir / f"proc-{self._sequence}.err"
        out_handle = out_path.open("w", encoding="utf-8")
        err_handle = err_path.open("w", encoding="utf-8")
        self._handles.extend((out_handle, err_handle))
        proc = subprocess.Popen(
            argv,
            stdout=out_handle,
            stderr=err_handle,
            text=True,
            env=env or self.env,
        )
        self.processes.append(proc)
        self._logs[proc.pid] = (out_path, err_path)
        return proc

    def output(self, proc):
        out_path, err_path = self._logs[proc.pid]
        return out_path.read_text(encoding="utf-8"), err_path.read_text(encoding="utf-8")

    def spawn_hub(
        self,
        *,
        host="127.0.0.1",
        port=0,
        db_path=None,
        token=ADMIN_TOKEN,
        extra_env=None,
        extra_args=(),
    ) -> Hub:
        db_path = Path(db_path or self.db_path)
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        proc = self.popen(
            [
                sys.executable,
                str(CLI),
                "start",
                "--host",
                host,
                "--port",
                str(port),
                "--db-path",
                str(db_path),
                "--admin-token",
                token,
                *extra_args,
            ],
            env=env,
        )

        def registry_ready():
            if proc.poll() is not None:
                out, err = self.output(proc)
                raise AssertionError(
                    f"hub exited during startup rc={proc.returncode}\nstdout={out}\nstderr={err}"
                )
            path = self.registry_root / f"{proc.pid}.json"
            return path if path.exists() else None

        registry_path = wait_until(registry_ready, message="registry write")
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
        actual_port = int(payload["port"])
        wait_until(
            lambda: lifecycle.run_lsof(actual_port).kind == "data",
            message=f"listener on {actual_port}",
        )
        hub = Hub(proc, actual_port, db_path, registry_path, self.output)
        self.hubs.append(hub)
        return hub

    def stop_all(self):
        for hub in self.hubs:
            if hub.proc.poll() is None:
                run_cli(["stop", "--port", str(hub.port)], self.env, timeout=20)
        for proc in self.processes:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        for handle in self._handles:
            try:
                handle.close()
            except OSError:
                pass
        self.hubs.clear()


@pytest.fixture
def sandbox(tmp_path):
    box = Sandbox(tmp_path)
    try:
        yield box
    finally:
        box.stop_all()


def registry_files(box: Sandbox):
    if not box.registry_root.exists():
        return []
    return sorted(box.registry_root.glob("*.json"))


def write_fake_hub_script(directory: Path) -> Path:
    """A process whose argv looks like the hub CLI but never binds."""
    script = directory / "herdr-experience-hub"
    script.write_text(
        "#!/usr/bin/env python3\nimport time\nwhile True:\n    time.sleep(0.2)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def spawn_fake_hub(box: Sandbox, script: Path):
    proc = box.popen([sys.executable, str(script), "start", "--port", str(free_port())])
    wait_until(lambda: proc.poll() is None, message="fake hub start")
    return proc


# ---------------------------------------------------------------------------
# AC-1 / AC-2 / AC-3 / AC-4 / AC-8
# ---------------------------------------------------------------------------


def test_stop_absent_free_port(sandbox):
    port = free_port()
    result = run_cli(["stop", "--port", str(port)], sandbox.env)
    assert result.returncode == 0, result.stderr
    assert "absent" in result.stdout.lower()
    assert not (sandbox.registry_root).exists()


def test_status_json_after_start(sandbox):
    hub = sandbox.spawn_hub()
    result = run_cli(["status", "--port", str(hub.port), "--json"], sandbox.env)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert set(report) == {
        "schema_version", "state", "manager", "attribution", "pid", "host", "port",
        "db_path", "db_exists", "started_at", "listeners", "registry_state",
    }
    assert report["schema_version"] == 1
    assert report["state"] == "running"
    assert report["attribution"] == "registry"
    assert report["pid"] == hub.pid
    assert report["port"] == hub.port
    assert report["db_exists"] is True
    assert report["registry_state"] == "valid"
    assert report["listeners"] == [
        {"pid": hub.pid, "host": "127.0.0.1", "port": hub.port, "wildcard": False}
    ]


def test_status_without_registry_uses_argv_db_path(sandbox):
    """BD-10/§3.2.3: no registry -> db_path comes from argv, never a default."""
    hub = sandbox.spawn_hub()
    hub.registry_path.unlink()

    result = run_cli(["status", "--port", str(hub.port), "--json"], sandbox.env)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["state"] == "running"
    assert report["attribution"] == "port"
    assert report["registry_state"] == "absent"
    assert report["pid"] == hub.pid
    assert Path(report["db_path"]).resolve() == hub.db_path.resolve()
    assert report["db_exists"] is True
    assert report["started_at"] is not None
    assert report["listeners"] == [
        {"pid": hub.pid, "host": "127.0.0.1", "port": hub.port, "wildcard": False}
    ]


def test_start_second_same_hostport(sandbox):
    hub = sandbox.spawn_hub()
    second_db = sandbox.tmp_path / "second" / "hub.db"
    result = run_cli(
        [
            "start",
            "--host",
            "127.0.0.1",
            "--port",
            str(hub.port),
            "--db-path",
            str(second_db),
            "--admin-token",
            "second-token-" + uuid.uuid4().hex,
        ],
        sandbox.env,
    )
    assert result.returncode != 0
    assert str(hub.pid) in result.stderr
    assert str(hub.db_path) in result.stderr
    assert "orphan-hub-probe" in result.stderr
    assert "管理员凭据初始化完成" not in result.stdout
    assert not second_db.exists()
    assert not second_db.parent.exists()
    assert len(registry_files(sandbox)) == 1


def test_start_stop_roundtrip(sandbox):
    hub = sandbox.spawn_hub()
    result = run_cli(["stop", "--port", str(hub.port)], sandbox.env)
    assert result.returncode == 0, result.stderr
    hub.proc.wait(timeout=10)
    assert lifecycle.run_lsof(hub.port).kind in ("empty", "unverifiable")
    wait_until(lambda: lifecycle.run_lsof(hub.port).kind == "empty", message="port release")
    assert not hub.registry_path.exists()
    assert hub.db_path.exists()


def test_real_chain_start_status_stop(sandbox):
    hub = sandbox.spawn_hub()
    status = run_cli(["status", "--port", str(hub.port), "--json"], sandbox.env)
    assert status.returncode == 0
    assert json.loads(status.stdout)["state"] == "running"

    stop = run_cli(["stop", "--port", str(hub.port)], sandbox.env)
    assert stop.returncode == 0
    hub.proc.wait(timeout=10)
    wait_until(lambda: lifecycle.run_lsof(hub.port).kind == "empty", message="port release")
    assert not hub.registry_path.exists()


# ---------------------------------------------------------------------------
# AC-5 identity fail-closed
# ---------------------------------------------------------------------------


def test_foreign_listener_fail_closed(sandbox):
    port = free_port()
    holder = sandbox.popen(
        [
            sys.executable,
            "-c",
            (
                "import socket,time,sys\n"
                "s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
                f"s.bind(('127.0.0.1', {port})); s.listen(5)\n"
                "print('ready', flush=True)\n"
                "time.sleep(120)\n"
            ),
        ]
    )
    wait_until(lambda: lifecycle.run_lsof(port).kind == "data", message="foreign listener")

    status = run_cli(["status", "--port", str(port), "--json"], sandbox.env)
    assert status.returncode == 4, status.stderr
    report = json.loads(status.stdout)
    assert report["state"] == "foreign"
    assert report["attribution"] == "none"
    assert report["listeners"][0]["pid"] == holder.pid

    stop = run_cli(["stop", "--port", str(port)], sandbox.env)
    assert stop.returncode == 4
    assert holder.poll() is None


def test_registry_points_to_live_non_hub(sandbox):
    sleeper = sandbox.popen([sys.executable, "-c", "import time; time.sleep(120)"])
    port = free_port()
    lifecycle.write_registry(
        pid=sleeper.pid,
        host="127.0.0.1",
        port=port,
        db_path=sandbox.tmp_path / "stale.db",
        root=sandbox.registry_root,
    )
    result = run_cli(["stop", "--port", str(port)], sandbox.env)
    assert result.returncode == 0, result.stderr
    assert sleeper.poll() is None
    assert not (sandbox.registry_root / f"{sleeper.pid}.json").exists()


# ---------------------------------------------------------------------------
# AC-6 / AC-9 / AC-9b / AC-9c
# ---------------------------------------------------------------------------


def test_compile_and_help():
    compile_result = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", "herdr", "services", "bin", "tests"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert compile_result.returncode == 0, compile_result.stderr
    help_result = subprocess.run(
        [sys.executable, str(CLI), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert help_result.returncode == 0
    assert "status" in help_result.stdout
    assert "stop" in help_result.stdout


def test_class_bind_failure_zero_side_effect(tmp_path):
    from herdr.experience.hub_server import ExperienceHubServer

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupier:
        occupier.bind(("127.0.0.1", 0))
        occupier.listen(1)
        port = occupier.getsockname()[1]
        db_path = tmp_path / "nested" / "hub.db"
        server = ExperienceHubServer(host="127.0.0.1", port=port, db_path=db_path)
        with pytest.raises(OSError):
            server.bind()
        assert not db_path.exists()
        assert not db_path.parent.exists()
        assert server._storage is None


def test_security_failure_zero_side_effect(sandbox):
    port = free_port()
    db_path = sandbox.tmp_path / "insecure" / "hub.db"
    result = run_cli(
        [
            "start",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--db-path",
            str(db_path),
            "--admin-token",
            ADMIN_TOKEN,
        ],
        sandbox.env,
    )
    assert result.returncode != 0
    assert not db_path.exists()
    assert not db_path.parent.exists()
    assert not sandbox.registry_root.exists()


def test_check_init_only_readonly(sandbox):
    db_path = sandbox.tmp_path / "check" / "hub.db"
    missing = run_cli(
        ["start", "--check-init-only", "--db-path", str(db_path)],
        sandbox.env,
    )
    assert missing.returncode != 0
    assert not db_path.exists()
    assert not db_path.parent.exists()
    assert not sandbox.registry_root.exists()

    created = run_cli(
        [
            "token",
            "create",
            "--token",
            "check-token-" + uuid.uuid4().hex,
            "--user",
            "admin",
            "--role",
            "admin",
            "--db-path",
            str(db_path),
        ],
        sandbox.env,
    )
    assert created.returncode == 0, created.stderr
    okay = run_cli(["start", "--check-init-only", "--db-path", str(db_path)], sandbox.env)
    assert okay.returncode == 0, okay.stderr
    assert not sandbox.registry_root.exists()


# ---------------------------------------------------------------------------
# AC-10 launchd refusal (fixture injection, no real launchd)
# ---------------------------------------------------------------------------


def _load_cli_module():
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("herdr_experience_hub_cli_under_test", str(CLI))
    spec = importlib.util.spec_from_loader("herdr_experience_hub_cli_under_test", loader)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    loader.exec_module(module)
    return module


def test_launchd_refuse_cli_fixture(sandbox, monkeypatch):
    hub = sandbox.spawn_hub()
    module = _load_cli_module()
    monkeypatch.setattr(module.lifecycle, "CONTROLLER_DIR", sandbox.home / ".herdr-controller")
    monkeypatch.setattr(
        module.lifecycle,
        "run_launchctl",
        lambda pid, **kwargs: lifecycle.ManagerProbe(lifecycle.MANAGER_LAUNCHD, pid, "fixture"),
    )
    exit_code = module.main(["stop", "--port", str(hub.port)])
    assert exit_code == lifecycle.EXIT_LAUNCHD_MANAGED
    assert hub.proc.poll() is None
    assert lifecycle.run_lsof(hub.port).kind == "data"

    cleanup = run_cli(["stop", "--port", str(hub.port)], sandbox.env)
    assert cleanup.returncode == 0, cleanup.stderr


def test_manager_unknown_cli_fixture(sandbox, monkeypatch):
    hub = sandbox.spawn_hub()
    module = _load_cli_module()
    monkeypatch.setattr(module.lifecycle, "CONTROLLER_DIR", sandbox.home / ".herdr-controller")
    monkeypatch.setattr(
        module.lifecycle,
        "run_launchctl",
        lambda pid, **kwargs: lifecycle.ManagerProbe(lifecycle.MANAGER_UNKNOWN, pid, "fixture"),
    )
    exit_code = module.main(["stop", "--port", str(hub.port)])
    assert exit_code == lifecycle.EXIT_UNVERIFIABLE
    assert hub.proc.poll() is None

    cleanup = run_cli(["stop", "--port", str(hub.port)], sandbox.env)
    assert cleanup.returncode == 0, cleanup.stderr


def test_stop_cli_unverifiable_lsof_reprobe(sandbox, monkeypatch, capsys):
    hub = sandbox.spawn_hub()
    module = _load_cli_module()
    monkeypatch.setattr(module.lifecycle, "CONTROLLER_DIR", sandbox.home / ".herdr-controller")

    real_run_lsof = module.lifecycle.run_lsof
    lsof_calls = 0

    def mock_run_lsof(port, **kwargs):
        nonlocal lsof_calls
        lsof_calls += 1
        # First 2 calls: collection and verify target
        if lsof_calls <= 2:
            return real_run_lsof(port, **kwargs)
        # Reprobe during stop loop is unverifiable
        return lifecycle.ProbeOutcome(kind="unverifiable", detail="simulated lsof failure")

    monkeypatch.setattr(module.lifecycle, "run_lsof", mock_run_lsof)

    exit_code = module.main(["stop", "--port", str(hub.port), "--json"])
    assert exit_code == lifecycle.EXIT_UNVERIFIABLE
    assert hub.registry_path.exists()

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["exit_code"] == lifecycle.EXIT_UNVERIFIABLE
    assert payload["state"] == lifecycle.STATE_UNVERIFIABLE
    assert payload["reoccupied"] is False
    assert payload["pid"] == hub.proc.pid
    assert payload["pid"] != -1
    assert payload.get("reoccupied_pid") is None
    assert "-1" not in payload["message"]
    assert "pid -1" not in payload["message"]
    assert "pid -1" not in captured.err
    assert "pid=-1" not in captured.err

    hub.proc.terminate()
    hub.proc.wait(timeout=5)


def test_stop_cli_unverifiable_ps_reprobe(sandbox, monkeypatch, capsys):
    hub = sandbox.spawn_hub()
    module = _load_cli_module()
    monkeypatch.setattr(module.lifecycle, "CONTROLLER_DIR", sandbox.home / ".herdr-controller")

    real_run_ps = module.lifecycle.run_ps
    ps_calls = 0

    def mock_run_ps(pid, **kwargs):
        nonlocal ps_calls
        ps_calls += 1
        # Initial collection and verify calls
        if ps_calls <= 2:
            return real_run_ps(pid, **kwargs)
        # Polling pid_alive probe is unverifiable
        return lifecycle.ProcessInfo(pid=pid, status="unverifiable", detail="simulated ps failure")

    monkeypatch.setattr(module.lifecycle, "run_ps", mock_run_ps)

    exit_code = module.main(["stop", "--port", str(hub.port), "--json"])
    assert exit_code == lifecycle.EXIT_UNVERIFIABLE
    assert hub.registry_path.exists()

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["exit_code"] == lifecycle.EXIT_UNVERIFIABLE
    assert payload["state"] == lifecycle.STATE_UNVERIFIABLE
    assert payload["reoccupied"] is False
    assert payload["pid"] == hub.proc.pid
    assert payload["pid"] != -1
    assert payload.get("reoccupied_pid") is None
    assert "-1" not in payload["message"]
    assert "pid -1" not in payload["message"]
    assert "pid -1" not in captured.err
    assert "pid=-1" not in captured.err

    hub.proc.terminate()
    hub.proc.wait(timeout=5)


def test_stop_cli_unverifiable_launchctl_preserves_registry(sandbox, monkeypatch, capsys):
    hub = sandbox.spawn_hub()
    module = _load_cli_module()
    monkeypatch.setattr(module.lifecycle, "CONTROLLER_DIR", sandbox.home / ".herdr-controller")
    monkeypatch.setattr(
        module.lifecycle,
        "run_launchctl",
        lambda pid, **kwargs: lifecycle.ManagerProbe(lifecycle.MANAGER_UNKNOWN, pid, "simulated launchctl failure"),
    )

    exit_code = module.main(["stop", "--port", str(hub.port), "--json"])
    assert exit_code == lifecycle.EXIT_UNVERIFIABLE
    assert hub.registry_path.exists()
    assert hub.proc.poll() is None

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["exit_code"] == lifecycle.EXIT_UNVERIFIABLE
    assert payload["state"] == lifecycle.STATE_RUNNING
    assert payload["reoccupied"] is False
    assert payload["pid"] == hub.proc.pid
    assert payload["pid"] != -1
    assert payload.get("reoccupied_pid") is None
    assert "-1" not in payload["message"]
    assert "pid -1" not in payload["message"]
    assert "pid -1" not in captured.err
    assert "pid=-1" not in captured.err

    hub.proc.terminate()
    hub.proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# AC-11 token redaction
# ---------------------------------------------------------------------------


def test_token_never_echoed(sandbox):
    hub = sandbox.spawn_hub(token=ADMIN_TOKEN)
    registry_text = hub.registry_path.read_text(encoding="utf-8")
    status = run_cli(["status", "--port", str(hub.port), "--json"], sandbox.env)
    stop = run_cli(["stop", "--port", str(hub.port), "--json"], sandbox.env)
    hub.proc.wait(timeout=10)

    combined = status.stdout + status.stderr + stop.stdout + stop.stderr
    assert ADMIN_TOKEN not in combined
    assert ADMIN_TOKEN not in registry_text
    assert "argv" not in registry_text


# ---------------------------------------------------------------------------
# AC-12 dual listener ambiguity
# ---------------------------------------------------------------------------


def test_dual_listener_ambiguous_and_narrowing(sandbox):
    port = free_port()
    loopback = sandbox.spawn_hub(host="127.0.0.1", port=port,
                                 db_path=sandbox.tmp_path / "loopback.db")
    wildcard = sandbox.spawn_hub(host="0.0.0.0", port=port,
                                 db_path=sandbox.tmp_path / "wildcard.db",
                                 extra_args=("--allow-insecure-http",))

    status = run_cli(["status", "--port", str(port), "--json"], sandbox.env)
    assert status.returncode == 4, status.stderr
    report = json.loads(status.stdout)
    assert report["state"] == "ambiguous"
    assert {item["host"] for item in report["listeners"]} == {"127.0.0.1", "*"}

    refused = run_cli(["stop", "--port", str(port)], sandbox.env)
    assert refused.returncode == 4
    assert loopback.proc.poll() is None
    assert wildcard.proc.poll() is None

    narrowed = run_cli(["stop", "--port", str(port), "--host", "127.0.0.1"], sandbox.env)
    assert narrowed.returncode == 0, narrowed.stderr
    loopback.proc.wait(timeout=10)
    assert wildcard.proc.poll() is None
    assert lifecycle.run_lsof(port).kind == "data"


# ---------------------------------------------------------------------------
# AC-13 degraded
# ---------------------------------------------------------------------------


def test_degraded_process_alive_no_listen(sandbox):
    script = write_fake_hub_script(sandbox.tmp_path)
    fake = spawn_fake_hub(sandbox, script)
    port = free_port()
    lifecycle.write_registry(
        pid=fake.pid,
        host="127.0.0.1",
        port=port,
        db_path=sandbox.tmp_path / "degraded.db",
        root=sandbox.registry_root,
    )
    status = run_cli(["status", "--port", str(port), "--json"], sandbox.env)
    assert status.returncode == 4, status.stderr
    report = json.loads(status.stdout)
    assert report["state"] == "degraded"
    assert report["pid"] == fake.pid

    stop = run_cli(["stop", "--port", str(port)], sandbox.env)
    assert stop.returncode == 4
    assert fake.poll() is None


# ---------------------------------------------------------------------------
# AC-14 port 0
# ---------------------------------------------------------------------------


def test_start_port_zero(sandbox):
    hub = sandbox.spawn_hub(port=0)
    payload = json.loads(hub.registry_path.read_text(encoding="utf-8"))
    assert payload["port"] == hub.port

    out, _err = hub.output()
    assert f"127.0.0.1:{hub.port}" in out

    status = run_cli(["status", "--port", str(hub.port), "--json"], sandbox.env)
    assert status.returncode == 0
    assert json.loads(status.stdout)["port"] == hub.port

    for command in ("status", "stop"):
        usage = run_cli([command, "--port", "0"], sandbox.env)
        assert usage.returncode == 2, (command, usage.stderr)


# ---------------------------------------------------------------------------
# AC-16 / AC-17 db-path
# ---------------------------------------------------------------------------


def test_status_stop_do_not_create_db(sandbox):
    dead = sandbox.popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=5)
    port = free_port()
    db_path = sandbox.tmp_path / "never-created" / "hub.db"
    lifecycle.write_registry(
        pid=dead.pid,
        host="127.0.0.1",
        port=port,
        db_path=db_path,
        root=sandbox.registry_root,
    )
    status = run_cli(["status", "--port", str(port), "--json"], sandbox.env)
    assert status.returncode == 3, status.stderr
    assert not db_path.exists()
    assert not db_path.parent.exists()

    stop = run_cli(["stop", "--port", str(port)], sandbox.env)
    assert stop.returncode == 0, stop.stderr
    assert not db_path.exists()
    assert not db_path.parent.exists()
    assert not (sandbox.registry_root / f"{dead.pid}.json").exists()


def test_stop_by_db_path_after_db_deleted(sandbox):
    hub = sandbox.spawn_hub()
    hub.db_path.unlink()
    assert not hub.db_path.exists()

    result = run_cli(["stop", "--db-path", str(hub.db_path)], sandbox.env)
    assert result.returncode == 0, result.stderr
    hub.proc.wait(timeout=10)
    wait_until(lambda: lifecycle.run_lsof(hub.port).kind == "empty", message="port release")


def test_db_path_zero_match_is_exit_4(sandbox):
    result = run_cli(["stop", "--db-path", str(sandbox.tmp_path / "unknown.db")], sandbox.env)
    assert result.returncode == 4, result.stderr
    assert not sandbox.registry_root.exists()

    # Two entries for the same DB path (different ports) are ambiguous.
    for index, port in enumerate((free_port(), free_port()), start=1):
        lifecycle.write_registry(
            pid=900000 + index,
            host="127.0.0.1",
            port=port,
            db_path=sandbox.tmp_path / "shared.db",
            root=sandbox.registry_root,
        )
    ambiguous = run_cli(["stop", "--db-path", str(sandbox.tmp_path / "shared.db")], sandbox.env)
    assert ambiguous.returncode == 4, ambiguous.stderr
    assert "narrow" in ambiguous.stderr.lower() or "multiple" in ambiguous.stderr.lower()


# ---------------------------------------------------------------------------
# AC-18 registry contract
# ---------------------------------------------------------------------------


def test_registry_contract(sandbox):
    hub = sandbox.spawn_hub()
    payload = json.loads(hub.registry_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert set(payload) == {
        "schema_version", "pid", "host", "port", "db_path", "started_at", "script", "manager",
    }
    assert payload["pid"] == hub.pid
    assert payload["db_path"] == str(hub.db_path)
    assert (hub.registry_path.stat().st_mode & 0o777) == 0o600
    assert (hub.registry_path.parent.stat().st_mode & 0o777) == 0o700
    assert "argv" not in json.dumps(payload)
    assert "token" not in hub.registry_path.read_text(encoding="utf-8").lower()

    # token create and status must not write a registry entry.
    other_db = sandbox.tmp_path / "token.db"
    token_create = run_cli(
        ["token", "create", "--token", "tok-" + uuid.uuid4().hex,
         "--user", "u", "--role", "admin", "--db-path", str(other_db)],
        sandbox.env,
    )
    assert token_create.returncode == 0, token_create.stderr
    assert len(registry_files(sandbox)) == 1
    run_cli(["status", "--port", str(free_port())], sandbox.env)
    assert len(registry_files(sandbox)) == 1


# ---------------------------------------------------------------------------
# AC-19 concurrent start
# ---------------------------------------------------------------------------


def test_concurrent_start_same_port(sandbox):
    port = free_port()
    db_a = sandbox.tmp_path / "a.db"
    db_b = sandbox.tmp_path / "b.db"
    first = sandbox.popen(
        [sys.executable, str(CLI), "start", "--host", "127.0.0.1", "--port", str(port),
         "--db-path", str(db_a), "--admin-token", "token-" + uuid.uuid4().hex]
    )
    second = sandbox.popen(
        [sys.executable, str(CLI), "start", "--host", "127.0.0.1", "--port", str(port),
         "--db-path", str(db_b), "--admin-token", "token-" + uuid.uuid4().hex]
    )

    wait_until(lambda: len(registry_files(sandbox)) == 1, message="single registry entry")
    registry_path = registry_files(sandbox)[0]
    winner_pid = int(json.loads(registry_path.read_text(encoding="utf-8"))["pid"])
    winner = first if first.pid == winner_pid else second
    loser = second if winner is first else first

    loser.wait(timeout=20)
    assert loser.returncode != 0
    assert not (db_a if loser is first else db_b).exists()
    assert len(registry_files(sandbox)) == 1

    result = run_cli(["stop", "--port", str(port)], sandbox.env)
    assert result.returncode == 0, result.stderr
    winner.wait(timeout=10)


# ---------------------------------------------------------------------------
# AC-21 usage errors
# ---------------------------------------------------------------------------


def test_pid_and_host_alone_usage_error(sandbox):
    sleeper = sandbox.popen([sys.executable, "-c", "import time; time.sleep(60)"])
    pid_only = run_cli(["stop", "--pid", str(sleeper.pid)], sandbox.env)
    assert pid_only.returncode == 2
    host_only = run_cli(["status", "--host", "127.0.0.1"], sandbox.env)
    assert host_only.returncode == 2
    assert sleeper.poll() is None


# ---------------------------------------------------------------------------
# AC-22 re-occupation with real sockets
# ---------------------------------------------------------------------------


FAKE_HANDOFF_HUB = """#!/usr/bin/env python3
import os
import signal
import socket
import sys
import time

port = int(sys.argv[sys.argv.index("--port") + 1])
handoff = sys.argv[sys.argv.index("--handoff") + 1]
server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", port))
server.listen(5)
with open(handoff + ".ready", "w") as handle:
    handle.write(str(os.getpid()))


def on_term(signum, frame):
    try:
        server.close()
    except OSError:
        pass
    with open(handoff, "w") as handle:
        handle.write("go")
    deadline = time.time() + 5
    while time.time() < deadline and not os.path.exists(handoff + ".bound"):
        time.sleep(0.01)
    sys.exit(0)


signal.signal(signal.SIGTERM, on_term)
while True:
    time.sleep(0.05)
"""

BINDER = """import os
import socket
import sys
import time

port = int(sys.argv[1])
handoff = sys.argv[2]
while not os.path.exists(handoff):
    time.sleep(0.01)
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
deadline = time.time() + 5
while time.time() < deadline:
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(5)
        break
    except OSError:
        time.sleep(0.01)
with open(handoff + ".bound", "w") as handle:
    handle.write("bound")
time.sleep(60)
"""


def _current_listeners(port):
    outcome = lifecycle.run_lsof(port)
    return outcome.listeners if outcome.kind == "data" else ()


def test_stop_reoccupied_real_socket_not_zero(sandbox):
    port = free_port()
    handoff = sandbox.tmp_path / "handoff"
    script = sandbox.tmp_path / "herdr-experience-hub"
    script.write_text(FAKE_HANDOFF_HUB, encoding="utf-8")
    script.chmod(0o755)

    target = sandbox.popen(
        [sys.executable, str(script), "start", "--port", str(port), "--handoff", str(handoff)]
    )
    wait_until(lambda: (handoff.parent / "handoff.ready").exists(), message="target ready")
    binder = sandbox.popen([sys.executable, "-c", BINDER, str(port), str(handoff)])

    outcome = lifecycle.stop_endpoint(
        target_pid=target.pid,
        verify=lambda: lifecycle.IdentityCheck(True, True, True),
        probe_listeners=lambda: _current_listeners(port),
        pid_alive=lambda pid: target.poll() is None,
        manager_for_pid=lambda pid: lifecycle.MANAGER_AD_HOC,
        timeout_seconds=10.0,
        poll_interval=0.02,
    )
    assert outcome.exit_code == lifecycle.EXIT_AMBIGUOUS
    assert outcome.exit_code != lifecycle.EXIT_OK
    assert outcome.reoccupied is True
    assert binder.poll() is None


def test_stop_db_path_stale_registry_does_not_signal_port_reuser(sandbox):
    # PR #338 review P1: hub A exits leaving its registry entry; hub B with a
    # different DB takes over the same (host, port).  ``stop --db-path A.db``
    # must never signal B — the stale entry cannot vouch for another process.
    hub_a = sandbox.spawn_hub(db_path=sandbox.tmp_path / "a.db")
    port = hub_a.port
    hub_a.proc.kill()
    hub_a.proc.wait(timeout=5)
    hub_b = sandbox.spawn_hub(port=port, db_path=sandbox.tmp_path / "b.db")

    result = run_cli(
        ["stop", "--port", str(port), "--db-path", str(hub_a.db_path), "--json"],
        sandbox.env,
        timeout=20,
    )
    payload = json.loads(result.stdout)
    assert payload["pid"] == hub_b.pid
    assert payload["signaled"] is False
    assert payload["exit_code"] != lifecycle.EXIT_OK
    assert hub_b.proc.poll() is None
    assert (sandbox.registry_root / f"{hub_b.pid}.json").exists()
    assert (sandbox.registry_root / f"{hub_a.pid}.json").exists()


# ---------------------------------------------------------------------------
# AC-23 / AC-24 / AC-25
# ---------------------------------------------------------------------------


def test_start_probe_unavailable_still_binds(sandbox):
    # With lsof/ps absent the suggestive preflight cannot run; bind is
    # authoritative and the hub must still start (P0-6/AC-23).
    hub = sandbox.spawn_hub(
        port=0,
        db_path=sandbox.tmp_path / "no-lsof.db",
        extra_env={"PATH": "/nonexistent"},
    )
    assert hub.port > 0
    assert hub.proc.poll() is None
    assert hub.registry_path.exists()


def test_start_foreground_ctrlc_returns(sandbox):
    hub = sandbox.spawn_hub()
    hub.proc.send_signal(2)  # SIGINT
    hub.proc.wait(timeout=5)
    wait_until(lambda: lifecycle.run_lsof(hub.port).kind == "empty", message="port release")


def test_storage_prestart_compat(tmp_path):
    from herdr.experience.hub_server import ExperienceHubServer

    server = ExperienceHubServer(host="127.0.0.1", port=0, db_path=tmp_path / "hub.db")
    storage = server.storage
    storage.register_token("compat-token", user_id="u", role="admin", allowed_customers=["*"])
    assert server.storage is storage
    server.start(block=False)
    try:
        assert server.storage is storage
        assert storage.authenticate_token("compat-token") is not None
    finally:
        server.stop()
