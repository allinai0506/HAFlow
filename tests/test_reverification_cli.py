"""Selective Reverification CLI tests (HAFlow PR #108).

Drives the real `bin/herdr-task reverification` subcommands against a real
temporary state DB, so "why did test not re-run?" is answerable from a command,
not only from an in-process function.
"""
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr import reverification as rv  # noqa: E402
from herdr import scheduler_facts as facts  # noqa: E402


def _load_module(name, path):
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, str(path)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_task_cli = _load_module(
    "herdr_task_reverification_cli_test", HERDR_ROOT / "bin" / "herdr-task")

WF = "wf-rever-cli"


class _Parsed(Exception):
    """Carries the namespace main() built before dispatching to the command."""

    def __init__(self, namespace):
        # Deliberately not `self.args`: BaseException.args is a special
        # attribute that must be a tuple, and assigning a Namespace to it
        # raises "not iterable".
        super().__init__("parsed")
        self.namespace = namespace


def _parse_reverification_args(argv):
    """Build the real parser by driving main(), then intercept the dispatch.

    ``main()`` constructs its parser inline, so there is no reusable factory to
    call. Duplicating the argument definitions in the test would let the CLI and
    the test drift apart silently — a renamed flag would keep passing. So the
    real ``main()`` runs with ``sys.argv`` pointed at the subcommand and the
    command function replaced by a capture hook.
    """
    original = _task_cli.cmd_reverification

    def capture(args):
        raise _Parsed(args)

    _task_cli.cmd_reverification = capture
    original_argv = sys.argv
    sys.argv = ["herdr-task"] + list(argv)
    try:
        _task_cli.main()
    except _Parsed as parsed:
        return parsed.namespace
    finally:
        sys.argv = original_argv
        _task_cli.cmd_reverification = original
    raise AssertionError("herdr-task main() did not dispatch reverification")


class ReverificationCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-rever-cli-")
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "state.db"

        # Env and the get_state_store singleton are process-global. Restoring
        # both is not optional housekeeping: leaving a stale singleton behind
        # makes every later test read another test's temp database, which shows
        # up as unrelated failures far from this file.
        import herdr.state_store as state_store

        for key, value in (
            ("HERDR_STATE_DB", os.environ.get("HERDR_STATE_DB")),
            ("HERDR_WORKFLOW_DOCS_DIR",
             os.environ.get("HERDR_WORKFLOW_DOCS_DIR")),
        ):
            if value is None:
                self.addCleanup(os.environ.pop, key, None)
            else:
                self.addCleanup(os.environ.__setitem__, key, value)
        os.environ["HERDR_STATE_DB"] = str(self.db)
        os.environ["HERDR_WORKFLOW_DOCS_DIR"] = str(
            Path(self.tmp.name) / "docs")
        self.addCleanup(state_store.reset_state_store)

        store = state_store.get_state_store(self.db)
        store.save_workflow({"workflow_id": WF, "status": "running"})
        self.record("test", "reuse", "a" * 40, "b" * 40,
                    source_task_id=f"{WF}-test-auto",
                    reusable_scope=["docs/**/*.md"])
        self.record("review", "rerun", "a" * 40, "b" * 40,
                    reason=rv.REASON_RERUN_NO_SCOPE)
        # `status` is scoped to the frozen candidate, so the candidate has to be
        # frozen for the A -> B decisions to be the current ones.
        facts.record_candidate_frozen(WF, "a" * 40, db_path=self.db)
        facts.record_candidate_frozen(WF, "b" * 40, db_path=self.db)

    def record(self, verifier, decision, from_sha, to_sha, **extra):
        payload = {
            "decision": decision, "verifier": verifier,
            "from_candidate_sha": from_sha, "to_candidate_sha": to_sha,
            "changed_paths": ["docs/user-guide.md"],
            "reason": rv.REASON_REUSE_NON_IMPACT,
            "source_task_id": f"{WF}-{verifier}-auto",
            "source_verdict": "pass",
            "source_verified_candidate_sha": from_sha,
            "policy_version": rv.POLICY_VERSION,
        }
        payload.update(extra)
        facts.record_reverification_decision(WF, payload, db_path=self.db)

    def run_cli(self, *argv):
        # `main()` owns the only parser in this script, so the test builds the
        # real one by driving main() with --help-style interception rather than
        # duplicating the subparser definitions here.
        args = _parse_reverification_args(list(argv))
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _task_cli.cmd_reverification(args)
        return buffer.getvalue()

    def test_status_json_reports_metrics_and_reason(self):
        out = self.run_cli("reverification", "status",
                           "--workflow-id", WF, "--json")
        payload = json.loads(out)
        self.assertEqual(payload["workflow_id"], WF)
        self.assertEqual(payload["metrics"], {
            "total_verifiers": 2, "rerun_count": 1, "reuse_count": 1,
            "reuse_rate": 0.5,
        })
        by_verifier = {d["verifier"]: d for d in payload["decisions"]}
        self.assertEqual(by_verifier["test"]["decision"], "reuse")
        self.assertEqual(by_verifier["review"]["decision"], "rerun")

    def test_status_text_explains_the_skipped_verifier(self):
        out = self.run_cli("reverification", "status", "--workflow-id", WF)
        self.assertIn("test: reuse", out)
        self.assertIn("review: rerun", out)
        self.assertIn(rv.REASON_REUSE_NON_IMPACT, out)
        self.assertIn("docs/user-guide.md", out)
        self.assertIn(f"{WF}-test-auto", out)
        self.assertIn("verdict=pass", out)
        self.assertIn(rv.POLICY_VERSION, out)

    def test_status_is_scoped_to_the_current_candidate(self):
        """§32: `status` answers about the current candidate, not history.

        Reporting the newest decision per verifier regardless of candidate would
        silently answer a different question than the command claims: after a
        rotation the previous episode's decision would still be shown.
        """
        facts.record_candidate_frozen(WF, "c" * 40, db_path=self.db)
        out = self.run_cli("reverification", "status",
                           "--workflow-id", WF, "--json")
        payload = json.loads(out)
        self.assertEqual(payload["candidate_sha"], "c" * 40)
        self.assertEqual(payload["decisions"], [])
        self.assertEqual(payload["metrics"]["total_verifiers"], 0)

    def test_status_prints_the_authorising_scope(self):
        out = self.run_cli("reverification", "status", "--workflow-id", WF)
        self.assertIn("scope: docs/**/*.md", out)

    def test_history_json_lists_every_episode(self):
        self.record("test", "rerun", "b" * 40, "c" * 40,
                    reason=rv.REASON_RERUN_OUTSIDE_SCOPE)
        out = self.run_cli("reverification", "history",
                           "--workflow-id", WF, "--json")
        rows = json.loads(out)
        self.assertEqual(len(rows), 3)
        self.assertEqual({(r["from_candidate_sha"], r["to_candidate_sha"])
                          for r in rows},
                         {("a" * 40, "b" * 40), ("b" * 40, "c" * 40)})

    def test_history_is_bounded_by_limit(self):
        for i in range(5):
            self.record("test", "rerun", f"{i}" * 40, f"{i + 1}" * 40)
        out = self.run_cli("reverification", "history",
                           "--workflow-id", WF, "--limit", "2", "--json")
        rows = json.loads(out)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["to_candidate_sha"], "5" * 40,
                         "the limit must return the newest entries, in order")

    def test_unknown_workflow_is_not_an_error(self):
        out = self.run_cli("reverification", "history",
                           "--workflow-id", "wf-unknown", "--json")
        self.assertEqual(json.loads(out), [])

    def test_status_of_unknown_workflow_reports_zeroes(self):
        out = self.run_cli("reverification", "status",
                           "--workflow-id", "wf-unknown", "--json")
        payload = json.loads(out)
        self.assertEqual(payload["metrics"]["total_verifiers"], 0)
        self.assertEqual(payload["metrics"]["reuse_rate"], 0.0)

    def test_invalid_limit_is_rejected(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("reverification", "history",
                         "--workflow-id", WF, "--limit", "0")
        self.assertEqual(ctx.exception.code, 2)

    def test_cli_is_wired_into_the_argument_parser(self):
        """The subcommand must be reachable from the real entry point."""
        result = subprocess.run(
            [sys.executable, str(HERDR_ROOT / "bin" / "herdr-task"),
             "reverification", "--help"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status", result.stdout)
        self.assertIn("history", result.stdout)


if __name__ == "__main__":
    unittest.main()
