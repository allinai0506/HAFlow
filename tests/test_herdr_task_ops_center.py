"""Tests for the herdr-task ops-center aggregation entry."""

import importlib
import importlib.machinery
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))


def _import_herdr_task():
    task_bin = HERDR_ROOT / "bin" / "herdr-task"
    spec = importlib.util.spec_from_loader(
        "herdr_task_ops_center_test",
        importlib.machinery.SourceFileLoader(
            "herdr_task_ops_center_test",
            str(task_bin),
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ht = _import_herdr_task()


class TestOpsCenterPayload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-ops-center-")
        self.task_file = str(Path(self.tmp.name) / "tasks.json")
        _ht.TASKS_FILE = self.task_file

    def tearDown(self):
        self.tmp.cleanup()

    def _write_tasks(self, tasks):
        with open(self.task_file, "w", encoding="utf-8") as f:
            json.dump({"tasks": tasks}, f)

    def _fleet_row(self, payload, agent):
        for row in payload["agent_fleet"]:
            if row["agent"] == agent:
                return row
        return None

    def _anomaly_kinds(self, payload):
        return {entry["kind"] for entry in payload["anomalies"]}

    def test_ops_center_payload_summary(self):
        tasks = [
            {
                "task_id": "impl-01",
                "workflow_id": "wf-alpha",
                "node": "implementation",
                "stage": "implementation",
                "agent": "opencode",
                "pane_id": "p-impl",
                "status": "working",
                "created_at": 10,
                "started_at": 20,
                "last_activity_at": 180,
                "acceptance_criteria": ["done"],
                "status_history": [
                    {"status": "pending", "at": 10, "from": None, "to": "pending"}
                ],
            },
            {
                "task_id": "test-01",
                "workflow_id": "wf-beta",
                "node": "test",
                "stage": "test",
                "agent": "codex",
                "pane_id": "p-test",
                "status": "dispatched",
                "created_at": 50,
                "started_at": 60,
                "last_activity_at": 100,
                "acceptance_criteria": ["pass"],
                "status_history": [
                    {"status": "pending", "at": 50, "from": None, "to": "pending"}
                ],
            },
        ]
        self._write_tasks(tasks)

        runtime_status = {"p-impl": "working", "p-test": "idle"}
        with patch.object(_ht, "_agent_runtime_status", side_effect=lambda pane: runtime_status.get(pane)):
            payload = _ht._ops_center_payload(workflow_id=None, now=300, include_tasks=False)

        self.assertEqual(payload["scope"], "all")
        self.assertEqual(payload["boss"]["running_workflows"], 2)
        self.assertEqual(payload["boss"]["working_agents"], 2)
        self.assertEqual(len(payload["workflow_cards"]), 2)
        self.assertEqual(payload["count"], 2)

        alpha = next(item for item in payload["workflow_cards"] if item["workflow_id"] == "wf-alpha")
        self.assertEqual(alpha["tasks"]["active"], 1)
        self.assertEqual(alpha["tasks"]["failed"], 0)

        codex = self._fleet_row(payload, "codex")
        self.assertIsNotNone(codex)
        self.assertEqual(codex["load"], 1)
        self.assertEqual(codex["runtime_status"], "idle")

    def test_ops_center_detects_anomalies(self):
        tasks = [
            {
                "task_id": "impl-01",
                "workflow_id": "wf-alpha",
                "node": "implementation",
                "stage": "implementation",
                "agent": "opencode",
                "pane_id": "p-impl",
                "status": "working",
                "created_at": 10,
                "started_at": 20,
                "last_activity_at": 100,
                "acceptance_criteria": ["done"],
            },
            {
                "task_id": "rev-01",
                "workflow_id": "wf-alpha",
                "node": "review",
                "stage": "review",
                "agent": "qodercli",
                "pane_id": "p-review",
                "status": "failed",
                "failure_reason": "Token limit reached, cannot continue",
                "created_at": 50,
                "started_at": 60,
                "failed_at": 200,
                "last_activity_at": 220,
                "acceptance_criteria": ["done"],
            },
        ]
        self._write_tasks(tasks)

        status_map = {
            "p-impl": "idle",
            "p-review": None,
        }
        with patch.object(_ht, "_agent_runtime_status", side_effect=lambda pane: status_map.get(pane)):
            payload = _ht._ops_center_payload(workflow_id="wf-alpha", now=800, include_tasks=False)

        anomaly_kinds = self._anomaly_kinds(payload)
        self.assertIn("TOKEN_EXHAUSTED", anomaly_kinds)
        self.assertIn("BLOCKED", anomaly_kinds)

        token_entry = next(
            entry for entry in payload["anomalies"] if entry["kind"] == "TOKEN_EXHAUSTED"
        )
        self.assertEqual(token_entry["task_id"], "rev-01")
        self.assertIn("让总指挥处理", token_entry["actions"])

    def test_ops_center_marks_stale_agent(self):
        tasks = [
            {
                "task_id": "impl-01",
                "workflow_id": "wf-alpha",
                "node": "implementation",
                "stage": "implementation",
                "agent": "qodercli",
                "pane_id": "p-impl",
                "status": "working",
                "created_at": 10,
                "started_at": 10,
                "last_activity_at": 100,
                "acceptance_criteria": ["done"],
            }
        ]
        self._write_tasks(tasks)
        with patch.object(_ht, "_agent_runtime_status", side_effect=lambda _: "idle"):
            payload = _ht._ops_center_payload(workflow_id="wf-alpha", now=1000, include_tasks=False)

        agent = self._fleet_row(payload, "qodercli")
        self.assertIsNotNone(agent)
        self.assertTrue(agent["stale"])
        self.assertEqual(agent["runtime_status"], "idle")
        self.assertEqual(agent["health"], "STALE")
        self.assertEqual(agent["task_status"], "working")

        detail = _ht._suggested_actions_for_anomaly("BLOCKED", tasks[0])
        self.assertIn("重试", detail["actions"])

    def test_ops_center_handles_invalid_failed_at(self):
        tasks = [
            {
                "task_id": "rev-01",
                "workflow_id": "wf-alpha",
                "node": "review",
                "stage": "review",
                "agent": "qodercli",
                "pane_id": "p-review",
                "status": "failed",
                "failure_reason": "Some temporary failure",
                "failed_at": "not-a-time",
                "created_at": 100,
                "last_activity_at": 200,
                "acceptance_criteria": ["done"],
            }
        ]
        self._write_tasks(tasks)

        payload = _ht._ops_center_payload(workflow_id="wf-alpha", now=400, include_tasks=False)
        self.assertEqual(payload["anomalies"][0]["kind"], "FAILED")

    def test_ops_center_degrades_when_runtime_probe_fails(self):
        tasks = [
            {
                "task_id": "impl-01",
                "workflow_id": "wf-alpha",
                "node": "implementation",
                "agent": "opencode",
                "status": "working",
                "pane_id": "p-impl",
                "created_at": 100,
                "started_at": 100,
                "last_activity_at": 390,
            }
        ]
        self._write_tasks(tasks)

        with patch.object(
            _ht.subprocess,
            "run",
            side_effect=_ht.subprocess.TimeoutExpired(cmd="herdr", timeout=3),
        ):
            payload = _ht._ops_center_payload(workflow_id="wf-alpha", now=400)

        agent = self._fleet_row(payload, "opencode")
        self.assertEqual(agent["runtime_status"], None)
        self.assertTrue(agent["stale"])
        self.assertIn("CONTROLLER_RECOVERY", self._anomaly_kinds(payload))


class TestSupersededStats(unittest.TestCase):
    """Superseded tasks must not pollute ops-center node card totals.

    The predicate here must stay in sync with is_node_complete in
    services/herdr-controller.py and node_status in bin/herdr-task.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-superseded-stats-")
        self.task_file = str(Path(self.tmp.name) / "tasks.json")
        _ht.TASKS_FILE = self.task_file

    def tearDown(self):
        self.tmp.cleanup()

    def _write_card_tasks(self, tasks, node="plan"):
        with open(self.task_file, "w", encoding="utf-8") as f:
            json.dump(
                {"tasks": [
                    dict(t, workflow_id="wf-1", node=node, stage=node)
                    for t in tasks
                ]},
                f,
            )

    def _card(self):
        with patch.object(
            _ht, "_agent_runtime_status", side_effect=lambda _: None
        ), patch.object(_ht, "load_workflow", return_value={"nodes": [{"id": "plan"}]}):
            payload = _ht._ops_center_payload(
                workflow_id="wf-1", now=1000, include_tasks=False
            )
        self.assertEqual(len(payload["workflow_cards"]), 1)
        return payload["workflow_cards"][0]

    def test_counts_exclude_superseded_tasks(self):
        counts = _ht._node_task_status_counts([
            {"task_id": "t1", "status": "cleaned"},
            {"task_id": "t2", "status": "superseded", "superseded_by": "t3"},
            {"task_id": "t3", "status": "cleaned"},
        ])
        self.assertEqual(counts["total"], 2)
        self.assertEqual(counts["completed"], 2)
        self.assertEqual(counts["superseded"], 1)

    def test_counts_exclude_superseded_by_carrying_tasks(self):
        counts = _ht._node_task_status_counts([
            {"task_id": "t1", "status": "cleaned", "superseded_by": "t2"},
        ])
        self.assertEqual(counts["total"], 0)
        self.assertEqual(counts["superseded"], 1)

    def test_card_node_completed_despite_superseded_task(self):
        self._write_card_tasks([
            {"task_id": "t1", "status": "cleaned"},
            {"task_id": "t2", "status": "superseded", "superseded_by": "t3"},
            {"task_id": "t3", "status": "cleaned"},
        ])
        node = self._card()["nodes"][0]
        self.assertEqual(node["status"], "completed")
        self.assertEqual(node["total"], 2)
        self.assertEqual(node["completed"], 2)
        self.assertEqual(node["superseded"], 1)

    def test_card_workflow_totals_exclude_superseded(self):
        self._write_card_tasks([
            {"task_id": "t1", "status": "cleaned"},
            {"task_id": "t2", "status": "superseded", "superseded_by": "t3"},
            {"task_id": "t3", "status": "cleaned"},
        ])
        tasks = self._card()["tasks"]
        self.assertEqual(tasks["total"], 2)
        self.assertEqual(tasks["completed"], 2)
        self.assertEqual(tasks["superseded"], 1)

    def test_card_node_all_superseded_is_marked_superseded(self):
        self._write_card_tasks([
            {"task_id": "t1", "status": "superseded", "superseded_by": "t2"},
        ])
        node = self._card()["nodes"][0]
        self.assertEqual(node["status"], "superseded")
        self.assertEqual(node["total"], 0)

    def test_drilldown_prefers_latest_authoritative_task(self):
        self._write_card_tasks([
            {"task_id": "t1", "status": "cleaned",
             "created_at": 10, "updated_at": 20},
            {"task_id": "t2", "status": "superseded", "superseded_by": "t3",
             "created_at": 30, "updated_at": 40},
            {"task_id": "t3", "status": "cleaned",
             "created_at": 50, "updated_at": 60},
        ])
        self.assertEqual(self._card()["nodes"][0]["drilldown_task"], "t3")


class PlannedNodeStatusTest(unittest.TestCase):
    def test_cli_and_card_do_not_complete_missing_planned_tasks(self):
        tasks = [{"task_id": "t1", "workflow_id": "wf-plan", "node": "implementation", "status": "integrated", "integration_mode": "git"}]
        cfg = {"nodes": [{"id": "implementation", "required_task_ids": ["t1", "t3"]}]}
        import io
        from contextlib import redirect_stdout
        with patch.object(_ht, "_safe_workflow", return_value=(cfg, "wf-plan")), patch.object(_ht, "resolve_node", return_value={"node_label": "implementation", "stage_label": "implementation"}), patch.object(_ht, "load_tasks", return_value={"tasks": tasks}):
            cards = _ht._build_workflow_cards({"wf-plan": tasks}, 1)
            self.assertNotEqual(cards[0]["nodes"][0]["status"], "completed")
            output = io.StringIO()
            with redirect_stdout(output):
                _ht.node_status("wf-plan", "implementation")
            self.assertFalse(json.loads(output.getvalue())["complete"])


class PersistedWorkflowConfigTest(unittest.TestCase):
    def test_node_status_uses_recorded_config_file_with_real_store(self):
        from herdr import projects
        from herdr.state_store import SQLiteStateStore
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "workflow.json"
            cfg.write_text(json.dumps({"nodes": [{"id": "implementation", "required_task_ids": ["t1", "t3"]}]}))
            store = SQLiteStateStore(Path(td) / "state.db")
            store.save_workflow({"workflow_id": "wf-plan", "status": "running", "workflow_file": str(cfg), "config": {}})
            store.save_task({"task_id": "t1", "workflow_id": "wf-plan", "node": "implementation", "status": "integrated", "integration_mode": "git"})
            with patch.object(_ht, "get_state_store", return_value=store), patch.object(projects, "_get_store", return_value=store), patch.object(_ht, "load_tasks", return_value={"tasks": store.list_tasks()}):
                output = io.StringIO()
                with redirect_stdout(output):
                    _ht.node_status("wf-plan", "implementation")
                self.assertFalse(json.loads(output.getvalue())["complete"])


    def test_missing_recorded_config_never_borrows_global_legacy(self):
        from herdr import projects
        from herdr.state_store import SQLiteStateStore
        with tempfile.TemporaryDirectory() as td:
            legacy = Path(td) / "legacy.json"
            legacy.write_text(json.dumps({"nodes": [{"id": "foreign"}]}))
            store = SQLiteStateStore(Path(td) / "state.db")
            store.save_workflow({"workflow_id": "wf-plan", "status": "running", "workflow_file": str(Path(td) / "missing.json"), "nodes": [{"id": "implementation", "required_task_ids": ["t3"]}]})
            with patch.object(_ht, "get_state_store", return_value=store), patch.object(projects, "_get_store", return_value=store), patch.object(projects, "LEGACY_WORKFLOW_FILE", legacy):
                self.assertEqual(_ht.load_workflow("wf-plan")["nodes"][0]["id"], "implementation")
