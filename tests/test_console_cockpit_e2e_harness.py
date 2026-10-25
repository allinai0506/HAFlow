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
ROOT = Path(__file__).resolve().parent.parent
SHELL_INSPECTOR_CSS = ROOT / "console/static/console/styles/shell-inspector.css"
POLISH_SHELL_CSS = ROOT / "console/static/console/styles/polish-shell.css"

MODAL_OPEN = "waitForFunction(()=>document.getElementById('modal').classList.contains('open'))"
MODAL_CLOSE = "waitForFunction(()=>!document.getElementById('modal').classList.contains('open'))"


def source():
    return JOURNEY.read_text(encoding="utf-8")


def css_rule(source_text, selector):
    match = re.search(rf"(?:^|\n){re.escape(selector)}\s*\{{([^}}]+)\}}", source_text)
    assert match, f"missing CSS rule for {selector}"
    return match.group(1)


def css_pixels(rule, property_name):
    match = re.search(rf"(?:^|;)\s*{re.escape(property_name)}:\s*(\d+)px(?:\s*!important)?\s*;", rule)
    assert match, f"missing {property_name} pixel declaration in {{{rule}}}"
    return int(match.group(1))


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


def test_collapsed_deep_drawer_is_anchored_below_the_workspace():
    """DEF-1: both responsive drawer anchors must clear the full visible head."""
    shell_css = SHELL_INSPECTOR_CSS.read_text(encoding="utf-8")
    drawer_bottoms = [
        int(value)
        for value in re.findall(r"\.deep-drawer\s*\{[^}]*\bbottom:\s*(\d+)px\s*;", shell_css)
    ]
    assert drawer_bottoms == [0, 0]


def test_light_bottom_bar_rule_matches_drawer_head_height():
    """DEF-3: scope the assertion to the two rules instead of the whole file."""
    shell_css = SHELL_INSPECTOR_CSS.read_text(encoding="utf-8")
    polish_css = POLISH_SHELL_CSS.read_text(encoding="utf-8")
    bottom_bar_height = css_pixels(css_rule(shell_css, ".light-bottom-bar"), "height")
    drawer_head_height = css_pixels(css_rule(polish_css, ".drawer-head"), "height")
    assert bottom_bar_height == drawer_head_height
