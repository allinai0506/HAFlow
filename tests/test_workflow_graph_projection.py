"""Flow Workbench v1 — Graph Projection tests (RED-first).

Covers task spec: DAG truth, parallel, status aggregation, legacy fail-soft,
custom workflow, context contract. Pure function, no I/O, no model calls.
"""

import unittest

from herdr import workflow as herdr_workflow
from herdr import workflow_graph as herdr_graph


def _sdv1():
    return herdr_workflow.load_template("software-development-v1")


class TestGraphProjectionEdges(unittest.TestCase):
    def test_software_development_v1_exact_edges(self):
        wf = _sdv1()
        proj = herdr_graph.workflow_graph_projection(wf, [])
        edges = {(e["from"], e["to"]) for e in proj["edges"]}
        expected = {
            ("requirements", "plan"),
            ("plan", "implementation"),
            ("implementation", "test"),
            ("implementation", "review"),
            ("test", "wrapup"),
            ("review", "wrapup"),
        }
        self.assertEqual(edges, expected)
        self.assertNotIn(("test", "review"), edges)
        self.assertNotIn(("review", "test"), edges)

    def test_parallel_nodes_keep_parallel_topology(self):
        wf = _sdv1()
        proj = herdr_graph.workflow_graph_projection(wf, [])
        by_id = {n["id"]: n for n in proj["nodes"]}
        self.assertEqual(by_id["test"]["depends_on"], ["implementation"])
        self.assertEqual(by_id["review"]["depends_on"], ["implementation"])
        # Projection must not manufacture serial relation between parallels
        edges = {(e["from"], e["to"]) for e in proj["edges"]}
        self.assertNotIn(("test", "review"), edges)
        self.assertNotIn(("review", "test"), edges)


class TestStatusAggregation(unittest.TestCase):
    def test_blocked_wins_over_working_and_completed(self):
        wf = _sdv1()
        tasks = [
            {"task_id": "a", "node": "implementation", "stage": "implementation", "status": "completed", "agent": "codex"},
            {"task_id": "b", "node": "implementation", "stage": "implementation", "status": "working", "agent": "opencode"},
            {"task_id": "c", "node": "implementation", "stage": "implementation", "status": "blocked", "agent": "codex"},
        ]
        proj = herdr_graph.workflow_graph_projection(wf, tasks)
        by_id = {n["id"]: n for n in proj["nodes"]}
        node = by_id["implementation"]
        self.assertEqual(node["status"], "blocked")
        self.assertEqual(node["task_count"], 3)
        self.assertEqual(node["blocked_task_count"], 1)
        self.assertEqual(node["failed_task_count"], 0)
        self.assertIn("codex", node["agents"])
        self.assertIn("opencode", node["agents"])
        self.assertTrue(node["has_attention"])

    def test_failed_priority_and_working(self):
        wf = _sdv1()
        proj = herdr_graph.workflow_graph_projection(
            wf,
            [{"task_id": "t1", "node": "test", "stage": "test", "status": "failed", "agent": "codex"}],
        )
        by_id = {n["id"]: n for n in proj["nodes"]}
        self.assertEqual(by_id["test"]["status"], "failed")

    def test_waiting_when_no_tasks(self):
        wf = _sdv1()
        proj = herdr_graph.workflow_graph_projection(wf, [])
        by_id = {n["id"]: n for n in proj["nodes"]}
        self.assertEqual(by_id["requirements"]["status"], "waiting")


class TestLegacyAndCustom(unittest.TestCase):
    def test_legacy_stages_only_fail_soft(self):
        legacy = {"stages": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}]}
        proj = herdr_graph.workflow_graph_projection(legacy, [])
        ids = [n["id"] for n in proj["nodes"]]
        self.assertEqual(ids, ["a", "b"])
        edges = {(e["from"], e["to"]) for e in proj["edges"]}
        self.assertEqual(edges, {("a", "b")})

    def test_empty_workflow_fail_soft(self):
        proj = herdr_graph.workflow_graph_projection({}, [])
        self.assertEqual(proj["nodes"], [])
        self.assertEqual(proj["edges"], [])

    def test_custom_diamond_fan(self):
        wf = {
            "nodes": [
                {"id": "A", "label": "A", "depends_on": []},
                {"id": "B", "label": "B", "depends_on": ["A"]},
                {"id": "C", "label": "C", "depends_on": ["B"]},
                {"id": "D", "label": "D", "depends_on": ["B"]},
                {"id": "E", "label": "E", "depends_on": ["B"]},
                {"id": "F", "label": "F", "depends_on": ["C", "D", "E"]},
            ]
        }
        proj = herdr_graph.workflow_graph_projection(wf, [])
        edges = {(e["from"], e["to"]) for e in proj["edges"]}
        self.assertEqual(
            edges,
            {
                ("A", "B"),
                ("B", "C"),
                ("B", "D"),
                ("B", "E"),
                ("C", "F"),
                ("D", "F"),
                ("E", "F"),
            },
        )
        by_id = {n["id"]: n for n in proj["nodes"]}
        self.assertEqual(sorted(by_id["F"]["depends_on"]), ["C", "D", "E"])


class TestContextContract(unittest.TestCase):
    def test_context_present_passthrough(self):
        wf = {
            "context": {"required": [{"id": "customer-profile"}], "optional": [{"id": "historical-quotes"}]},
            "nodes": [{"id": "A", "label": "A", "depends_on": []}],
        }
        proj = herdr_graph.workflow_graph_projection(wf, [])
        self.assertIn("customer-profile", proj["context"]["required"])
        self.assertIn("historical-quotes", proj["context"]["optional"])

    def test_no_context_no_invention(self):
        wf = {"nodes": [{"id": "A", "label": "A", "depends_on": []}]}
        proj = herdr_graph.workflow_graph_projection(wf, [])
        self.assertEqual(proj["context"]["required"], [])
        self.assertEqual(proj["context"]["optional"], [])


if __name__ == "__main__":
    unittest.main()
