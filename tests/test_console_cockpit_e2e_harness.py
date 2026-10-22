"""Guards for the one cockpit-journey race this change actually fixed.

`confirm_action` pressed Enter and then filled the modal's reason field with no
wait in between. The cockpit opens that modal asynchronously, so the fill raced
and intermittently failed with `Selector #cockpitReason matched 0 elements`.
The journeys now wait for the modal to open, symmetric with the existing wait
for it to close. These guards keep that pair from being half-removed later.

Two other intermittent failures in this file are NOT addressed here and have no
guard pretending otherwise: ego-browser's pointer-interception check reports a
fixed 3000ms figure that does not follow the `timeout` option, and one journey
hits a `page.evaluate` that stays pending past 15000ms. Both are tracked in the
follow-up issue; neither is a scheduler-contention problem (a controlled
`--dist loadgroup` run still failed), so do not "fix" them by re-adding
xdist_group, loadgroup or larger action budgets.
"""

import re
from pathlib import Path

JOURNEY = Path(__file__).resolve().parent / "test_console_controller_cockpit_e2e.py"

MODAL_OPEN = "waitForFunction(()=>document.getElementById('modal').classList.contains('open'))"
MODAL_CLOSE = "waitForFunction(()=>!document.getElementById('modal').classList.contains('open'))"


def source():
    return JOURNEY.read_text(encoding="utf-8")


def test_every_modal_is_opened_before_its_reason_field_is_filled():
    """Each reason fill needs a modal-open wait in the same browser call.

    The journeys open the modal in an earlier subprocess, so the wait has to sit
    immediately above the fill it protects, not merely somewhere earlier.
    """
    lines = source().splitlines()
    for index, line in enumerate(lines):
        # Only executable fills; the module docstring quotes this selector too.
        if not line.lstrip().startswith("await page.fill('#cockpitReason'"):
            continue
        preceding = lines[max(0, index - 3):index]
        assert any(MODAL_OPEN in item for item in preceding), (
            f"line {index + 1} fills #cockpitReason without a modal-open wait"
        )


def test_open_and_close_waits_stay_paired():
    text = source()
    assert MODAL_OPEN in text, "modal-open wait was removed"
    assert MODAL_CLOSE in text, "modal-close wait was removed"


def test_no_contention_fixes_reappear_without_evidence():
    """The loadgroup and action-timeout experiments were falsified by their own run."""
    text = source()
    for gone in ("xdist_group", "ACTION_TIMEOUT"):
        assert gone not in text, f"{gone} returned: it was disproved, not merely unused"


def test_failure_diagnostics_collected_on_browser_error():
    """Issue #326 requires diagnostic telemetry on browser() failure path."""
    text = source()
    assert "collect_cockpit_failure_diagnostics" in text, "failure path must collect diagnostics"
    assert "elementFromPoint" in text, "diagnostics must sample elementFromPoint"
    assert "controllerReceipt" in text, "diagnostics must record controllerReceipt state"
    assert "inFlightFetches" in text, "diagnostics must capture inFlightFetches"


def test_open_cockpit_waits_for_decision_data():
    """open_cockpit must wait for decision data to avoid asynchronous cockpit repaints."""
    text = source()
    assert "state.decisionData!==undefined" in text, "open_cockpit must await decisionData settlement"


def test_return_from_controller_cockpit_is_awaited_before_canvas_click():
    """Returning from controller cockpit must wait for flowWrap display before clicking canvas."""
    text = source()
    assert "returnFromControllerCockpit" in text
    assert "state.workflowId!=='__ctl__'&&document.getElementById('flowWrap')?.style.display==='flex'" in text


def test_collect_cockpit_failure_diagnostics_handles_missing_page_or_binary():
    from tests.test_console_controller_cockpit_e2e import collect_cockpit_failure_diagnostics
    assert collect_cockpit_failure_diagnostics(None, "p1", "await page.click('x')", None) == ""
    assert collect_cockpit_failure_diagnostics("ego-browser", None, "await page.click('x')", None) == ""
