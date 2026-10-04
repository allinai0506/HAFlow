import unittest
import sys
from pathlib import Path

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr import gate_validator


class TestGateValidator(unittest.TestCase):
    def test_syntax_validation(self):
        """Valid shell commands pass; invalid syntax returns error."""
        ok, err = gate_validator.validate_shell_syntax("awk -F'\\t' '$1!=\"\"' file.tsv")
        self.assertTrue(ok)
        self.assertEqual(err, "")

        ok, err = gate_validator.validate_shell_syntax("awk -F'\\t' '$1!=")
        self.assertFalse(ok)
        self.assertIn("syntax", err.lower())

    def test_detect_tautology_same_column(self):
        """Comparing a column to itself ($4 == $4 or $4 != $4) must be detected."""
        res = gate_validator.validate_gate_command("awk -F'\\t' '$4 == $4' file.tsv")
        self.assertFalse(res["valid"])
        self.assertTrue(any("tautology" in issue.lower() or "self-comparison" in issue.lower() for issue in res["issues"]))

        res = gate_validator.validate_gate_command("awk -F'\\t' '$4 != $4' file.tsv")
        self.assertFalse(res["valid"])
        self.assertTrue(any("contradiction" in issue.lower() or "self-comparison" in issue.lower() for issue in res["issues"]))

    def test_detect_out_of_bounds_column(self):
        """Accessing a column index beyond the TSV schema must fail."""
        schema = ["col1", "col2", "col3"]
        res = gate_validator.validate_gate_command("awk -F'\\t' '$5 != \"\"' file.tsv", tsv_schema=schema)
        self.assertFalse(res["valid"])
        self.assertTrue(any("out of bounds" in issue.lower() or "index 5" in issue.lower() for issue in res["issues"]))

    def test_detect_disjoint_domain_tautology(self):
        """Comparing columns with disjoint domains (e.g. YES/NO vs 保留/裁剪) must be rejected as tautology."""
        schema = {
            1: {"name": "id", "type": "string"},
            2: {"name": "verdict", "type": "enum", "allowed_values": ["PASS", "FAIL"]},
            3: {"name": "action", "type": "enum", "allowed_values": ["RETAIN", "PRUNE"]},
        }
        # $2 != $3 will ALWAYS be true (disjoint sets)
        res = gate_validator.validate_gate_command("awk -F'\\t' '$2 != $3' file.tsv", tsv_schema=schema)
        self.assertFalse(res["valid"])
        self.assertTrue(any("disjoint" in issue.lower() or "tautology" in issue.lower() for issue in res["issues"]))

        # $2 == $3 will ALWAYS be false (disjoint sets)
        res = gate_validator.validate_gate_command("awk -F'\\t' '$2 == $3' file.tsv", tsv_schema=schema)
        self.assertFalse(res["valid"])
        self.assertTrue(any("disjoint" in issue.lower() or "contradiction" in issue.lower() for issue in res["issues"]))

    def test_valid_schema_comparison(self):
        """Comparing compatible columns passes."""
        schema = {
            1: {"name": "expected_status", "type": "enum", "allowed_values": ["A", "B"]},
            2: {"name": "actual_status", "type": "enum", "allowed_values": ["A", "B"]},
        }
        res = gate_validator.validate_gate_command("awk -F'\\t' '$1 == $2' file.tsv", tsv_schema=schema)
        self.assertTrue(res["valid"])
    def test_validate_workflow_dag_rejects_tautological_gate(self):
        """Workflow DAG validation must reject nodes with tautological gate commands."""
        from herdr.workflow import validate_workflow_dag
        nodes = [
            {
                "id": "gate_node",
                "gate": {
                    "auto_criteria": "awk -F'\\t' '$4 == $4' output.tsv"
                }
            }
        ]
        with self.assertRaises(ValueError) as ctx:
            validate_workflow_dag(nodes)
        self.assertIn("gate command validation failed", str(ctx.exception))
        self.assertIn("tautology", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
