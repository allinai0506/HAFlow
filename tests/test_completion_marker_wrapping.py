"""Wrap-tolerant completion-marker detection (regression).

A pane is a hard-wrapped terminal screen, not a logical document.  TUI
message bodies keep a left margin, so a long ``HERDR_TASK_DONE:<task_id>``
token is split across physical lines and a naive ``literal in screen`` read
can never see it.  That stranded ``plan-adversarial-*`` in ``working`` with
every deliverable already on disk, because ``marker_present`` stayed False
and ``compare_and_set_completion_transition`` rejected forever with
``completion_marker_absent``.
"""

import re

import pytest

from herdr.completion import (
    BLOCKER_MARKER_PREFIX,
    DONE_MARKER_PREFIX,
    ORCH_MARKER_PREFIX,
    marker_literal,
    marker_present,
)

TASK_ID = "plan-adversarial-unified-task-workbench-v1"


def _wrapped(marker, body="", indent="     "):
    """Render a marker the way a TUI does: wrapped mid-token, left margin."""
    head, tail = marker[: len(marker) - 9], marker[len(marker) - 9 :]
    return f"{body}\n{indent}{head}\n{indent}{tail}\n"


def test_marker_literal_composes_prefix_and_task_id():
    assert marker_literal(TASK_ID) == f"HERDR_TASK_DONE:{TASK_ID}"
    assert (
        marker_literal(TASK_ID, BLOCKER_MARKER_PREFIX)
        == f"HERDR_TASK_BLOCKER:{TASK_ID}"
    )


def test_wrapped_marker_is_detected():
    """The regression: split mid-token, must still count as present."""
    screen = _wrapped(marker_literal(TASK_ID))
    assert marker_literal(TASK_ID) not in screen, "fixture must stay wrapped"
    assert marker_present(screen, TASK_ID) is True


def test_wrapped_marker_is_detected_for_every_prefix():
    for prefix in (
        DONE_MARKER_PREFIX,
        BLOCKER_MARKER_PREFIX,
        ORCH_MARKER_PREFIX,
    ):
        screen = _wrapped(marker_literal(TASK_ID, prefix))
        assert marker_present(screen, TASK_ID, prefix) is True, prefix


def test_real_opencode_screen_sample_is_detected():
    """Verbatim shape of the pane that actually stalled the workflow."""
    screen = (
        "净结论：ARCHIVE / ABNORMAL / OCR 三类已具备实施条\n"
        "     件；MATCH_CONFIRMATION 一类在返工前不具备可实施性。\n"
        "\n"
        f"     {DONE_MARKER_PREFIX}{TASK_ID[:35]}\n"
        f"     {TASK_ID[35:]}\n"
        "\n"
        "     Build · Space Bunny Free · 20m 35s\n"
    )
    assert marker_present(screen, TASK_ID) is True


def test_single_line_marker_still_detected():
    screen = f"  {marker_literal(TASK_ID)}  \n"
    assert marker_present(screen, TASK_ID) is True


def test_absent_marker_is_not_detected():
    assert marker_present("nothing to see here\n", TASK_ID) is False
    assert marker_present("", TASK_ID) is False
    assert marker_present(f"{marker_literal(TASK_ID)}\n", "") is False


def test_other_task_marker_does_not_match():
    """Cross-task evidence must never complete somebody else's task."""
    screen = _wrapped(marker_literal("plan-arch-unified-task-workbench-v1"))
    assert marker_present(screen, TASK_ID) is False
    assert marker_present(screen, "plan-arch-unified-task-workbench-v1") is True


def test_longer_identifier_does_not_satisfy_shorter_task_id():
    """``...-v1`` must not match a pane showing ``...-v1b``."""
    longer = f"{TASK_ID}b"
    for screen in (
        f"{marker_literal(longer)}\n",
        _wrapped(marker_literal(longer)),
    ):
        assert marker_present(screen, TASK_ID) is False, screen


def test_hard_break_does_not_splice_unrelated_text():
    """Fail closed: only indented continuations are soft wraps."""
    screen = (
        f"{DONE_MARKER_PREFIX}plan-arch-unified-\n"
        "task-workbench-v1\n"
    )
    assert marker_present(screen, TASK_ID) is False


def test_blank_line_does_not_splice_unrelated_text():
    screen = f"{DONE_MARKER_PREFIX}plan-arch-\n\nunified-task-workbench-v1\n"
    assert marker_present(screen, TASK_ID) is False


def test_whitespace_only_line_does_not_splice_unrelated_text():
    screen = f"{DONE_MARKER_PREFIX}plan-adversarial-\n     \nunified-task-workbench-v1\n"
    assert marker_present(screen, TASK_ID) is False


def test_neutral_token_never_matches_a_real_task():
    """Prompt sanitizer replaces real markers; the token must stay inert."""
    assert marker_present("HERDR_TASK_DONE:<TASK_ID>\n", TASK_ID) is False


def test_marker_instruction_text_does_not_match():
    """The dispatch prompt describes the marker without carrying the id."""
    screen = "output the prefix HERDR_TASK_DONE: followed immediately by this task id\n"
    assert marker_present(screen, TASK_ID) is False


def test_soft_wrap_regex_only_joins_indented_continuations():
    from herdr.completion import _SOFT_WRAP_RE

    assert _SOFT_WRAP_RE.sub("", "a\n     b\nc\nd\n") == "ab\nc\nd\n"
    # A whitespace-only line is a block separator, not a continuation.
    assert _SOFT_WRAP_RE.sub("", "a\n  \nb\n") == "a\n  \nb\n"


def test_detection_is_stable_against_repeated_polls():
    """Two polls of the same wrapped screen must agree (idempotent)."""
    screen = _wrapped(marker_literal(TASK_ID))
    assert [marker_present(screen, TASK_ID) for _ in range(3)] == [True] * 3


@pytest.mark.parametrize("bad", ["", None])
def test_empty_inputs_fail_closed(bad):
    assert marker_present(bad, TASK_ID) is False
    assert marker_present("screen", bad) is False


def test_no_horizontal_run_before_task_id_is_required():
    """The prefix may itself be split; only the id must land whole."""
    screen = f"{DONE_MARKER_PREFIX[:10]}\n     {DONE_MARKER_PREFIX[10:]}{TASK_ID}\n"
    assert marker_present(screen, TASK_ID) is True


def test_real_screen_roundtrip_for_stalled_task():
    """Guard the fixture against drifting away from the captured pane.

    The sibling task in the same node is the control: its marker is short
    enough to fit one line and always completed, this one is not.
    """
    sibling = "plan-arch-unified-task-workbench-v1"
    assert re.fullmatch(r"[0-9a-z-]+", TASK_ID)
    assert len(marker_literal(TASK_ID)) == 58
    assert len(marker_literal(sibling)) == 51
    assert len(marker_literal(TASK_ID)) - len(marker_literal(sibling)) == 7


# ---------------------------------------------------------------------------
# Call-site contract: both daemons must detect markers through the choke point.
# ---------------------------------------------------------------------------

DAEMONS = ("herdr-sentinel.py", "herdr-controller.py")


def _source(name):
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    return (root / "services" / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("daemon", DAEMONS)
def test_daemon_detects_markers_through_the_shared_helper(daemon):
    src = _source(daemon)
    assert "marker_present(" in src, f"{daemon} must use the shared marker helper"


@pytest.mark.parametrize("daemon", DAEMONS)
@pytest.mark.parametrize(
    "prefix", ("HERDR_TASK_DONE", "HERDR_TASK_BLOCKER", "HERDR_ORCH_TASK")
)
def test_daemon_has_no_raw_marker_substring_check(daemon, prefix):
    """A rebuilt ``f"{PREFIX}:{task_id}" in screen`` is exactly the defect.

    The source-level guard is the only one that survives a future re-inline of
    the marker, so the pure-layer tests alone are not enough.
    """
    src = _source(daemon)
    assert f'{prefix}:{{task_id}}' not in src, (
        f"{daemon} composes {prefix} inline; route it through marker_present()"
    )

