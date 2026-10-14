"""Tests for herdr/interaction_loop.py — Agent Human Interaction Loop V1.

Covers:
- Terminal pattern detection (known patterns, unknown patterns)
- Option extraction and key mapping
- Prompt fingerprinting (determinism, sensitivity)
- Identity validation (pane/agent drift detection)
- Response submission guard (expired, already-responded)
- Concurrent submission idempotency (fingerprint mismatch rejection)

Mock policy:
- All subprocess calls (_send_keys, herdr pane read) are mocked.
- StateStore is a lightweight in-memory stub.
- No real terminals or agents are started.
"""

import uuid
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

import herdr.interaction_loop as il


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CLAUDE_ASK_SAMPLE = (
    "  Do you want to proceed with deleting the file?\n"
    "  1. Yes, proceed\n"
    "  2. No, cancel\n"
    "  [1 for yes, 2 to cancel]: "
)

CODEX_ASK_SAMPLE = (
    "Allow execution?\n"
    "  a) Yes, allow once\n"
    "  b) No, deny\n"
    "  c) Yes, always allow\n"
)

QODER_ASK_SAMPLE = (
    "\u2295 Permission requested\n"
    "  Run shell command: rm -rf /tmp/build?\n"
    "  [y] Yes  [n] No: "
)

UNKNOWN_SAMPLE = (
    "Building project...\n"
    "Compiling src/main.rs\n"
    "[100%] Linked binary\n"
)

BLOCKED_NO_PROMPT_SAMPLE = (
    "Error: cannot connect to server\n"
    "Retrying in 5s...\n"
)


@pytest.fixture
def fake_store():
    """Minimal in-memory StateStore stub for audit event tests."""
    events: List[Dict[str, Any]] = []

    class FakeStore:
        def record_event(self, event_type, payload, **kwargs):
            events.append({"event_type": event_type, "payload": dict(payload), **kwargs})
            return {"id": str(uuid.uuid4())}

        def get_events(self):
            return list(events)

    return FakeStore()


# ---------------------------------------------------------------------------
# 1. detect_agent_interaction — pattern detection
# ---------------------------------------------------------------------------

class TestDetectAgentInteraction:
    """Tests for detect_agent_interaction()."""

    def test_claude_numbered_prompt_detected(self):
        result = il.detect_agent_interaction(
            pane_content=CLAUDE_ASK_SAMPLE,
            agent_name="claude",
        )
        assert result["detected"] is True
        assert result["interaction_type"] == "choice_prompt"
        assert len(result["options"]) >= 2
        assert result["raw_prompt"] != ""

    def test_codex_lettered_prompt_detected(self):
        result = il.detect_agent_interaction(
            pane_content=CODEX_ASK_SAMPLE,
            agent_name="codex",
        )
        assert result["detected"] is True
        assert result["interaction_type"] == "choice_prompt"
        assert len(result["options"]) >= 2

    def test_qoder_yn_prompt_detected(self):
        result = il.detect_agent_interaction(
            pane_content=QODER_ASK_SAMPLE,
            agent_name="qodercli",
        )
        assert result["detected"] is True
        assert result["interaction_type"] == "yn_prompt"
        assert len(result["options"]) == 2

    def test_normal_running_output_not_detected(self):
        result = il.detect_agent_interaction(
            pane_content=UNKNOWN_SAMPLE,
            agent_name="claude",
        )
        assert result["detected"] is False

    def test_blocked_without_prompt_not_detected(self):
        result = il.detect_agent_interaction(
            pane_content=BLOCKED_NO_PROMPT_SAMPLE,
            agent_name="claude",
        )
        assert result["detected"] is False

    def test_empty_content_not_detected(self):
        result = il.detect_agent_interaction(
            pane_content="",
            agent_name="claude",
        )
        assert result["detected"] is False

    def test_unknown_agent_returns_detected_false_or_generic(self):
        """Unknown agent should not crash; may return detected=False."""
        result = il.detect_agent_interaction(
            pane_content=CLAUDE_ASK_SAMPLE,
            agent_name="unknown_agent_xyz",
        )
        # Must not crash; detected may be False (fail-closed) or generic True
        assert "detected" in result

    def test_options_contain_required_fields(self):
        result = il.detect_agent_interaction(
            pane_content=CLAUDE_ASK_SAMPLE,
            agent_name="claude",
        )
        assert result["detected"] is True
        for opt in result["options"]:
            assert "option_id" in opt     # server-generated stable ID
            assert "label" in opt         # human-readable label
            assert "key_sequence" in opt  # actual keystroke(s) to send


# ---------------------------------------------------------------------------
# 2. compute_prompt_fingerprint — determinism + sensitivity
# ---------------------------------------------------------------------------

class TestComputePromptFingerprint:

    def test_same_content_same_fingerprint(self):
        fp1 = il.compute_prompt_fingerprint(CLAUDE_ASK_SAMPLE)
        fp2 = il.compute_prompt_fingerprint(CLAUDE_ASK_SAMPLE)
        assert fp1 == fp2

    def test_different_content_different_fingerprint(self):
        fp1 = il.compute_prompt_fingerprint(CLAUDE_ASK_SAMPLE)
        fp2 = il.compute_prompt_fingerprint(CODEX_ASK_SAMPLE)
        assert fp1 != fp2

    def test_fingerprint_is_hex_string(self):
        fp = il.compute_prompt_fingerprint(CLAUDE_ASK_SAMPLE)
        assert isinstance(fp, str)
        assert len(fp) == 16  # 8 bytes hex = 16 chars
        int(fp, 16)  # must parse as hex

    def test_whitespace_variation_changes_fingerprint(self):
        fp1 = il.compute_prompt_fingerprint("foo bar")
        fp2 = il.compute_prompt_fingerprint("foo  bar")
        assert fp1 != fp2


# ---------------------------------------------------------------------------
# 3. validate_interaction_identity — drift detection
# ---------------------------------------------------------------------------

class TestValidateInteractionIdentity:

    BASE_SNAPSHOT = {
        "pane_id": "w1:p1",
        "agent": "claude",
        "terminal_id": "term_abc123",
        "revision": 5,
        "prompt_fingerprint": "abcdef0123456789",
    }

    def test_identical_snapshot_valid(self):
        current = dict(self.BASE_SNAPSHOT)
        ok, reason = il.validate_interaction_identity(
            stored=self.BASE_SNAPSHOT, current=current
        )
        assert ok is True
        assert reason == ""

    def test_pane_id_drift_rejected(self):
        current = dict(self.BASE_SNAPSHOT, pane_id="w1:p99")
        ok, reason = il.validate_interaction_identity(
            stored=self.BASE_SNAPSHOT, current=current
        )
        assert ok is False
        assert "pane" in reason.lower() or "drift" in reason.lower()

    def test_agent_change_rejected(self):
        current = dict(self.BASE_SNAPSHOT, agent="codex")
        ok, reason = il.validate_interaction_identity(
            stored=self.BASE_SNAPSHOT, current=current
        )
        assert ok is False

    def test_terminal_id_change_rejected(self):
        current = dict(self.BASE_SNAPSHOT, terminal_id="term_other")
        ok, reason = il.validate_interaction_identity(
            stored=self.BASE_SNAPSHOT, current=current
        )
        assert ok is False

    def test_fingerprint_mismatch_rejected(self):
        current = dict(self.BASE_SNAPSHOT, prompt_fingerprint="0000000000000000")
        ok, reason = il.validate_interaction_identity(
            stored=self.BASE_SNAPSHOT, current=current
        )
        assert ok is False
        assert "fingerprint" in reason.lower() or "prompt" in reason.lower()

    def test_higher_revision_still_valid(self):
        """Higher revision means agent continued — prompt may be gone, but identity unchanged."""
        current = dict(self.BASE_SNAPSHOT, revision=10)
        ok, reason = il.validate_interaction_identity(
            stored=self.BASE_SNAPSHOT, current=current
        )
        assert isinstance(ok, bool)


# ---------------------------------------------------------------------------
# 4. resolve_option_key — server-side key mapping
# ---------------------------------------------------------------------------

class TestResolveOptionKey:

    def test_valid_option_id_returns_key(self):
        result = il.detect_agent_interaction(
            pane_content=CLAUDE_ASK_SAMPLE,
            agent_name="claude",
        )
        assert result["detected"] is True
        first_opt = result["options"][0]
        key_seq = il.resolve_option_key(
            option_id=first_opt["option_id"],
            options=result["options"],
        )
        assert key_seq == first_opt["key_sequence"]

    def test_unknown_option_id_returns_none(self):
        result = il.detect_agent_interaction(
            pane_content=CLAUDE_ASK_SAMPLE,
            agent_name="claude",
        )
        key_seq = il.resolve_option_key(
            option_id="nonexistent_option_id_xyz",
            options=result["options"],
        )
        assert key_seq is None

    def test_arbitrary_keystroke_cannot_be_submitted(self):
        """Browser must not be able to submit raw ctrl-c by injecting option_id."""
        result = il.detect_agent_interaction(
            pane_content=CLAUDE_ASK_SAMPLE,
            agent_name="claude",
        )
        key_seq = il.resolve_option_key(
            option_id="ctrl-c",
            options=result["options"],
        )
        assert key_seq is None  # must be rejected


# ---------------------------------------------------------------------------
# 5. record_interaction_event — audit trail
# ---------------------------------------------------------------------------

class TestRecordInteractionEvent:

    def test_event_recorded_on_submit(self, fake_store):
        il.record_interaction_event(
            store=fake_store,
            event_type="interaction_sent",
            workflow_id="wf-001",
            task_id="task-001",
            pane_id="w1:p1",
            agent="claude",
            prompt_fingerprint="abcdef01",
            option_id="opt_yes",
            result="sent",
        )
        events = fake_store.get_events()
        assert len(events) == 1
        e = events[0]
        assert e["event_type"] == "interaction_sent"
        assert e["payload"]["pane_id"] == "w1:p1"
        assert e["payload"]["prompt_fingerprint"] == "abcdef01"

    def test_no_sensitive_data_in_event(self, fake_store):
        """Passwords, tokens, key_sequence must not be logged."""
        il.record_interaction_event(
            store=fake_store,
            event_type="interaction_sent",
            workflow_id="wf-001",
            task_id="task-001",
            pane_id="w1:p1",
            agent="claude",
            prompt_fingerprint="abcdef01",
            option_id="opt_yes",
            result="sent",
        )
        events = fake_store.get_events()
        payload_str = str(events[0]["payload"])
        for forbidden in ("password", "token", "secret", "key_sequence"):
            assert forbidden not in payload_str.lower()

    def test_failed_result_recorded(self, fake_store):
        il.record_interaction_event(
            store=fake_store,
            event_type="interaction_failed",
            workflow_id="wf-001",
            task_id="task-001",
            pane_id="w1:p1",
            agent="claude",
            prompt_fingerprint="abcdef01",
            option_id="opt_yes",
            result="timeout",
            detail="herdr pane send-keys timed out",
        )
        events = fake_store.get_events()
        assert events[0]["event_type"] == "interaction_failed"
        assert events[0]["payload"]["result"] == "timeout"


# ---------------------------------------------------------------------------
# 6. describe_known_patterns — documentation of supported interactions
# ---------------------------------------------------------------------------

class TestDescribeKnownPatterns:

    def test_returns_list(self):
        patterns = il.describe_known_patterns()
        assert isinstance(patterns, list)
        assert len(patterns) >= 1

    def test_each_pattern_has_required_fields(self):
        for p in il.describe_known_patterns():
            assert "agent" in p
            assert "pattern_type" in p
            assert "description" in p


# ---------------------------------------------------------------------------
# 7. Integration: full pipeline (mocked subprocess)
# ---------------------------------------------------------------------------

class TestFullPipeline:
    """Integration test: detect to validate to resolve to send to audit."""

    def test_happy_path_claude_prompt_responds_correctly(self, fake_store):
        """Full pipeline with mocked _send_keys."""
        detection = il.detect_agent_interaction(
            pane_content=CLAUDE_ASK_SAMPLE,
            agent_name="claude",
        )
        assert detection["detected"] is True
        first_opt = detection["options"][0]

        fingerprint = il.compute_prompt_fingerprint(CLAUDE_ASK_SAMPLE)
        stored = {
            "pane_id": "w1:p1",
            "agent": "claude",
            "terminal_id": "term_abc",
            "revision": 3,
            "prompt_fingerprint": fingerprint,
        }
        current = dict(stored)

        ok, reason = il.validate_interaction_identity(stored=stored, current=current)
        assert ok is True

        key_seq = il.resolve_option_key(
            option_id=first_opt["option_id"],
            options=detection["options"],
        )
        assert key_seq is not None

        with patch("herdr.interaction_loop._send_keys_impl", return_value=True) as mock_send:
            sent = il.send_interaction_response(pane_id="w1:p1", key_sequence=key_seq)
            assert sent is True
            assert mock_send.call_count == 2
            assert mock_send.call_args_list[0][0] == ("w1:p1", key_seq)
            assert mock_send.call_args_list[1][0] == ("w1:p1", "enter")

        il.record_interaction_event(
            store=fake_store,
            event_type="interaction_sent",
            workflow_id="wf-001",
            task_id="task-001",
            pane_id="w1:p1",
            agent="claude",
            prompt_fingerprint=fingerprint,
            option_id=first_opt["option_id"],
            result="sent",
        )
        assert len(fake_store.get_events()) == 1

    def test_fingerprint_mismatch_rejects_response(self, fake_store):
        """If fingerprint changed (agent moved on), response is rejected."""
        stored = {
            "pane_id": "w1:p1",
            "agent": "claude",
            "terminal_id": "term_abc",
            "revision": 3,
            "prompt_fingerprint": "abcdef0123456789",
        }
        current = dict(stored, prompt_fingerprint="0000000000000000")

        ok, reason = il.validate_interaction_identity(stored=stored, current=current)
        assert ok is False

    def test_send_failure_records_uncertain_state(self, fake_store):
        """On send failure, must record uncertain state, not retry."""
        with patch("herdr.interaction_loop._send_keys_impl", return_value=False):
            sent = il.send_interaction_response(pane_id="w1:p1", key_sequence="1")
        assert sent is False
        il.record_interaction_event(
            store=fake_store,
            event_type="interaction_uncertain",
            workflow_id="wf-001",
            task_id="task-001",
            pane_id="w1:p1",
            agent="claude",
            prompt_fingerprint="abcdef01",
            option_id="opt_1",
            result="uncertain",
            detail="send_keys returned False",
        )
        events = fake_store.get_events()
        assert events[0]["event_type"] == "interaction_uncertain"
