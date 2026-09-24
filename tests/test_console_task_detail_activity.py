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

    def test_task_created_dedupe_via_node(self):
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node not found; skipping JS execution test")
        src = _extract_fn(self.html, "buildTaskEvents")
        self.assertIsNotNone(src)
        num_src = _extract_fn(self.html, "taskDrawerNumTs")
        upd_src = _extract_fn(self.html, "taskUpdatedAt")
        harness = (
            STUB_GLOBALS
            + "\n" + num_src
            + "\n" + upd_src
            + "\n" + src
            + "\nconst dup=buildTaskEvents({created_at:1000,status_history:[{to:'pending',at:1000}]},{});"
            + "\nconst distinct=buildTaskEvents({created_at:1000,status_history:[{to:'working',at:2000}]},{});"
            + "\nconsole.log(JSON.stringify(["
            + "dup.filter(e=>e.type==='task_created').length,"
            + "distinct.map(e=>e.type)]));"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(harness)
            p = f.name
        try:
            res = subprocess.run([node_bin, p], capture_output=True, text=True, timeout=15)
        finally:
            Path(p).unlink(missing_ok=True)
        self.assertEqual(res.returncode, 0, f"node failed: {res.stderr}")
        dup_count, distinct_types = json.loads(res.stdout.strip())
        self.assertEqual(dup_count, 1, "created_at + pending history must not duplicate 任务已创建")
        self.assertEqual(distinct_types.count("task_created"), 1)
        self.assertIn("agent_started", distinct_types)

    def test_start_time_semantics_via_node(self):
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node not found; skipping JS execution test")
        parts = []
        for fn in ["taskDrawerNumTs", "taskExecStart", "taskStartedAt", "taskDisplayStart"]:
            src = _extract_fn(self.html, fn)
            self.assertIsNotNone(src, f"{fn} not found")
            parts.append(src)
        harness = (
            STUB_GLOBALS
            + "\n" + "\n".join(parts)
            + "\nconsole.log(JSON.stringify(["
            + "taskExecStart({started_at:900,created_at:1000,runtime:{started_at:1200}}),"
            + "taskStartedAt({started_at:900,created_at:1000,runtime:{started_at:1200}}),"
            + "taskExecStart({created_at:1000,runtime:{started_at:1200}}),"
            + "taskDisplayStart({created_at:1000,runtime:{started_at:1200}}),"
            + "taskExecStart({created_at:1000}),"
            + "taskDisplayStart({created_at:1000}),"
            + "taskDisplayStart({})]));"
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
        # explicit started_at wins over runtime and creation
        self.assertEqual(vals[0], 900)
        self.assertEqual(vals[1], 900)
        # runtime start beats creation time and is labeled 开始时间
        self.assertEqual(vals[2], 1200)
        self.assertEqual(vals[3], {"label": "开始时间", "ts": 1200})
        # creation-only falls back with honest 创建时间 label, never faked as 开始时间
        self.assertIsNone(vals[4])
        self.assertEqual(vals[5], {"label": "创建时间", "ts": 1000})
        self.assertIsNone(vals[6])

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
            + "\n" + _extract_fn(self.html, "taskExecStart")
            + "\n" + _extract_fn(self.html, "taskDisplayStart")
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
