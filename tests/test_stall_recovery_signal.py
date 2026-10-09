"""Stall diagnostics must stay actionable and must not drown real alerts.

Regression context (2026-10-09, wf-nexusarchive-1009-02-requirements-challenger):

A ``receipt-v1`` task printed ``HERDR_TASK_DONE`` and went idle, but the
screen-marker completion path is *structurally impossible* for that protocol
(``process_completion_observation`` returns early, so the Controller never
consumes the sample).  Two defects turned that design boundary into a
69-minute silent deadlock:

1. Sentinel re-printed ``[SENTINEL COMPLETION READY] ... Controller CAS pending``
   every poll for a protocol where the CAS path can never succeed — 452 lines of
   noise that buried the real ``[SENTINEL STALL]`` alert.
2. The stall alert carried no recovery command, so an operator who *did* see it
   had nothing actionable to run (contrast ``Controller._notify_blocked_human_upgrade``
   which emits copy-paste commands).
"""

import contextlib
import importlib
import io
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def _load_sentinel():
    return importlib.import_module("services.herdr-sentinel")


class ReceiptV1CompletionSpamTest(unittest.TestCase):
    """Defect 1: a protocol-impossible CAS path must not be re-announced."""

    def _sweep(self, *, protocol, agent_state="idle"):
        sentinel = _load_sentinel()
        task = {
            "task_id": "t-receipt",
            "workflow_id": "wf-1",
            "status": "working",
            "pane_id": "pane-1",
            "node": "requirements",
            "completion_protocol": protocol,
            "version": 5,
        }
        store = Mock()
        store.observe_completion.return_value = {
            "ready": True,
            "observed_version": 5,
            "consecutive_samples": 231,
        }
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), \
             patch.object(sentinel, "pane_visible",
                          return_value="HERDR_TASK_DONE:t-receipt"), \
             patch.object(sentinel, "agent_status", return_value=agent_state), \
             patch.object(sentinel, "_record_sentinel_event"):
            sentinel.observe_completion_sample(task, store, now=1000.0)
        return stream.getvalue()

    def test_receipt_v1_marker_present_does_not_print_completion_ready(self):
        out = self._sweep(protocol="receipt-v1")

        self.assertNotIn(
            "COMPLETION READY",
            out,
            "receipt-v1 tasks can never complete via the screen-marker CAS path; "
            "re-announcing it every poll buries the real stall alert",
        )

    def test_legacy_fact_protocol_still_prints_completion_ready(self):
        """The screen-marker path remains valid for legacy FACT tasks."""
        out = self._sweep(protocol=None)

        self.assertIn(
            "COMPLETION READY",
            out,
            "suppressing the announcement for legacy FACT tasks would hide a "
            "legitimate pending Controller CAS",
        )

    def test_receipt_v1_names_the_receipt_channel_once(self):
        """Instead of spam, point at the channel that can actually settle it."""
        sentinel = _load_sentinel()
        self.assertTrue(
            hasattr(sentinel, "receipt_completion_hint"),
            "expected a helper that names the receipt-v1 recovery channel",
        )
        hint = sentinel.receipt_completion_hint(
            {"task_id": "t-1", "completion_identity_path": "/tmp/c/receipt-a.json"}
        )
        self.assertIn("report-completion", hint)
        self.assertIn("t-1", hint)


class StallAlertActionabilityTest(unittest.TestCase):
    """Defect 2: a stall alert must carry a runnable recovery command."""

    def test_stall_alert_includes_receipt_command_for_receipt_v1(self):
        sentinel = _load_sentinel()
        task = {
            "task_id": "t-1",
            "workflow_id": "wf-1",
            "status": "working",
            "completion_protocol": "receipt-v1",
            "completion_identity_path": "/tmp/completion-credentials/receipt-abc.json",
        }
        body = sentinel.stall_alert_body(task, idle_seconds=1800)

        self.assertIn("report-completion", body)
        self.assertIn("t-1", body)
        self.assertIn("receipt-abc.json", body)


class TerminalSampleReapTest(unittest.TestCase):
    """Defect 5: terminal tasks must not keep stale completion samples.

    ``reap_terminal_completion_samples`` receives the module object under
    test injected via ``db_module`` so tests never touch the real database
    layer; production passes the real ``herdr.state_db``.
    """

    def test_terminal_tasks_have_their_completion_samples_reaped(self):
        sentinel = _load_sentinel()
        store = Mock()
        tasks = [
            {"task_id": "t-terminal", "status": "cleaned"},
            {"task_id": "t-active", "status": "working"},
        ]
        cleared = []

        class FakeStateDB:
            @staticmethod
            def clear_completion_observation(task_id):
                cleared.append(task_id)
                return task_id == "t-terminal"

        removed = sentinel.reap_terminal_completion_samples(
            tasks, store, db_module=FakeStateDB)

        self.assertEqual(removed, 1)
        self.assertEqual(cleared, ["t-terminal"])

    def test_reap_only_touches_terminal_tasks(self):
        sentinel = _load_sentinel()
        store = Mock()
        tasks = [{"task_id": "t-live", "status": "working"}]

        class FakeStateDB:
            @staticmethod
            def clear_completion_observation(task_id):
                raise AssertionError("must not touch live tasks")

        removed = sentinel.reap_terminal_completion_samples(
            tasks, store, db_module=FakeStateDB)

        self.assertEqual(removed, 0)

    def test_reap_survives_a_failing_clear(self):
        sentinel = _load_sentinel()
        store = Mock()
        tasks = [{"task_id": "t-terminal", "status": "failed"}]

        class FailingStateDB:
            @staticmethod
            def clear_completion_observation(task_id):
                raise OSError("db gone")

        removed = sentinel.reap_terminal_completion_samples(
            tasks, store, db_module=FailingStateDB)

        self.assertEqual(removed, 0)


    def test_stall_alert_for_legacy_task_offers_supersede(self):
        sentinel = _load_sentinel()
        task = {
            "task_id": "t-2",
            "workflow_id": "wf-1",
            "status": "working",
            "completion_protocol": None,
        }
        body = sentinel.stall_alert_body(task, idle_seconds=1800)

        self.assertIn("supersede", body)
        self.assertNotIn("report-completion", body)

    def test_stalled_receipt_v1_task_escalates_with_actionable_body(self):
        sentinel = _load_sentinel()
        tasks = [{
            "task_id": "t-3",
            "workflow_id": "wf-1",
            "status": "working",
            "completion_protocol": "receipt-v1",
            "completion_identity_path": "/tmp/creds/receipt-x.json",
            "updated_at": 0.0,
        }]
        notify = Mock(return_value=True)
        with patch("herdr.liveness.evaluate_task_stalls",
                   return_value=([{"task_id": "t-3", "workflow_id": "wf-1",
                                   "status": "working", "idle_seconds": 1800,
                                   "updated_at": 0.0}], {"t-3": {}})), \
             patch.object(sentinel, "notify_stall"):
            sentinel.check_task_stalls(tasks, {}, now=1800.0, notify_fn=notify)

        self.assertTrue(
            notify.called,
            "a stalled receipt-v1 task must escalate on the dedicated human channel",
        )
        self.assertIn("report-completion", notify.call_args[0][2])


class EmittedCommandContractTest(unittest.TestCase):
    """The commands we print must actually run when pasted.

    A notification that hands the operator a stale subcommand or an
    incomplete argument contract is worse than no notification: it looks
    like a fix and fails on paste.  These tests execute the emitted
    ``report-completion`` and ``supersede`` commands for real against an
    isolated store (same pattern as
    ``tests/test_blocked_recovery_command_contract.py``), and fall back to
    an argparse-signature check only for commands a fixture cannot complete
    (``renew-completion`` needs a caller-supplied operation id,
    ``close-workflow`` would tear the fixture down).
    """

    TASK_ID = "wf-x-requirements-challenger"
    WORKFLOW_ID = "wf-x"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="herdr-cmd-contract-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env = dict(os.environ)
        self.env.update({
            "TASKS_FILE": str(self.root / "tasks.json"),
            "WORKFLOWS_FILE": str(self.root / "workflows.json"),
            "HERDR_STATE_DB": str(self.root / "state.db"),
        })

        from herdr.completion_receipt import issue_completion_contract
        from herdr.state_store import SQLiteStateStore, reset_state_store

        reset_state_store()
        self.store = SQLiteStateStore(Path(self.env["HERDR_STATE_DB"]))
        self.store.save_workflow({"workflow_id": self.WORKFLOW_ID, "status": "running"})
        self.store.save_task({
            "task_id": self.TASK_ID,
            "workflow_id": self.WORKFLOW_ID,
            "run_id": "run-cmd-contract",
            "status": "working",
            "node": "requirements",
            "stage": "requirements",
            "agent": "opencode",
            "dispatch_role": "challenger",
            "integration_mode": "none",
            "started_at": time.time() - 7200.0,
        })
        contract = issue_completion_contract(self.TASK_ID, self.store)
        self.identity_path = contract["path"]
        self.addCleanup(reset_state_store)

    def _body_for(self, protocol):
        sentinel = _load_sentinel()
        return sentinel.stall_alert_body(
            {
                "task_id": self.TASK_ID,
                "workflow_id": self.WORKFLOW_ID,
                "status": "working",
                "completion_protocol": protocol,
                "completion_identity_path": self.identity_path,
            },
            idle_seconds=1800,
        )

    def _emitted_commands(self, body):
        commands = []
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("herdr-task "):
                commands.append(shlex.split(stripped.split("#", 1)[0]))
        return commands

    def _run(self, tokens):
        # tokens[0] is the printed "herdr-task" name; the real executable is
        # passed separately, so only the subcommand and arguments follow.
        return subprocess.run(
            [sys.executable, str(ROOT / "bin" / "herdr-task"), *tokens[1:]],
            cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=60,
        )

    def _find(self, commands, verb):
        for tokens in commands:
            if verb in tokens:
                return tokens
        return None

    def test_emitted_report_completion_actually_settles_the_task(self):
        """The primary recovery command must work end to end, as printed."""
        tokens = self._find(self._emitted_commands(self._body_for("receipt-v1")),
                            "report-completion")
        self.assertIsNotNone(tokens, "receipt-v1 body must emit report-completion")

        result = self._run(tokens)

        self.assertEqual(
            result.returncode, 0,
            f"emitted command does not run: {' '.join(tokens)}\n"
            f"{result.stdout}\n{result.stderr}",
        )
        self.assertIn(self.TASK_ID, result.stdout)

    def test_emitted_supersede_actually_abandons_the_task(self):
        """`supersede --reason` alone is rejected; the printed form must not be."""
        for protocol in ("receipt-v1", None):
            tokens = self._find(self._emitted_commands(self._body_for(protocol)),
                                "supersede")
            self.assertIsNotNone(tokens, f"body for {protocol} must emit supersede")

            result = self._run(tokens)

            with self.subTest(protocol=protocol):
                self.assertEqual(
                    result.returncode, 0,
                    f"emitted command does not run: {' '.join(tokens)}\n"
                    f"{result.stdout}\n{result.stderr}",
                )
                self.assertNotIn("[SUPERSEDE REJECTED]", result.stdout)

    def test_no_emitted_command_is_rejected_by_argparse(self):
        """Covers commands a fixture cannot complete (renew, close)."""
        for protocol in ("receipt-v1", None):
            for tokens in self._emitted_commands(self._body_for(protocol)):
                with self.subTest(protocol=protocol, cmd=" ".join(tokens)):
                    result = self._run(tokens)
                    self.assertNotIn(
                        "herdr-task: error:",
                        result.stderr,
                        f"stale CLI syntax emitted to operator: {' '.join(tokens)}\n"
                        f"{result.stderr}",
                    )

    def test_no_emitted_reason_is_a_bare_placeholder(self):
        """A literal ``...`` would be pasted verbatim and land in the audit log."""
        for protocol in ("receipt-v1", None):
            tokens = self._find(self._emitted_commands(self._body_for(protocol)),
                                "supersede")
            self.assertIsNotNone(tokens)
            reason = tokens[tokens.index("--reason") + 1]
            self.assertNotEqual(reason.strip(), "")
            self.assertNotEqual(
                reason.strip().strip('"').strip("'"), "...",
                "supersede --reason needs a concrete reason token",
            )


class ControllerUpgradeCommandContractTest(unittest.TestCase):
    """The Controller's human-upgrade card must emit runnable commands too.

    It printed ``herdr-task supersede <id> --reason ...``, which the CLI
    rejects (``supersede`` needs ``--by`` or ``--abandon``), so the one
    escape hatch offered to a human at 3am failed on paste — the same defect
    class as the stall alert.  Executed against a real isolated store.
    """

    TASK_ID = "wf-cmd-ctl-challenger"
    WORKFLOW_ID = "wf-cmd-ctl"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="herdr-ctl-cmd-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env = dict(os.environ)
        self.env.update({
            "TASKS_FILE": str(self.root / "tasks.json"),
            "WORKFLOWS_FILE": str(self.root / "workflows.json"),
            "HERDR_STATE_DB": str(self.root / "state.db"),
        })
        from herdr.state_store import SQLiteStateStore, reset_state_store

        reset_state_store()
        self.store = SQLiteStateStore(Path(self.env["HERDR_STATE_DB"]))
        self.store.save_workflow({"workflow_id": self.WORKFLOW_ID, "status": "running"})
        self.store.save_task({
            "task_id": self.TASK_ID,
            "workflow_id": self.WORKFLOW_ID,
            "status": "blocked",
            "node": "implementation",
            "stage": "implementation",
            "agent": "opencode",
        })
        self.addCleanup(reset_state_store)

    def _upgrade_card_body(self):
        import importlib as _il
        from unittest.mock import Mock

        controller = _il.import_module("services.herdr-controller")
        captured = {}
        notifier = Mock()
        notifier.notify_human_upgrade = lambda tid, wid, body, url=None: (
            captured.setdefault("body", body) or True
        )
        notifier.build_console_url = lambda **kw: "http://localhost/test"
        original = _il.import_module

        def controlled(name, *args, **kwargs):
            if name == "services.herdr-notifier":
                return notifier
            return original(name, *args, **kwargs)

        with patch.object(_il, "import_module", controlled):
            controller._notify_blocked_human_upgrade(
                self.store.get_task(self.TASK_ID), "episode-test", 1800
            )
        return captured["body"]

    def _run(self, tokens):
        return subprocess.run(
            [sys.executable, str(ROOT / "bin" / "herdr-task"), *tokens[1:]],
            cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=60,
        )

    def test_controller_supersede_command_actually_runs(self):
        body = self._upgrade_card_body()
        supersede = None
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("herdr-task supersede"):
                supersede = shlex.split(stripped.split("#", 1)[0])
        self.assertIsNotNone(
            supersede, f"upgrade card must offer a supersede command:\n{body}"
        )

        result = self._run(supersede)

        self.assertEqual(
            result.returncode, 0,
            f"emitted command does not run: {' '.join(supersede)}\n"
            f"{result.stdout}\n{result.stderr}",
        )
        self.assertNotIn("[SUPERSEDE REJECTED]", result.stdout)


if __name__ == "__main__":
    unittest.main()
