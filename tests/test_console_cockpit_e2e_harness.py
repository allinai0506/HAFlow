"""Guards that keep the real-browser cockpit journeys CI-stable.

These journeys drive one shared ego-browser daemon. Before this contract the
suite ran them under xdist's default `load` scheduler with ego-browser's
3000ms action default, and CI failed intermittently on whichever test lost the
queue: `CDP request timed out: Input.dispatchKeyEvent/MouseEvent`, or
`<article> intercepts pointer events` while the cockpit re-rendered a card.
Nothing here runs a browser; the guards only pin the harness configuration.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOURNEY = ROOT / "tests" / "test_console_controller_cockpit_e2e.py"
WORKFLOW = ROOT / ".github" / "workflows" / "haflow-tests.yml"

ACTIONS = ("press", "click", "fill")


def source():
    return JOURNEY.read_text(encoding="utf-8")


def test_journeys_pin_one_worker_for_the_shared_daemon():
    tree = ast.parse(source())
    marks = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id == "pytestmark"
    ]
    assert marks, "pytestmark is missing: the journeys would spread across workers"
    rendered = ast.unparse(marks[0])
    assert "xdist_group" in rendered and "ego-browser" in rendered, rendered


def test_ci_uses_the_scheduler_that_honours_xdist_group():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "--dist loadgroup" in text, (
        "loadgroup is required: the default `load` scheduler ignores xdist_group"
    )


def test_every_action_overrides_the_default_timeout():
    text = source()
    offenders = [
        line.strip()
        for line in text.splitlines()
        if re.search(r"page\.(%s)\(" % "|".join(ACTIONS), line)
        and "ACTION_TIMEOUT" not in line
    ]
    assert not offenders, f"actions left on the 3000ms default: {offenders}"


def test_action_timeout_is_raised_well_past_the_default():
    tree = ast.parse(source())
    values = [
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id == "ACTION_TIMEOUT"
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, int)
    ]
    assert values, "ACTION_TIMEOUT must be defined as a literal for the JS prelude"
    # ego-browser's default is 3000ms; the guard pins the raised budget and its
    # single injection point, so a silent revert to the default is caught.
    assert values[0] >= 15000 and len(values) == 1, values


def test_prelude_injects_the_timeout_once():
    text = source()
    assert text.count("const ACTION_TIMEOUT=") == 1, (
        "the timeout must reach each browser subprocess through the shared prelude"
    )


def test_modal_open_is_awaited_before_the_reason_field_is_filled():
    """The modal opens asynchronously after Enter.

    The first CI run after loadgroup failed a *different* way --
    `page.fill: Selector #cockpitReason matched 0 elements` -- because the
    journey filled the reason field without waiting for the modal. Serialization
    and action budgets cannot cover that; the wait must be explicit and
    symmetric with the close-wait that already followed the confirm click.
    """
    text = source()
    press = re.search(r"press\([^\n]*'Enter'[^\n]*\);\n(\s*)await page\.waitForFunction\(\(\)=>document\.getElementById\('modal'\)\.classList\.contains\('open'\)\);", text)
    assert press, "no explicit wait for the modal to open after Enter"
    close = re.search(r"waitForFunction\(\(\)=>!document\.getElementById\('modal'\)\.classList\.contains\('open'\)\)", text)
    assert close, "the matching wait for the modal to close went missing"
