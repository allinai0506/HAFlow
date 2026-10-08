"""Tests for herdr/finding_verifier.py (Finding Verifier Safety V1).

Tests safety constraints:
1. Tightened 'verified' (runtime/data consequences demoted to 'uncertain').
2. Tightened 'rejected' (only same scope, same revision, reachable execution).
3. Adversarial test cases (unrelated scope, TYPE_CHECKING, local imports,
   different receivers, comments, methods in unrelated classes).
"""

import tempfile
import unittest
from pathlib import Path

from herdr.finding_verifier import verify_finding, verify_findings

HERDR_ROOT = Path(__file__).resolve().parent.parent


class FindingVerifierSafetyTestCase(unittest.TestCase):
    # -------------------------------------------------------------
    # 1. Historical Regressions (Confirmed FPs successfully rejected)
    # -------------------------------------------------------------
    def test_reject_variable_never_set_when_counter_evidence_in_same_scope(self):
        """PR #149: claiming args._launch_intent is never set in _launch_task must be rejected."""
        finding = {
            "producer": "agy",
            "file": "bin/herdr-task",
            "start_line": 3377,
            "end_line": 3404,
            "message": (
                "When handling non-isolation router rejection in `_launch_task`, `launch_intent` is looked up via "
                "`getattr(args, '_launch_intent', None)`. However, `_launch_task` obtains launch intent claims earlier in "
                "local variable `claim` and records intent in `claim['intent']`, never setting `args._launch_intent`. "
                "Consequently, `launch_intent` is always evaluated as None/falsy, so `abort_launch_intent` is never called."
            ),
        }
        res = verify_finding(finding, repo_dir=HERDR_ROOT, head_commit="42ab1b3")
        self.assertEqual(res["verification_status"], "rejected")
        self.assertIn("counter_evidence_found", res["verification_reason"])
        self.assertEqual(res["counter_evidence"]["type"], "assignment_exists")
        self.assertEqual(res["counter_evidence"]["target"], "args._launch_intent")

    def test_reject_function_not_defined_when_counter_evidence_at_top_level(self):
        """PR #152: claiming _record_router_failure_task is not defined must be rejected."""
        finding = {
            "producer": "agy",
            "file": "bin/herdr-task",
            "start_line": 3455,
            "end_line": 3456,
            "message": (
                "When catching an exception from the router, `_record_router_failure_task(args, node_id, exc)` is called. "
                "However, `_record_router_failure_task` is neither defined nor imported in `bin/herdr-task`."
            ),
        }
        res = verify_finding(finding, repo_dir=HERDR_ROOT, head_commit="0758bbb")
        self.assertEqual(res["verification_status"], "rejected")
        self.assertIn("counter_evidence_found", res["verification_reason"])
        self.assertEqual(res["counter_evidence"]["type"], "definition_exists")
        self.assertEqual(res["counter_evidence"]["symbol"], "_record_router_failure_task")
        self.assertEqual(res["counter_evidence"]["scope"], "module")

    def test_reject_module_not_imported_when_counter_evidence_at_top_level(self):
        """PR #152: claiming time/json are not imported must be rejected."""
        finding = {
            "producer": "agy",
            "file": "bin/herdr-task",
            "start_line": 7220,
            "end_line": 7233,
            "message": (
                "In `cmd_workflow_recovery`, `time` and `json` modules are referenced, and `sqlite3.Error` is caught, "
                "but `sqlite3`, `time`, and `json` are not ensured to be imported within scope or in the global namespace."
            ),
        }
        res = verify_finding(finding, repo_dir=HERDR_ROOT, head_commit="0758bbb")
        self.assertEqual(res["verification_status"], "rejected")
        self.assertIn("counter_evidence_found", res["verification_reason"])
        self.assertEqual(res["counter_evidence"]["type"], "import_exists")

    def test_reject_record_never_written_when_counter_evidence_in_active_call(self):
        """PR #160: claiming coordinator_stalled is never recorded must be rejected."""
        finding = {
            "producer": "agy",
            "file": "services/herdr-controller.py",
            "start_line": 6987,
            "end_line": 7000,
            "message": (
                "`intake_stalled` relies on `attention_get(intake_key)` having `attempts >= 2` and `reason == 'coordinator_stalled'`. "
                "However, nowhere in `herdr-controller.py` is an attention record with `coordinator_stalled` ever recorded."
            ),
        }
        res = verify_finding(finding, repo_dir=HERDR_ROOT, head_commit="cd6b95e")
        self.assertEqual(res["verification_status"], "rejected")
        self.assertIn("counter_evidence_found", res["verification_reason"])
        self.assertEqual(res["counter_evidence"]["type"], "record_exists")
        self.assertEqual(res["counter_evidence"]["literal"], "coordinator_stalled")

    # -------------------------------------------------------------
    # 2. Strict 'verified' -> Demote unproven runtime defects to 'uncertain'
    # -------------------------------------------------------------
    def test_demote_runtime_fingerprint_defect_to_uncertain_and_retain(self):
        """PR #158: Pattern match on sort key alone cannot prove runtime fingerprint drift, must be uncertain and retained."""
        finding = {
            "producer": "agy",
            "file": "herdr/fix_loop.py",
            "start_line": 66,
            "end_line": 69,
            "message": (
                "Ephemeral `task_id` is still used as the sort key (`key=lambda b: str(b.get('task_id') or '')`) when ordering blockers. "
                "When multiple blockers exist across task generations, differing task IDs change the sort order in `parts`, "
                "causing `verdict_fingerprint` to produce different hashes for semantically identical verdicts."
            ),
        }
        res = verify_finding(finding, repo_dir=HERDR_ROOT, head_commit="291c1f7")
        # Must be uncertain because static analysis cannot prove multiple blockers exist or hash drifts
        self.assertEqual(res["verification_status"], "uncertain")
        self.assertIn("unverifiable_statically", res["verification_reason"])

    def test_retain_subjective_timing_findings_as_uncertain(self):
        """PR #160: timing race condition without static counter-evidence is marked uncertain and retained."""
        finding = {
            "producer": "agy",
            "file": "herdr/projects.py",
            "start_line": 713,
            "end_line": 724,
            "message": (
                "In `ensure_coordinator_running`, if `_start_coordinator` fails or if the coordinator fails to come alive "
                "within the startup invocation, `_COORDINATOR_HEAL_ATTEMPTS[key]` has already been updated to `now`. "
                "The pane is locked out of retry attempts for the full 30 seconds even if the failure was transient."
            ),
        }
        res = verify_finding(finding, repo_dir=HERDR_ROOT, head_commit="cd6b95e")
        self.assertEqual(res["verification_status"], "uncertain")

    # -------------------------------------------------------------
    # 3. Basic Grounding (File and Line Boundary)
    # -------------------------------------------------------------
    def test_reject_nonexistent_file(self):
        finding = {
            "producer": "agy",
            "file": "non/existent/module_xyz.py",
            "start_line": 10,
            "end_line": 20,
            "message": "Syntax error in non-existent module",
        }
        res = verify_finding(finding, repo_dir=HERDR_ROOT)
        self.assertEqual(res["verification_status"], "rejected")
        self.assertIn("file_not_found", res["verification_reason"])

    def test_reject_line_out_of_bounds(self):
        finding = {
            "producer": "agy",
            "file": "herdr/fix_loop.py",
            "start_line": 99999,
            "end_line": 100005,
            "message": "Crash on line 99999",
        }
        res = verify_finding(finding, repo_dir=HERDR_ROOT)
        self.assertEqual(res["verification_status"], "rejected")
        self.assertIn("line_out_of_bounds", res["verification_reason"])

    # -------------------------------------------------------------
    # 4. Adversarial Safety Tests (Ensuring True Defects are NEVER Wrongly Rejected)
    # -------------------------------------------------------------
    def test_adversarial_same_variable_name_in_different_scope_not_rejected(self):
        """Variable assigned in an unrelated function must NOT reject finding for target function."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            sample_code = (
                "def unrelated_cleanup():\n"
                "    data = [1, 2, 3]  # Assigned here in unrelated function\n"
                "\n"
                "def process_items():\n"
                "    # data is never assigned in this function\n"
                "    return len(data)\n"
            )
            (tmp / "test_scope.py").write_text(sample_code, encoding="utf-8")
            finding = {
                "file": "test_scope.py",
                "start_line": 5,
                "end_line": 6,
                "message": "In `process_items`, `data` is never assigned before being used.",
            }
            res = verify_finding(finding, repo_dir=tmp)
            # MUST NOT be rejected! The assignment in unrelated_cleanup does not refute the defect in process_items!
            self.assertNotEqual(res["verification_status"], "rejected")
            self.assertEqual(res["verification_status"], "uncertain")

    def test_adversarial_unreachable_type_checking_assignment_not_rejected(self):
        """Assignment under `if TYPE_CHECKING:` is dead at runtime, must NOT reject finding."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            sample_code = (
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n"
                "    client = None  # Static typing stub only\n"
                "\n"
                "def run_request():\n"
                "    return client.connect()\n"
            )
            (tmp / "test_tc.py").write_text(sample_code, encoding="utf-8")
            finding = {
                "file": "test_tc.py",
                "start_line": 5,
                "end_line": 6,
                "message": "`client` is never initialized at runtime.",
            }
            res = verify_finding(finding, repo_dir=tmp)
            # MUST NOT be rejected!
            self.assertNotEqual(res["verification_status"], "rejected")
            self.assertEqual(res["verification_status"], "uncertain")

    def test_adversarial_local_import_in_unrelated_function_not_rejected(self):
        """Local import in unrelated function must NOT refute claim of missing import in target scope."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            sample_code = (
                "def other_worker():\n"
                "    import urllib.parse  # Local import only\n"
                "\n"
                "def handle_query():\n"
                "    # Uses urllib.parse without importing\n"
                "    return urllib.parse.quote('hello')\n"
            )
            (tmp / "test_import.py").write_text(sample_code, encoding="utf-8")
            finding = {
                "file": "test_import.py",
                "start_line": 5,
                "end_line": 6,
                "message": "In `handle_query`, module `urllib` is not imported in scope.",
            }
            res = verify_finding(finding, repo_dir=tmp)
            # MUST NOT be rejected!
            self.assertNotEqual(res["verification_status"], "rejected")
            self.assertEqual(res["verification_status"], "uncertain")

    def test_adversarial_guarded_try_except_import_not_rejected(self):
        """Guarded import in `try...except ImportError` must NOT unconditionally refute missing import claim."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            sample_code = (
                "try:\n"
                "    import optional_accelerator\n"
                "except ImportError:\n"
                "    optional_accelerator = None\n"
                "\n"
                "def compute():\n"
                "    return optional_accelerator.fast_run()\n"
            )
            (tmp / "test_guarded.py").write_text(sample_code, encoding="utf-8")
            finding = {
                "file": "test_guarded.py",
                "start_line": 6,
                "end_line": 7,
                "message": "`optional_accelerator` is not ensured to be imported or available.",
            }
            res = verify_finding(finding, repo_dir=tmp)
            # MUST NOT be rejected!
            self.assertNotEqual(res["verification_status"], "rejected")
            self.assertEqual(res["verification_status"], "uncertain")

    def test_adversarial_different_attribute_receiver_not_rejected(self):
        """Assignment to `other_obj.intent` must NOT reject finding claiming `args.intent` is never set."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            sample_code = (
                "def launch(args):\n"
                "    other_obj.intent = 'done'\n"
                "    # args.intent is never set\n"
                "    return args.intent\n"
            )
            (tmp / "test_recv.py").write_text(sample_code, encoding="utf-8")
            finding = {
                "file": "test_recv.py",
                "start_line": 3,
                "end_line": 4,
                "message": "In `launch`, `args.intent` is never set before read.",
            }
            res = verify_finding(finding, repo_dir=tmp)
            # MUST NOT be rejected!
            self.assertNotEqual(res["verification_status"], "rejected")
            self.assertEqual(res["verification_status"], "uncertain")

    def test_adversarial_literal_in_comment_not_rejected(self):
        """Occurrence of literal in a comment must NOT refute finding claiming event is never recorded."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            sample_code = (
                "# Note: we should record 'queue_overflow' when queue size exceeds 100\n"
                "def enqueue(item):\n"
                "    # But nowhere is queue_overflow actually recorded!\n"
                "    pass\n"
            )
            (tmp / "test_comment.py").write_text(sample_code, encoding="utf-8")
            finding = {
                "file": "test_comment.py",
                "start_line": 2,
                "end_line": 4,
                "message": "Nowhere in `test_comment.py` is `queue_overflow` ever recorded.",
            }
            res = verify_finding(finding, repo_dir=tmp)
            # MUST NOT be rejected!
            self.assertNotEqual(res["verification_status"], "rejected")
            self.assertEqual(res["verification_status"], "uncertain")

    def test_adversarial_method_in_unrelated_class_not_rejected(self):
        """Method in an unrelated class must NOT refute finding that standalone function is undefined."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            sample_code = (
                "class WorkerPool:\n"
                "    def dispatch_now(self):\n"
                "        pass\n"
                "\n"
                "def standalone_caller():\n"
                "    # Calling dispatch_now() globally will raise NameError\n"
                "    return dispatch_now()\n"
            )
            (tmp / "test_method.py").write_text(sample_code, encoding="utf-8")
            finding = {
                "file": "test_method.py",
                "start_line": 6,
                "end_line": 7,
                "message": "In `standalone_caller`, `dispatch_now` is neither defined nor imported.",
            }
            res = verify_finding(finding, repo_dir=tmp)
            # MUST NOT be rejected!
            self.assertNotEqual(res["verification_status"], "rejected")
            self.assertEqual(res["verification_status"], "uncertain")

    def test_verify_findings_batch_and_zero_tp_rejection_guarantee(self):
        """Batch verification must reject confirmed FPs while retaining 100% of TPs and edge cases."""
        test_findings = [
            # 1. Confirmed FP: args._launch_intent in same scope
            {
                "file": "bin/herdr-task",
                "start_line": 3377,
                "end_line": 3404,
                "message": "never setting `args._launch_intent`",
            },
            # 2. Confirmed FP: _record_router_failure_task at module level
            {
                "file": "bin/herdr-task",
                "start_line": 3455,
                "end_line": 3456,
                "message": "`_record_router_failure_task` is neither defined nor imported",
            },
            # 3. Known TP: task_id sort key drift (must be retained as uncertain)
            {
                "file": "herdr/fix_loop.py",
                "start_line": 66,
                "end_line": 69,
                "message": "Ephemeral `task_id` used in `key=lambda b: str(b.get('task_id') or '')`",
            },
            # 4. Known TP: timeout policy (must be retained as uncertain)
            {
                "file": "services/herdr-controller.py",
                "start_line": 5398,
                "end_line": 5422,
                "message": "Fallback returns unknown on exception rather than raising",
            },
        ]
        retained, rejected, summary = verify_findings(test_findings, repo_dir=HERDR_ROOT)
        self.assertEqual(len(retained), 2)  # 2 TPs retained!
        self.assertEqual(len(rejected), 2)  # 2 FPs rejected!
        self.assertEqual(summary["counts"]["rejected"], 2)
        self.assertEqual(summary["counts"]["uncertain"], 2)
        self.assertEqual(summary["counts"]["verified"], 0)  # No unjustified verified status!
        self.assertLess(summary["elapsed_ms"], 10000)

    def test_reject_syntax_error_claim_when_ast_parses_cleanly(self):
        """Claims of SyntaxError/unexpected EOF/incomplete function must be REJECTED when file parses cleanly."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            sample_code = (
                "def verify_os_security_isolation(repo_dir):\n"
                "    isolated_api_key = os.environ.get('HERDR_REVIEWER_GEMINI_API_KEY')\n"
                "    isolated_auth_dir = os.environ.get('HERDR_REVIEWER_AUTH_DIR')\n"
                "    if isolated_api_key or isolated_auth_dir:\n"
                "        profile = generate_macos_seatbelt_profile(repo_dir, isolated_auth_dir=isolated_dir)\n"
                "        return True, 'verified', {}\n"
                "    return False, 'not isolated', {}\n"
            )
            (tmp / "herdr_code.py").write_text(sample_code, encoding="utf-8")
            finding = {
                "file": "herdr_code.py",
                "start_line": 4,
                "end_line": 5,
                "message": (
                    "SyntaxError: Incomplete function definition in verify_os_security_isolation. "
                    "Line ends abruptly with isolated_auth_dir=isola, leaving an unclosed function call."
                ),
            }
            res = verify_finding(finding, repo_dir=tmp)
            self.assertEqual(res["verification_status"], "rejected")
            self.assertIn("counter_evidence_found", res["verification_reason"])
            self.assertTrue(res["counter_evidence"]["ast_parsed"])


    def test_reject_chinese_unclosed_string_claim_when_ast_parses_cleanly(self):
        """Claims of '存在未闭合字符串' or unclosed strings must be REJECTED when AST parses cleanly."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            sample_code = (
                "def generate_macos_seatbelt_profile(repo_dir, isolated_auth_dir=None):\n"
                "    return '''(version 1)\n(allow default)\n'''\n"
            )
            (tmp / "seatbelt.py").write_text(sample_code, encoding="utf-8")
            finding = {
                "file": "seatbelt.py",
                "start_line": 1,
                "end_line": 2,
                "message": "generate_macos_seatbelt_profile 存在未闭合字符串，导致解析异常",
            }
            res = verify_finding(finding, repo_dir=tmp)
            self.assertEqual(res["verification_status"], "rejected")
            self.assertIn("counter_evidence_found", res["verification_reason"])
            self.assertTrue(res["counter_evidence"]["ast_parsed"])

    def test_reject_chinese_symbol_not_defined_claim_when_symbol_exists(self):
        """Claims of 'symbol 未定义' must be REJECTED when symbol definition or import is reachable."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            sample_code = (
                "def verify_os_security_isolation(repo_dir):\n"
                "    return True, 'verified', {}\n"
            )
            (tmp / "sec.py").write_text(sample_code, encoding="utf-8")
            finding = {
                "file": "sec.py",
                "start_line": 1,
                "end_line": 2,
                "message": "verify_os_security_isolation 未定义，调用将触发 NameError",
            }
            res = verify_finding(finding, repo_dir=tmp)
            self.assertEqual(res["verification_status"], "rejected")
            self.assertIn("counter_evidence_found", res["verification_reason"])
            self.assertEqual(res["counter_evidence"]["type"], "definition_exists")
            self.assertEqual(res["counter_evidence"]["symbol"], "verify_os_security_isolation")

    def test_adversarial_resource_leak_unclosed_connection_not_rejected_by_check_e(self):
        """Resource leak findings mentioning '未闭合' (e.g. 数据库连接未闭合) must NOT be rejected as syntax errors."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            sample_code = (
                "def fetch_data():\n"
                "    conn = create_connection()\n"
                "    return conn.query()\n"
            )
            (tmp / "db.py").write_text(sample_code, encoding="utf-8")
            finding = {
                "file": "db.py",
                "start_line": 2,
                "end_line": 3,
                "message": "数据库连接未闭合，在异常退出时存在连接泄漏风险",
            }
            res = verify_finding(finding, repo_dir=tmp)
            # Should NOT be rejected by Check E (valid syntax refutation)
            self.assertNotEqual(res["verification_status"], "rejected")
            self.assertEqual(res["verification_status"], "uncertain")


if __name__ == "__main__":
    unittest.main()
