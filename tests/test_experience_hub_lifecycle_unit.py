"""Unit tests for ``herdr/experience/hub_lifecycle.py`` (T-A).

Case-name -> requirement map (see docs/plans/...-plan-risk-validation.md §3.1
and requirements v3 §4b)::

    test_matrix_row_01_absent_status/_stop          §4b row 1
    test_matrix_row_02_port_hub_status/_stop        §4b row 2
    test_matrix_row_03_foreign_status/_stop         §4b row 3
    test_matrix_row_04_ambiguous_status/_stop       §4b row 4
    test_matrix_row_05_registry_hub_status/_stop    §4b row 5
    test_matrix_row_06_stale_status/_stop           §4b row 6
    test_matrix_row_06b_stale_foreign_status/_stop  §4b row 6b
    test_matrix_row_07_degraded_status/_stop        §4b row 7
    test_matrix_row_08_dead_registry_status/_stop   §4b row 8
    test_matrix_row_09_dead_registry_port_hub_*     §4b row 9
    test_matrix_row_10_corrupt_hub_status/_stop     §4b row 10
    test_matrix_row_10b_corrupt_empty_status/_stop  §4b row 10b
    test_matrix_row_11_unverifiable_status/_stop    §4b row 11
    test_exit_codes_status_and_stop_differ          P0-1 (absent/launchd/unknown)
    test_manager_*                                  B20 (three-way manager)
    test_stop_reoccupied_*                          B22 (never 0)
    test_probe_*                                    B26 (three-way probe)
    test_selector_*                                 N-4/B24/P0-5/B23
    test_redact_*                                   B8
    test_identity_*                                 BD-14/B25/B6
    test_registry_*                                 B19/Q8/BD-3/BD-1
    test_stop_endpoint_*                            §3.1.5/AC-22
    test_admin_state_readonly_*                     B4/P1-5
"""

from __future__ import annotations

import ast
import json
import signal
import stat
from pathlib import Path

import pytest

from herdr.experience import hub_lifecycle as lifecycle

PORT = 51888
DB = "/tmp/eh-lifecycle/hub.db"
HUB_ARGV = (
    "/usr/bin/python3",
    "bin/herdr-experience-hub",
    "start",
    "--host",
    "127.0.0.1",
    "--port",
    str(PORT),
    "--db-path",
    DB,
)


def selector(*, command: str = "stop", port=PORT, db_path=None, host=None, pid=None):
    return lifecycle.parse_selector(
        command=command,
        port=port,
        db_path=db_path,
        host=host,
        pid=pid,
    )


def entry(
    *,
    pid: int = 100,
    host: str = "127.0.0.1",
    port: int = PORT,
    db_path: str = DB,
    started_at: float = 1000.0,
    corrupt: bool = False,
) -> lifecycle.RegistryEntry:
    return lifecycle.RegistryEntry(
        path=Path(f"/tmp/eh-registry/{pid}.json"),
        pid=pid,
        host=host,
        port=port,
        db_path=db_path,
        started_at=started_at,
        script="bin/herdr-experience-hub",
        manager=lifecycle.MANAGER_UNKNOWN,
        schema_version=1,
        corrupt=corrupt,
    )


def process(
    *,
    pid: int = 100,
    status: str = "alive",
    argv=HUB_ARGV,
    started_at: float = 1000.0,
) -> lifecycle.ProcessInfo:
    return lifecycle.ProcessInfo(
        pid=pid,
        status=status,
        started_at=started_at,
        argv=tuple(argv),
    )


def listener(
    *,
    pid: int = 100,
    host: str = "127.0.0.1",
    port: int = PORT,
    wildcard: bool = False,
    is_hub=True,
) -> lifecycle.Listener:
    return lifecycle.Listener(pid=pid, host=host, port=port, wildcard=wildcard, is_hub=is_hub)


# ---------------------------------------------------------------------------
# 13-row matrix, status and stop (P0-1)
# ---------------------------------------------------------------------------

MATRIX_CASES = [
    (
        "row_01_absent",
        {"listeners": (), "registry": None, "registry_state": "absent", "process": None},
        {"state": lifecycle.STATE_ABSENT, "attribution": lifecycle.ATTRIBUTION_NONE, "action": lifecycle.ACTION_NONE, "exit_code": 3},
        {"state": lifecycle.STATE_ABSENT, "attribution": lifecycle.ATTRIBUTION_NONE, "action": lifecycle.ACTION_NONE, "exit_code": 0},
    ),
    (
        "row_02_port_hub",
        {"listeners": (listener(),), "registry": None, "registry_state": "absent", "process": None,
             "manager": lifecycle.MANAGER_AD_HOC},
        {"state": lifecycle.STATE_RUNNING, "attribution": lifecycle.ATTRIBUTION_PORT, "action": lifecycle.ACTION_NONE, "exit_code": 0},
        {"state": lifecycle.STATE_RUNNING, "attribution": lifecycle.ATTRIBUTION_PORT, "action": lifecycle.ACTION_SIGNAL, "exit_code": 0},
    ),
    (
        "row_03_foreign",
        {"listeners": (listener(is_hub=False),), "registry": None, "registry_state": "absent", "process": None},
        {"state": lifecycle.STATE_FOREIGN, "action": lifecycle.ACTION_REFUSE, "exit_code": 4},
        {"state": lifecycle.STATE_FOREIGN, "action": lifecycle.ACTION_REFUSE, "exit_code": 4},
    ),
    (
        "row_04_ambiguous",
        {"listeners": (listener(pid=100), listener(pid=200, is_hub=False)), "registry": None, "registry_state": "absent"},
        {"state": lifecycle.STATE_AMBIGUOUS, "action": lifecycle.ACTION_REFUSE, "exit_code": 4},
        {"state": lifecycle.STATE_AMBIGUOUS, "action": lifecycle.ACTION_REFUSE, "exit_code": 4},
    ),
    (
        "row_05_registry_hub",
        {"listeners": (listener(pid=100),), "registry": entry(pid=100), "registry_state": "valid",
             "process": process(pid=100), "manager": lifecycle.MANAGER_AD_HOC},
        {"state": lifecycle.STATE_RUNNING, "attribution": lifecycle.ATTRIBUTION_REGISTRY, "action": lifecycle.ACTION_NONE, "exit_code": 0},
        {"state": lifecycle.STATE_RUNNING, "attribution": lifecycle.ATTRIBUTION_REGISTRY, "action": lifecycle.ACTION_SIGNAL, "exit_code": 0},
    ),
    (
        "row_06_stale",
        {"listeners": (), "registry": entry(pid=100), "registry_state": "valid",
             "process": process(pid=100, argv=("/bin/sleep", "60"))},
        {"state": lifecycle.STATE_STALE, "action": lifecycle.ACTION_CLEANUP, "exit_code": 3, "registry_state": "stale"},
        {"state": lifecycle.STATE_STALE, "action": lifecycle.ACTION_CLEANUP, "exit_code": 0, "registry_state": "stale"},
    ),
    (
        "row_06b_stale_foreign",
        {"listeners": (listener(pid=200, is_hub=False),), "registry": entry(pid=100), "registry_state": "valid",
             "process": process(pid=100, argv=("/bin/sleep", "60"))},
        {"state": lifecycle.STATE_FOREIGN, "action": lifecycle.ACTION_REFUSE, "exit_code": 4},
        {"state": lifecycle.STATE_FOREIGN, "action": lifecycle.ACTION_REFUSE, "exit_code": 4},
    ),
    (
        "row_07_degraded",
        {"listeners": (), "registry": entry(pid=100), "registry_state": "valid",
             "process": process(pid=100), "manager": lifecycle.MANAGER_AD_HOC},
        {"state": lifecycle.STATE_DEGRADED, "action": lifecycle.ACTION_REFUSE, "exit_code": 4},
        {"state": lifecycle.STATE_DEGRADED, "action": lifecycle.ACTION_REFUSE, "exit_code": 4},
    ),
    (
        "row_08_dead_registry",
        {"listeners": (), "registry": entry(pid=100), "registry_state": "valid", "process": process(pid=100, status="dead")},
        {"state": lifecycle.STATE_ABSENT, "action": lifecycle.ACTION_CLEANUP, "exit_code": 3, "registry_state": "stale"},
        {"state": lifecycle.STATE_ABSENT, "action": lifecycle.ACTION_CLEANUP, "exit_code": 0, "registry_state": "stale"},
    ),
    (
        "row_09_dead_registry_port_hub",
        {"listeners": (listener(pid=200, host="127.0.0.1"),), "registry": entry(pid=100), "registry_state": "valid",
             "process": process(pid=100, status="dead"), "manager": lifecycle.MANAGER_AD_HOC},
        {"state": lifecycle.STATE_RUNNING, "attribution": lifecycle.ATTRIBUTION_PORT, "action": lifecycle.ACTION_NONE, "exit_code": 0},
        {"state": lifecycle.STATE_RUNNING, "attribution": lifecycle.ATTRIBUTION_PORT, "action": lifecycle.ACTION_SIGNAL, "exit_code": 0},
    ),
    (
        "row_10_corrupt_hub",
        {"listeners": (listener(pid=200),), "registry": None, "registry_state": "corrupt", "process": None,
             "manager": lifecycle.MANAGER_AD_HOC},
        {"state": lifecycle.STATE_RUNNING, "attribution": lifecycle.ATTRIBUTION_PORT, "action": lifecycle.ACTION_NONE,
             "exit_code": 0, "registry_state": "corrupt"},
        {"state": lifecycle.STATE_RUNNING, "attribution": lifecycle.ATTRIBUTION_PORT, "action": lifecycle.ACTION_SIGNAL,
             "exit_code": 0, "registry_state": "corrupt"},
    ),
    (
        "row_10b_corrupt_empty",
        {"listeners": (), "registry": None, "registry_state": "corrupt", "process": None},
        {"state": lifecycle.STATE_UNKNOWN, "action": lifecycle.ACTION_CLEANUP_REFUSE, "exit_code": 4, "registry_state": "corrupt"},
        {"state": lifecycle.STATE_UNKNOWN, "action": lifecycle.ACTION_CLEANUP_REFUSE, "exit_code": 4, "registry_state": "corrupt"},
    ),
    (
        "row_11_unverifiable",
        {"listeners": (), "registry": None, "registry_state": "absent", "process": None, "probe_status": "unverifiable"},
        {"state": lifecycle.STATE_UNVERIFIABLE, "action": lifecycle.ACTION_REFUSE, "exit_code": 5},
        {"state": lifecycle.STATE_UNVERIFIABLE, "action": lifecycle.ACTION_REFUSE, "exit_code": 5},
    ),
]


@pytest.mark.parametrize("case_name,case_kwargs,status_expected,stop_expected", MATRIX_CASES,
                         ids=[case[0] for case in MATRIX_CASES])
def test_matrix_row_status(case_name, case_kwargs, status_expected, stop_expected):
    classification = lifecycle.classify(operation="status", selector=selector(), **case_kwargs)
    for field, value in status_expected.items():
        assert getattr(classification, field) == value, (
            f"{case_name} expected {field}={value!r}, got {getattr(classification, field)!r}"
        )


@pytest.mark.parametrize("case_name,case_kwargs,status_expected,stop_expected", MATRIX_CASES,
                         ids=[case[0] for case in MATRIX_CASES])
def test_matrix_row_stop(case_name, case_kwargs, status_expected, stop_expected):
    classification = lifecycle.classify(operation="stop", selector=selector(), **case_kwargs)
    for field, value in stop_expected.items():
        assert getattr(classification, field) == value, (
            f"{case_name} expected {field}={value!r}, got {getattr(classification, field)!r}"
        )


def test_exit_codes_status_and_stop_differ():
    """P0-1: absent 3/0; launchd 0/6; manager-unknown 0/5; running 0/0."""
    absent = lifecycle.classify(operation="status", selector=selector(), listeners=())
    assert absent.exit_code == lifecycle.EXIT_ABSENT
    assert lifecycle.classify(operation="stop", selector=selector(), listeners=()).exit_code == lifecycle.EXIT_OK

    common = {
        "selector": selector(),
        "listeners": (listener(),),
        "registry": None,
        "registry_state": "absent",
    }
    launchd_status = lifecycle.classify(operation="status", selector=common["selector"],
                                        listeners=common["listeners"], registry=None,
                                        registry_state="absent", manager=lifecycle.MANAGER_LAUNCHD)
    launchd_stop = lifecycle.classify(operation="stop", selector=common["selector"],
                                      listeners=common["listeners"], registry=None,
                                      registry_state="absent", manager=lifecycle.MANAGER_LAUNCHD)
    assert (launchd_status.exit_code, launchd_status.action) == (0, lifecycle.ACTION_NONE)
    assert (launchd_stop.exit_code, launchd_stop.action) == (6, lifecycle.ACTION_REFUSE)

    unknown_status = lifecycle.classify(operation="status", selector=common["selector"],
                                        listeners=common["listeners"], registry=None,
                                        registry_state="absent", manager=lifecycle.MANAGER_UNKNOWN)
    unknown_stop = lifecycle.classify(operation="stop", selector=common["selector"],
                                      listeners=common["listeners"], registry=None,
                                      registry_state="absent", manager=lifecycle.MANAGER_UNKNOWN)
    assert (unknown_status.exit_code, unknown_status.action) == (0, lifecycle.ACTION_NONE)
    assert (unknown_stop.exit_code, unknown_stop.action) == (5, lifecycle.ACTION_REFUSE)

    running_stop = lifecycle.classify(operation="stop", selector=common["selector"],
                                      listeners=common["listeners"], registry=None,
                                      registry_state="absent", manager=lifecycle.MANAGER_AD_HOC)
    assert (running_stop.exit_code, running_stop.action) == (0, lifecycle.ACTION_SIGNAL)


# ---------------------------------------------------------------------------
# B20 manager three-way
# ---------------------------------------------------------------------------

LAUNCHCTL_LIST_RUNNING = """{
\t"StandardOutPath" = "/tmp/out.log";
\t"Label" = "com.user.herdr-experience-hub";
\t"PID" = 47164;
};"""

LAUNCHCTL_LIST_NOT_RUNNING = """{
\t"Label" = "com.user.herdr-experience-hub";
\t"LastExitStatus" = 0;
};"""

LAUNCHCTL_PRINT = """gui/501/com.user.herdr-experience-hub = {
\tactive count = 1
\tstate = running
\tpid = 47164
\tprogram = /opt/homebrew/bin/python3
};"""


def test_manager_launchd_running():
    probe = lifecycle.classify_manager(LAUNCHCTL_LIST_RUNNING, returncode=0, target_pid=47164)
    assert probe.manager == lifecycle.MANAGER_LAUNCHD


def test_manager_loaded_not_running_is_ad_hoc():
    probe = lifecycle.classify_manager(LAUNCHCTL_LIST_NOT_RUNNING, returncode=0, target_pid=80677)
    assert probe.manager == lifecycle.MANAGER_AD_HOC


def test_manager_other_pid_is_ad_hoc():
    probe = lifecycle.classify_manager(LAUNCHCTL_LIST_RUNNING, returncode=0, target_pid=80677)
    assert probe.manager == lifecycle.MANAGER_AD_HOC


def test_manager_command_failed_is_unknown_and_stop_refuses():
    probe = lifecycle.classify_manager("", returncode=113, target_pid=80677,
                                       stderr="Could not find service in domain for port")
    assert probe.manager == lifecycle.MANAGER_UNKNOWN

    classification = lifecycle.classify(
        operation="stop",
        selector=selector(),
        listeners=(listener(pid=80677),),
        registry=None,
        registry_state="absent",
        manager=probe.manager,
    )
    assert classification.exit_code == lifecycle.EXIT_UNVERIFIABLE
    assert classification.action == lifecycle.ACTION_REFUSE


def test_manager_timeout_is_unknown():
    probe = lifecycle.classify_manager("", timed_out=True, target_pid=1)
    assert probe.manager == lifecycle.MANAGER_UNKNOWN


def test_manager_missing_tool_is_unknown():
    probe = lifecycle.classify_manager("", missing=True, target_pid=1)
    assert probe.manager == lifecycle.MANAGER_UNKNOWN


def test_manager_print_format():
    probe = lifecycle.classify_manager(LAUNCHCTL_PRINT, returncode=0, target_pid=47164)
    assert probe.manager == lifecycle.MANAGER_LAUNCHD
    assert lifecycle.parse_launchctl_pid(LAUNCHCTL_PRINT) == 47164


# ---------------------------------------------------------------------------
# B22 re-occupied endpoint
# ---------------------------------------------------------------------------


def _fake_stop_runtime(reoccupied_manager, new_listener):
    state = {"sent": [], "calls": 0}

    def verify():
        return lifecycle.IdentityCheck(True, True, True)

    def probe_listeners():
        state["calls"] += 1
        return (new_listener,)

    def pid_alive(pid):
        return False

    def send_signal(pid, sig):
        state["sent"].append((pid, sig))

    outcome = lifecycle.stop_endpoint(
        target_pid=100,
        verify=verify,
        probe_listeners=probe_listeners,
        pid_alive=pid_alive,
        manager_for_pid=lambda pid: reoccupied_manager,
        send_signal=send_signal,
    )
    return outcome, state


def test_stop_reoccupied_launchd_is_6():
    outcome, state = _fake_stop_runtime(
        lifecycle.MANAGER_LAUNCHD,
        listener(pid=200, is_hub=True),
    )
    assert outcome.exit_code == lifecycle.EXIT_LAUNCHD_MANAGED
    assert outcome.reoccupied is True
    assert state["sent"] == [(100, signal.SIGTERM)]


def test_stop_reoccupied_foreign_is_4():
    outcome, _ = _fake_stop_runtime(
        lifecycle.MANAGER_AD_HOC,
        listener(pid=200, is_hub=False),
    )
    assert outcome.exit_code == lifecycle.EXIT_AMBIGUOUS
    assert outcome.state == lifecycle.STATE_FOREIGN


def test_stop_reoccupied_unknown_is_4_not_0():
    outcome, _ = _fake_stop_runtime(
        lifecycle.MANAGER_UNKNOWN,
        listener(pid=200, is_hub=None),
    )
    assert outcome.exit_code == lifecycle.EXIT_AMBIGUOUS
    assert outcome.exit_code != lifecycle.EXIT_OK


def test_stop_endpoint_success_requires_no_listener():
    sent = []
    clocks = iter([0.0, 0.0, 1.0])

    outcome = lifecycle.stop_endpoint(
        target_pid=100,
        verify=lambda: lifecycle.IdentityCheck(True, True, True),
        probe_listeners=lambda: (),
        pid_alive=lambda pid: False,
        send_signal=lambda pid, sig: sent.append((pid, sig)),
        clock=lambda: next(clocks, 10.0),
    )
    assert outcome.exit_code == lifecycle.EXIT_OK
    assert outcome.signaled is True
    assert sent == [(100, signal.SIGTERM)]


def test_stop_endpoint_timeout_keeps_signal_unscalated():
    sent = []
    clock_values = iter([0.0, 0.1, 100.0])

    outcome = lifecycle.stop_endpoint(
        target_pid=100,
        verify=lambda: lifecycle.IdentityCheck(True, True, True),
        probe_listeners=lambda: (listener(pid=100),),
        pid_alive=lambda pid: True,
        send_signal=lambda pid, sig: sent.append((pid, sig)),
        timeout_seconds=1.0,
        clock=lambda: next(clock_values, 100.0),
        sleep=lambda _seconds: None,
    )
    assert outcome.exit_code == lifecycle.EXIT_TIMEOUT
    assert outcome.timed_out is True
    assert sent == [(100, signal.SIGTERM)]


def test_stop_endpoint_refuses_when_recheck_fails():
    sent = []
    outcome = lifecycle.stop_endpoint(
        target_pid=100,
        verify=lambda: lifecycle.IdentityCheck(False, True, True, ("pid does not hold the endpoint",)),
        probe_listeners=lambda: (),
        pid_alive=lambda pid: True,
        send_signal=lambda pid, sig: sent.append((pid, sig)),
    )
    assert outcome.exit_code == lifecycle.EXIT_AMBIGUOUS
    assert outcome.signaled is False
    assert sent == []


# ---------------------------------------------------------------------------
# B26 probe classification
# ---------------------------------------------------------------------------

LSOF_FIELD_OUTPUT = """p47164
cPython
f5
n127.0.0.1:8780
"""

LSOF_TABLE_OUTPUT = """COMMAND   PID USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME
Python  47164 user    5u  IPv4 0xf5a89ce3b6803049      0t0  TCP 127.0.0.1:8780 (LISTEN)
"""


def test_probe_rc0_has_data():
    outcome = lifecycle.classify_probe_result(0, LSOF_FIELD_OUTPUT, "")
    assert outcome.kind == "data"
    assert outcome.listeners[0].pid == 47164
    assert outcome.listeners[0].host == "127.0.0.1"
    assert outcome.listeners[0].port == 8780


def test_probe_rc0_table_format_is_supported():
    outcome = lifecycle.classify_probe_result(0, LSOF_TABLE_OUTPUT, "")
    assert outcome.kind == "data"
    assert outcome.listeners[0].pid == 47164


def test_probe_rc0_wildcard_is_marked():
    outcome = lifecycle.classify_probe_result(0, "p1\nn*:9000\n", "")
    assert outcome.kind == "data"
    assert outcome.listeners[0].wildcard is True


def test_probe_rc1_empty_stderr_is_empty():
    outcome = lifecycle.classify_probe_result(1, "", "")
    assert outcome.kind == "empty"
    assert outcome.listeners == ()


def test_probe_rc1_with_stderr_is_unverifiable():
    outcome = lifecycle.classify_probe_result(1, "", "lsof: permission denied")
    assert outcome.kind == "unverifiable"


def test_probe_rc2_is_unverifiable():
    outcome = lifecycle.classify_probe_result(2, "", "")
    assert outcome.kind == "unverifiable"


def test_probe_timeout_is_unverifiable():
    assert lifecycle.classify_probe_result(0, "", "", timed_out=True).kind == "unverifiable"


def test_probe_missing_tool_is_unverifiable():
    assert lifecycle.classify_probe_result(0, "", "", missing=True).kind == "unverifiable"


def test_probe_rc0_unparsable_is_unverifiable():
    assert lifecycle.classify_probe_result(0, "garbage output", "").kind == "unverifiable"


def test_ps_dead_and_unverifiable():
    dead = lifecycle.classify_ps_result(99999, 1, "", "")
    assert dead.status == "dead"
    missing = lifecycle.classify_ps_result(99999, 0, "", "", missing=True)
    assert missing.status == "unverifiable"
    assert lifecycle.classify_ps_result(99999, 2, "", "boom").status == "unverifiable"


def test_ps_parses_lstart_and_argv():
    line = "47164     1 Thu Oct 22 21:33:35 2026     /usr/bin/python3 bin/herdr-experience-hub start\n"
    info = lifecycle.parse_ps_output(line)
    assert info.status == "alive"
    assert info.ppid == 1
    assert info.started_at is not None
    assert info.argv[-1] == "start"


# ---------------------------------------------------------------------------
# selectors
# ---------------------------------------------------------------------------


def test_selector_start_port_zero_allowed():
    assert selector(command="start", port=0).port == 0


def test_selector_status_and_stop_reject_port_zero():
    with pytest.raises(lifecycle.UsageError):
        selector(command="status", port=0)
    with pytest.raises(lifecycle.UsageError):
        selector(command="stop", port=0)


def test_selector_pid_requires_port():
    with pytest.raises(lifecycle.UsageError):
        selector(command="stop", port=None, pid=123)
    with pytest.raises(lifecycle.UsageError):
        selector(command="stop", port=None, db_path=DB, pid=123)
    with pytest.raises(lifecycle.UsageError):
        selector(command="status", port=PORT, pid=123)
    assert selector(command="stop", port=PORT, pid=123).pid == 123


def test_selector_host_requires_port_for_status_and_stop():
    with pytest.raises(lifecycle.UsageError):
        selector(command="status", port=None, host="127.0.0.1")
    with pytest.raises(lifecycle.UsageError):
        selector(command="stop", port=None, db_path=DB, host="127.0.0.1")
    assert selector(command="status", port=PORT, host="127.0.0.1").host == "127.0.0.1"


def test_selector_requires_port_or_db():
    with pytest.raises(lifecycle.UsageError):
        selector(command="status", port=None)
    with pytest.raises(lifecycle.UsageError):
        selector(command="stop", port=None)


def test_selector_port_range():
    with pytest.raises(lifecycle.UsageError):
        selector(port=65536)
    with pytest.raises(lifecycle.UsageError):
        selector(port=-1)


def test_selector_db_path_is_normalized():
    parsed = selector(command="status", port=None, db_path="~/probe/hub.db")
    assert parsed.db_path is not None
    assert parsed.db_path.is_absolute()
    assert str(parsed.db_path).endswith("probe/hub.db")


def test_registry_resolution_db_path_zero_and_multi(tmp_path):
    sel = selector(command="stop", port=None, db_path=DB)
    zero = lifecycle.resolve_registry_port(sel, ())
    assert zero.exit_code == lifecycle.EXIT_AMBIGUOUS

    one = lifecycle.resolve_registry_port(sel, (entry(pid=100),))
    assert one.exit_code == lifecycle.EXIT_OK
    assert one.port == PORT

    multi = lifecycle.resolve_registry_port(sel, (entry(pid=100, port=5001), entry(pid=200, port=5002)))
    assert multi.exit_code == lifecycle.EXIT_AMBIGUOUS
    assert multi.port is None


def test_registry_resolution_port_db_intersection_empty_is_ambiguous():
    sel = selector(command="stop", port=PORT, db_path="/tmp/other/hub.db")
    resolved = lifecycle.resolve_registry_port(sel, (entry(pid=100),))
    assert resolved.exit_code == lifecycle.EXIT_AMBIGUOUS


# ---------------------------------------------------------------------------
# redact
# ---------------------------------------------------------------------------


def test_redact_admin_token_forms_and_env_value():
    argv = (
        "python3",
        "bin/herdr-experience-hub",
        "start",
        "--admin-token",
        "sentinel-secret",
        "--admin-token=second-secret",
        "--token",
        "third-secret",
        "--token=fourth-secret",
        "--port",
        "8780",
    )
    redacted = lifecycle.redact_argv(argv, secrets=("sentinel-secret",))
    text = " ".join(redacted)
    for secret in ("sentinel-secret", "second-secret", "third-secret", "fourth-secret"):
        assert secret not in text
    assert text.count(lifecycle.REDACTED) == 4
    assert "bin/herdr-experience-hub" in text


def test_redact_text_masks_injected_secret():
    assert lifecycle.redact_text("failed with sekret", secrets=("sekret",)) == "failed with <redacted>"


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------


def test_identity_relative_and_absolute_and_snapshot():
    relative = ("python3", "bin/herdr-experience-hub", "start", "--port", "1")
    absolute = ("python3", "/opt/releases/sha/bin/herdr-experience-hub", "start")
    snapshot = ("/usr/bin/python3", "/Users/x/.herdr-controller/releases/sha/bin/herdr-experience-hub", "start")
    assert lifecycle.is_hub_argv(relative)
    assert lifecycle.is_hub_argv(absolute)
    assert lifecycle.is_hub_argv(snapshot)


def test_identity_requires_adjacent_start():
    assert not lifecycle.is_hub_argv(("python3", "bin/herdr-experience-hub", "token", "create"))
    assert not lifecycle.is_hub_argv(("python3", "bin/herdr-experience-hub", "--help"))
    assert not lifecycle.is_hub_argv(("python3", "bin/not-herdr-experience-hub", "start"))


def test_identity_three_terms_independent():
    sel = selector()
    ok = lifecycle.check_identity(
        target_pid=100,
        listeners=(listener(pid=100),),
        process=process(pid=100),
        registry=entry(pid=100),
        selector=sel,
    )
    assert (ok.holds_endpoint, ok.holds_cmdline, ok.holds_registry) == (True, True, True)

    no_endpoint = lifecycle.check_identity(
        target_pid=100,
        listeners=(listener(pid=200),),
        process=process(pid=100),
        registry=entry(pid=100),
        selector=sel,
    )
    assert no_endpoint.holds_endpoint is False
    assert no_endpoint.ok is False

    no_cmdline = lifecycle.check_identity(
        target_pid=100,
        listeners=(listener(pid=100),),
        process=process(pid=100, argv=("/bin/sleep", "60")),
        registry=None,
        selector=sel,
    )
    assert no_cmdline.holds_cmdline is False

    wrong_registry = lifecycle.check_identity(
        target_pid=100,
        listeners=(listener(pid=100),),
        process=process(pid=100),
        registry=entry(pid=100, db_path="/tmp/other/hub.db"),
        selector=selector(db_path=DB),
    )
    assert wrong_registry.holds_registry is False


def test_host_matching_and_wildcards():
    assert lifecycle.host_matches("127.0.0.1", "0.0.0.0") is True
    assert lifecycle.host_matches("localhost", "::1") is True
    assert lifecycle.host_matches("0.0.0.0", "127.0.0.1") is False

    exact = listener(pid=1, host="127.0.0.1")
    wildcard = listener(pid=2, host="0.0.0.0", wildcard=True)
    selected = lifecycle.select_listeners((exact, wildcard), "127.0.0.1")
    assert [item.pid for item in selected] == [1]


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_registry_missing_root_is_empty_and_not_created(tmp_path):
    root = tmp_path / "does-not-exist"
    assert lifecycle.scan_registry(root) == ()
    assert not root.exists()


def test_registry_write_contract_and_dual_host(tmp_path):
    root = tmp_path / "run" / "experience-hub"
    first = lifecycle.write_registry(
        pid=100, host="127.0.0.1", port=PORT, db_path=tmp_path / "hub.db",
        script="/repo/bin/herdr-experience-hub", root=root,
    )
    second = lifecycle.write_registry(
        pid=200, host="0.0.0.0", port=PORT, db_path=tmp_path / "hub2.db",
        script="/repo/bin/herdr-experience-hub", root=root,
    )
    assert first.name == "100.json"
    assert second.name == "200.json"
    assert first != second
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert stat.S_IMODE(root.stat().st_mode) == 0o700

    entries = lifecycle.scan_registry(root)
    assert len(entries) == 2
    assert {item.host for item in entries} == {"127.0.0.1", "0.0.0.0"}
    payload = json.loads(first.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert set(payload) == {
        "schema_version", "pid", "host", "port", "db_path", "started_at", "script", "manager",
    }
    assert "argv" not in payload
    assert "admin-token" not in first.read_text(encoding="utf-8")


def test_registry_corrupt_entry_is_readable_but_never_trusted(tmp_path):
    root = tmp_path / "registry"
    root.mkdir(parents=True)
    (root / "100.json").write_text('{"pid": 100, "port": 51888,', encoding="utf-8")
    entries = lifecycle.scan_registry(root)
    assert len(entries) == 1
    assert entries[0].corrupt is True
    assert entries[0].port == PORT

    classification = lifecycle.classify(
        operation="stop",
        selector=selector(),
        listeners=(),
        registry=None,
        registry_state="corrupt",
        process=None,
    )
    assert classification.exit_code == lifecycle.EXIT_AMBIGUOUS
    assert classification.action == lifecycle.ACTION_CLEANUP_REFUSE


def test_registry_remove_rechecks_content(tmp_path):
    root = tmp_path / "registry"
    path = lifecycle.write_registry(pid=100, host="127.0.0.1", port=PORT,
                                    db_path=tmp_path / "hub.db", root=root)
    entries = lifecycle.scan_registry(root)
    assert lifecycle.remove_registry_entry(entries[0]) is True
    assert not path.exists()


def test_registry_payload_has_no_token(tmp_path):
    root = tmp_path / "registry"
    path = lifecycle.write_registry(pid=100, host="127.0.0.1", port=PORT,
                                    db_path=tmp_path / "hub.db", root=root)
    text = path.read_text(encoding="utf-8")
    assert "token" not in text.lower()
    assert "argv" not in text


# ---------------------------------------------------------------------------
# report schema
# ---------------------------------------------------------------------------


def test_status_report_schema_has_no_argv():
    sel = selector(command="status")
    classification = lifecycle.classify(
        operation="status",
        selector=sel,
        listeners=(listener(pid=100),),
        registry=entry(pid=100),
        registry_state="valid",
        process=process(pid=100),
        manager=lifecycle.MANAGER_AD_HOC,
    )
    report = lifecycle.build_status_report(
        classification, selector=sel, listeners=(listener(pid=100),),
        registry=entry(pid=100), process=process(pid=100),
    )
    assert set(report) == {
        "schema_version", "state", "manager", "attribution", "pid", "host", "port",
        "db_path", "db_exists", "started_at", "listeners", "registry_state",
    }
    assert report["schema_version"] == 1
    assert report["attribution"] == lifecycle.ATTRIBUTION_REGISTRY
    assert "argv" not in json.dumps(report)
    assert report["listeners"] == [
        {"pid": 100, "host": "127.0.0.1", "port": PORT, "wildcard": False}
    ]


# ---------------------------------------------------------------------------
# read-only admin check (B4/P1-5)
# ---------------------------------------------------------------------------


def test_admin_state_readonly_missing_creates_nothing(tmp_path):
    db = tmp_path / "nested" / "hub.db"
    assert lifecycle.admin_state_readonly(db) == "missing"
    assert not db.exists()
    assert not db.parent.exists()


def test_admin_state_readonly_detects_admin(tmp_path):
    import sqlite3

    db = tmp_path / "hub.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE auth_tokens (token_hash TEXT PRIMARY KEY, user_id TEXT, role TEXT, "
        "allowed_customers_json TEXT, created_at REAL)"
    )
    connection.execute(
        "INSERT INTO auth_tokens VALUES ('h', 'admin', 'admin', '[\"*\"]', 0)"
    )
    connection.commit()
    connection.close()
    assert lifecycle.admin_state_readonly(db) == "initialized"


# ---------------------------------------------------------------------------
# static dependency gate (R9 exploit surface)
# ---------------------------------------------------------------------------


def test_lifecycle_has_no_server_or_storage_dependency():
    source_path = Path(lifecycle.__file__)
    source = source_path.read_text(encoding="utf-8")
    assert "hub_server" not in source
    assert "HubStorage" not in source

    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any("hub_server" in name for name in imported)
    assert not any("HubStorage" in name for name in imported)


def test_module_has_no_psutil_dependency():
    source = Path(lifecycle.__file__).read_text(encoding="utf-8")
    assert "psutil" not in source
