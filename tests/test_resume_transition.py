"""resume must only restore a paused task to a state the transition matrix allows.

SC-016 regression: resume_task restored the historical from-state (dispatched or
blocked), but paused -> dispatched / blocked are not legal edges, so the CLI
printed "Illegal transition" and the task stayed paused.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERDR_ROOT = Path(__file__).resolve().parent.parent
REAL_PYTHON = sys.executable


class ResumeTransitionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="resume-test-"))
        self.env = dict(os.environ)
        self.env["HERDR_STATE_DB"] = str(self.tmp / "state.db")
        self.env["HOME"] = str(self.tmp / "home")
        (self.tmp / "home").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, status="dispatched"):
        code = (
            "import os, sys; sys.path.insert(0, r'%s');"
            "from pathlib import Path; from herdr import state_db;"
            "db = Path(os.environ['HERDR_STATE_DB']);"
            "state_db.save_workflow({'workflow_id':'wf-x','project_id':'p','title':'t','status':'running','config':{}}, db_path=db);"
            "state_db.save_task({'task_id':'T-R','workflow_id':'wf-x','node':'impl','stage':'impl',"
            "'agent':'claude','status':'%s','goal':'g','payload_json':{}}, db_path=db)"
        ) % (HERDR_ROOT, status)
        subprocess.run([REAL_PYTHON, "-c", code], env=self.env, check=True, capture_output=True)

    def _status(self):
        code = (
            "import os, sys; sys.path.insert(0, r'%s');"
            "from pathlib import Path; from herdr import state_db;"
            "print(state_db.get_task('T-R', db_path=Path(os.environ['HERDR_STATE_DB']))['status'])"
        ) % (HERDR_ROOT,)
        result = subprocess.run([REAL_PYTHON, "-c", code], env=self.env, check=True, capture_output=True, text=True)
        return result.stdout.strip()

    def _task_cli(self, *args):
        return subprocess.run([REAL_PYTHON, str(HERDR_ROOT / "bin" / "herdr-task"), *args],
                              env=self.env, capture_output=True, text=True)

    def _pause_from(self, status):
        self._seed(status=status)
        paused = self._task_cli("set", "T-R", "paused")
        self.assertEqual(paused.returncode, 0, paused.stderr)
        self.assertEqual(self._status(), "paused")
        return self._task_cli("resume", "T-R")

    def test_resume_from_working_returns_to_working(self):
        done = self._pause_from("working")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self._status(), "working")

    def test_resume_from_rework_returns_to_rework(self):
        done = self._pause_from("rework")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self._status(), "rework")

    def test_resume_from_dispatched_does_not_wedge_the_task(self):
        done = self._pause_from("dispatched")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("Illegal transition", done.stdout + done.stderr)
        self.assertEqual(self._status(), "working")

    def test_resume_from_blocked_does_not_wedge_the_task(self):
        done = self._pause_from("blocked")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("Illegal transition", done.stdout + done.stderr)
        self.assertEqual(self._status(), "working")

    def test_resume_of_task_paused_from_dispatched_is_not_silently_stuck(self):
        # The old code left the task paused and still exited 0 from the pipe;
        # the unwind must be visible in the task row.
        done = self._pause_from("dispatched")
        self.assertNotEqual(self._status(), "paused", done.stdout + done.stderr)

    def test_resume_uses_the_latest_pause_event(self):
        # rework -> paused -> rework -> working -> blocked -> paused -> resume
        # must return to working, not to the older rework target.
        self._seed(status="paused")
        history = [
            {"from": "rework", "to": "paused", "at": 1},
            {"from": "paused", "to": "rework", "at": 2},
            {"from": "rework", "to": "working", "at": 3},
            {"from": "working", "to": "blocked", "at": 4},
            {"from": "blocked", "to": "paused", "at": 5},
        ]
        code = (
            "import os, sys; sys.path.insert(0, r'%s');"
            "from pathlib import Path; from herdr import state_db;"
            "db = Path(os.environ['HERDR_STATE_DB']);"
            "t = state_db.get_task('T-R', db_path=db);"
            "t['status_history'] = %r;"
            "state_db.save_task(t, db_path=db)"
        ) % (HERDR_ROOT, history)
        subprocess.run([REAL_PYTHON, "-c", code], env=self.env, check=True, capture_output=True)
        done = self._task_cli("resume", "T-R")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertNotIn("Illegal transition", done.stdout + done.stderr)
        self.assertEqual(self._status(), "working")


if __name__ == "__main__":
    unittest.main()
