"""A successful test summary cannot override failed command execution."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

from herdr.evaluator import calculate_metrics, init_loop, is_converged, read_state


@pytest.mark.parametrize("summary", [
    "4015 passed in 3.00s",
    "Tests  4015 passed (4015)",
    "Tests: 4015 passed, 4015 total",
])
@pytest.mark.parametrize("exit_code", [1, 124, -15])
def test_nonzero_exit_cannot_converge_after_green_summary(summary, exit_code):
    metrics = calculate_metrics(summary, exit_code)
    assert metrics.passed_tests == 4015  # retain the observed counts
    assert metrics.composite_score < 99.9
    assert not is_converged(metrics)


def test_loop_cli_persists_failure_after_green_summary(tmp_path):
    loader = importlib.machinery.SourceFileLoader(
        "loop_execution_status", str(Path(__file__).parents[1] / "bin/herdr-loop")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    loop = importlib.util.module_from_spec(spec)
    loader.exec_module(loop)
    directory = init_loop(tmp_path, "execution must finish successfully",
                          test_cmd='sh -c "printf \'Tests  4015 passed (4015)\\n\'; exit 124"')
    metrics, converged, status = loop.run_evaluation(tmp_path)
    assert metrics.details["test_exit_code"] != 0
    assert not converged
    assert status == "iterating"
    assert not read_state(directory)["converged"]
    snapshot = json.loads((directory / "EVAL_DONE.json").read_text())
    assert not snapshot["converged"]
    assert snapshot["passed_tests"] == 4015
    assert "Evaluation Succeeded" not in (directory / "EVALUATION.md").read_text()
