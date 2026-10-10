"""`herdr-task halt` must not report success when the interrupt actually failed.

Regression context (2026-10-09, wf-nexusarchive-1009-02): three `halt` commands
were issued against a stuck workflow.  `bin/herdr-task` printed `[HALTED]`
unconditionally — it never inspected `res["ok"]` — so every failed interrupt
looked like a successful one and the operator had no signal that the task was
still `working`.  `herdr/steering.halt_task` already returns an honest
`{"ok": False, "error": ...}` contract (its docstring explicitly forbids
transitioning the task on failure); only the CLI layer discarded it.
"""

import contextlib
import importlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load_cli(name="herdr_task_cli"):
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, str(ROOT / "bin" / "herdr-task"))
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_cli(module, *argv):
    """Invoke the CLI dispatch layer in-process; return (exit_code, output)."""
    stream = io.StringIO()
    old_argv = sys.argv
    sys.argv = ["herdr-task", *argv]
    try:
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            try:
                module.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
            else:
                code = 0
    finally:
        sys.argv = old_argv
    return code, stream.getvalue()


def _failed_halt():
    """The exact shape steering.halt_task returns when the interrupt fails."""
    return {
        "ok": False,
        "task_id": "stuck-task",
        "status": "working",
        "error": "interrupt_signal_failed",
        "reason": "human interrupt",
    }


class HaltFailureHonestyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cli = load_cli()

    def test_failed_halt_exits_nonzero_and_does_not_claim_halting(self):
        with patch("herdr.steering.halt_task", return_value=_failed_halt()):
            code, out = run_cli(self.cli, "halt", "stuck-task")

        self.assertNotEqual(
            code, 0,
            "a failed interrupt must not report exit 0",
        )
        self.assertNotIn(
            "[HALTED]",
            out,
            "claiming [HALTED] on a failed interrupt is what hid the real blocker",
        )

    def test_failed_halt_reports_the_underlying_error(self):
        with patch("herdr.steering.halt_task", return_value=_failed_halt()):
            _, out = run_cli(self.cli, "halt", "stuck-task")

        self.assertIn("interrupt_signal_failed", out)
        self.assertIn("[HALT FAILED]", out)

    def test_successful_halt_still_reports_halted(self):
        ok = {
            "ok": True,
            "task_id": "stuck-task",
            "status": "interrupted",
            "error": None,
            "reason": "human interrupt",
        }
        with patch("herdr.steering.halt_task", return_value=ok):
            code, out = run_cli(self.cli, "halt", "stuck-task")

        self.assertEqual(code, 0, out)
        self.assertIn("[HALTED]", out)


class HaltTaskContractTest(unittest.TestCase):
    """steering.halt_task itself must keep its own no-transition-on-failure rule."""

    def _load_steering(self):
        return importlib.import_module("herdr.steering")

    def test_failed_interrupt_does_not_transition_the_task(self):
        steering = self._load_steering()
        adapter = Mock()
        adapter.name = "opencode"
        adapter.protocol_level = "tty"
        adapter.supports_interrupt = True
        adapter.interrupt.return_value = False

        task = {"task_id": "stuck-task", "status": "working",
                "pane_id": "pane-1", "agent": "opencode",
                "workflow_id": "wf-1"}
        with patch.object(steering, "load_tasks_data",
                           return_value={"tasks": [task]}), \
             patch.object(steering, "get_agent_adapter", return_value=adapter), \
             patch.object(steering, "load_steering_data", return_value={}), \
             patch.object(steering, "save_steering_data"), \
             patch("herdr.kernel.transition_task") as transition:
            result = steering.halt_task("stuck-task")

        self.assertFalse(result["ok"])
        self.assertFalse(
            transition.called,
            "a failed interrupt must never move the task to 'interrupted'",
        )


if __name__ == "__main__":
    unittest.main()
