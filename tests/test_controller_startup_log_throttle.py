"""Bounded startup-wait logging across both Controller wait sites."""

from types import SimpleNamespace

from herdr.log_throttle import StartupWaitLog
from services.controller import advancement, stage_delivery


def _advancement_ports(log, ready=False):
    return SimpleNamespace(
        workflow_closed=lambda _w: False,
        _get_store=lambda: SimpleNamespace(
            get_workflow=lambda _w: {"status": "completed"}),
        workflow_config_for=lambda _w: {"nodes": []},
        project_for_workflow=lambda _w: {"startup_ready": ready},
        _workflow_entry=lambda _w: {"status": "completed"},
        _startup_wait_log=log,
        time=SimpleNamespace(time=lambda: 0),
    )


def test_startup_wait_log_bounds_per_interval():
    log = StartupWaitLog(interval_seconds=600)
    assert [t for t in range(0, 600, 2) if log.waiting("w", t)] == [0]
    assert log.waiting("w", 600) is True
    assert log.finished("w") is True
    assert log.finished("w") is False


def test_startup_wait_log_cache_is_bounded():
    log = StartupWaitLog(interval_seconds=600, max_entries=3)
    for index in range(10):
        assert log.waiting(f"wf-{index}", 1000) is True
    assert len(log._last) == 3
    assert "wf-9" in log._last


def test_advancement_hold_emits_bounded_lines(capsys):
    log = StartupWaitLog(interval_seconds=600)
    ports = _advancement_ports(log)
    for tick in range(0, 600, 2):
        ports.time = SimpleNamespace(time=lambda t=tick: t)
        advancement.check_workflow_stage_advance(ports, "wf")
    out = capsys.readouterr().out
    assert 1 <= out.count("[STARTUP WAIT] workflow=wf") <= 3

    ports.project_for_workflow = lambda _w: {"startup_ready": True}
    ports.time = SimpleNamespace(time=lambda: 600)
    advancement.check_workflow_stage_advance(ports, "wf")
    assert "[STARTUP READY] workflow=wf" in capsys.readouterr().out
    advancement.check_workflow_stage_advance(ports, "wf")
    assert "[STARTUP READY]" not in capsys.readouterr().out


def test_advancement_closed_after_wait_emits_one_end_line(capsys):
    log = StartupWaitLog(interval_seconds=600)
    assert log.waiting("wf", 0) is True
    ports = SimpleNamespace(
        workflow_closed=lambda _w: True,
        _startup_wait_log=log,
    )
    advancement.check_workflow_stage_advance(ports, "wf")
    out = capsys.readouterr().out
    assert out.count("[STARTUP WAIT ENDED] workflow=wf") == 1
    advancement.check_workflow_stage_advance(ports, "wf")
    assert "[STARTUP WAIT ENDED]" not in capsys.readouterr().out


def test_stage_delivery_hold_emits_bounded_lines(capsys):
    log = StartupWaitLog(interval_seconds=600)
    ticks = iter(range(600))
    ports = SimpleNamespace(
        _startup_wait_log=log,
        time=SimpleNamespace(time=lambda: next(ticks), sleep=lambda _s: None),
    )
    for _ in range(600):
        assert stage_delivery._hold_for_startup(
            ports, "wf", {"startup_ready": False}) is True
    out = capsys.readouterr().out
    assert 1 <= out.count("[STARTUP WAIT] workflow=wf") <= 3

    ports.time = SimpleNamespace(time=lambda: 600, sleep=lambda _s: None)
    assert stage_delivery._hold_for_startup(
        ports, "wf", {"startup_ready": True}) is False
    assert "[STARTUP READY] workflow=wf queued event released" in capsys.readouterr().out


def test_closed_workflow_stops_advancement(capsys):
    calls = []
    ports = SimpleNamespace(
        workflow_closed=lambda _wid: True,
        _get_store=lambda: calls.append("store"),
        _startup_wait_log=StartupWaitLog(),
    )
    advancement.check_workflow_stage_advance(ports, "wf")
    assert calls == []
    assert capsys.readouterr().out == ""


def test_terminal_workflow_emits_no_wait_lines(capsys):
    log = StartupWaitLog(interval_seconds=600)
    ports = SimpleNamespace(
        workflow_closed=lambda _wid: True,
        _get_store=lambda: (_ for _ in ()).throw(AssertionError("no store read")),
        _startup_wait_log=log,
    )
    log.waiting("wf", 0)  # simulate a prior wait episode
    capsys.readouterr()
    for tick in range(300):
        advancement.check_workflow_stage_advance(ports, "wf")
    out = capsys.readouterr().out
    assert "[STARTUP WAIT]" not in out
    assert out.count("[STARTUP WAIT ENDED] workflow=wf") == 1
