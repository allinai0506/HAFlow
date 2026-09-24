"""Task Detail Activity: only real fields generate events, stable sort, no JS errors."""

import importlib.machinery
import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_console(name):
    path = ROOT / "console" / "herdr_factory_console.py"
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(name, str(path)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _extract_fn(html, name):
    # Extract a top-level `function name(...) {...}` with brace matching.
    start = html.find(f"function {name}(")
    if start < 0:
        return None
    i = html.find("{", start)
    if i < 0:
        return None
    depth = 0
    for j in range(i, len(html)):
        if html[j] == "{":
            depth += 1
        elif html[j] == "}":
            depth -= 1
            if depth == 0:
                return html[start:j + 1]
    return None


STUB_GLOBALS = (
    "function humanStatus(s){return s||'unknown';}"
    "var state={selectedTaskDetail:null,workflowId:null};"
    "function taskDisplayName(t){return 'T';}"
    "function esc(s){return String(s??'');}"
    "function badge(s){return String(s||'');}"
    "function formatElapsed(s){return (s==null?'':String(s)+'s');}"
)


class TestTaskDetailActivity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.console = _load_console("herdr_console_task_detail_activity_test")
        cls.html = getattr(cls.console, "HTML_TEMPLATE", "")

    def test_event_builder_exists_and_uses_real_fields(self):
        src = _extract_fn(self.html, "buildTaskEvents")
        self.assertIsNotNone(src, "buildTaskEvents not found")
        self.assertIn("status_history", src)
        self.assertIn("created_at", src)
        # must not fabricate timestamps or random events
        self.assertNotIn("Math.random", src)

    def test_event_builder_logic_via_node(self):
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node not found; skipping JS execution test")
        src = _extract_fn(self.html, "buildTaskEvents")
        self.assertIsNotNone(src)
        num_src = _extract_fn(self.html, "taskDrawerNumTs")
        self.assertIsNotNone(num_src)
        upd_src = _extract_fn(self.html, "taskUpdatedAt")
        self.assertIsNotNone(upd_src)
        # stub page globals used inside builder (humanStatus)
        harness = (
            STUB_GLOBALS
            + "\n" + num_src
            + "\n" + upd_src
            + "\n" + src
            + "\nconst task={task_id:'t1',status:'working',created_at:1000,updated_at:1060,"
            + "status_history:[{to:'pending',at:1000},{to:'dispatched',at:1010},{to:'working',at:1020}],"
            + "blocker:'',runtime:{started_at:1015}};"
            + "\nconst proj={recent_activity:['hi']};"
            + "\nconst evs=buildTaskEvents(task,proj);"
            + "\nconsole.log(JSON.stringify(evs));"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(harness)
            p = f.name
        try:
            res = subprocess.run([node_bin, p], capture_output=True, text=True, timeout=15)
        finally:
            Path(p).unlink(missing_ok=True)
        self.assertEqual(res.returncode, 0, f"node failed: {res.stderr}")
        evs = json.loads(res.stdout.strip())
        self.assertGreaterEqual(len(evs), 4)
        timed = [e for e in evs if e.get("timestamp")]
        ts = [float(e["timestamp"]) for e in timed]
        self.assertEqual(ts, sorted(ts), "timestamps must be stably sorted")
        types = [e["type"] for e in evs]
        self.assertIn("task_created", types)
        # recent_activity without timestamp must not invent one
        untimed = [e for e in evs if not e.get("timestamp")]
        self.assertTrue(any(e["type"] == "observation" for e in untimed))

    def test_empty_task_yields_no_fake_events_via_node(self):
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node not found; skipping JS execution test")
        src = _extract_fn(self.html, "buildTaskEvents")
        self.assertIsNotNone(src)
        num_src = _extract_fn(self.html, "taskDrawerNumTs")
        self.assertIsNotNone(num_src)
        upd_src = _extract_fn(self.html, "taskUpdatedAt")
        self.assertIsNotNone(upd_src)
        harness = (
            STUB_GLOBALS
            + "\n" + num_src
            + "\n" + upd_src
            + "\n" + src
            + "\nconsole.log(JSON.stringify(buildTaskEvents({},{})));"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(harness)
            p = f.name
        try:
            res = subprocess.run([node_bin, p], capture_output=True, text=True, timeout=15)
        finally:
            Path(p).unlink(missing_ok=True)
        self.assertEqual(res.returncode, 0, f"node failed: {res.stderr}")
        evs = json.loads(res.stdout.strip())
        self.assertEqual(evs, [])

    def test_runtime_missing_fields_no_throw_via_node(self):
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node not found; skipping JS execution test")
        for fn in ["renderTaskRuntime", "renderTaskArtifacts", "renderTaskOverview"]:
            src = _extract_fn(self.html, fn)
            self.assertIsNotNone(src, f"{fn} not found")
        harness = (
            STUB_GLOBALS
            + "\n" + _extract_fn(self.html, "taskDrawerNumTs")
            + "\n" + _extract_fn(self.html, "fmtClock")
            + "\n" + _extract_fn(self.html, "taskStartedAt")
            + "\n" + _extract_fn(self.html, "taskUpdatedAt")
            + "\n" + _extract_fn(self.html, "taskDurationSecs")
            + "\n" + _extract_fn(self.html, "renderTaskRuntime")
            + "\n" + _extract_fn(self.html, "renderTaskArtifacts")
            + "\n" + _extract_fn(self.html, "renderTaskOverview")
            + "\nconsole.log(JSON.stringify(["
            + "renderTaskRuntime({},{}).length>0,"
            + "renderTaskArtifacts({},{}).length>0,"
            + "renderTaskOverview({},{}).length>0]));"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(harness)
            p = f.name
        try:
            res = subprocess.run([node_bin, p], capture_output=True, text=True, timeout=15)
        finally:
            Path(p).unlink(missing_ok=True)
        self.assertEqual(res.returncode, 0, f"node failed: {res.stderr}")
        vals = json.loads(res.stdout.strip())
        self.assertEqual(vals, [True, True, True])


if __name__ == "__main__":
    unittest.main()
