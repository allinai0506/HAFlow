"""Real runner receipts, fresh logs and outer success are necessary evidence."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import shlex

import pytest

from herdr.evaluator import MetricVector, init_loop, is_converged, write_baseline_lint, write_state


@pytest.fixture
def loop_cli():
    script = Path(__file__).resolve().parents[1] / "bin" / "herdr-loop"
    loader = importlib.machinery.SourceFileLoader("loop_runner_contract", str(script))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def controlled_runner(tmp_path, *, exit_code=0, receipts=None, repro=False,
                      stale=None, missing=None, lint_output=""):
    directory = init_loop(tmp_path, "execution evidence must belong to this evaluation",
                          test_cmd="true", repro_cmd="true" if repro else "")
    logs = directory / "logs"
    logs.mkdir(exist_ok=True)
    outputs = {"test": "5 passed in 0.1s\n", "lint": lint_output}
    if repro:
        outputs["repro"] = "2 passed in 0.1s\n"
    body = "#!/bin/bash\n"
    for name, output in outputs.items():
        path = logs / (name + ".log")
        if name == stale:
            path.write_text(output)
        elif name != missing:
            body += "printf %s " + shlex.quote(output) + " > " + shlex.quote(str(path)) + "\n"
    if receipts is None:
        receipts = ["TEST_EXIT=0", "LINT_EXIT=0"] + (["REPRO_EXIT=0"] if repro else [])
    body += "".join("echo " + shlex.quote(receipt) + "\n" for receipt in receipts)
    body += "exit " + str(exit_code) + "\n"
    script = directory / "EVALUATOR.sh"
    script.write_text(body)
    script.chmod(0o755)
    return directory


@pytest.mark.parametrize("options", [
    {"exit_code": 17},
    {"receipts": ["TEST_EXIT=0"]},
    {"repro": True, "receipts": ["TEST_EXIT=0", "LINT_EXIT=0"]},
    {"receipts": ["TEST_EXIT=0", "TEST_EXIT=0", "LINT_EXIT=0"]},
    {"receipts": ["TEST_EXIT=invalid", "LINT_EXIT=0"]},
    {"receipts": ["TEST_EXIT=0=invalid", "LINT_EXIT=0"]},
    {"receipts": ["TEST_EXIT=0", "LINT_EXIT=invalid"]},
    {"stale": "test"},
    {"stale": "lint"},
    {"repro": True, "stale": "repro"},
    {"missing": "test"},
    {"missing": "lint"},
])
def test_invalid_execution_never_persists_success(tmp_path, loop_cli, options):
    directory = controlled_runner(tmp_path, **options)
    metrics, converged, status = loop_cli.run_evaluation(tmp_path)
    assert not converged
    assert status == "iterating"
    assert metrics.composite_score < 99.9
    snapshot = json.loads((directory / "EVAL_DONE.json").read_text())
    assert not snapshot["converged"]
    assert metrics.details.get("evaluation_errors")
    assert snapshot.get("evaluation_errors")


def test_nonzero_outer_exit_preserves_observed_passing_counts(tmp_path, loop_cli):
    controlled_runner(tmp_path, exit_code=17)
    metrics, converged, _ = loop_cli.run_evaluation(tmp_path)
    assert metrics.passed_tests == metrics.total_tests == 5
    assert metrics.details.get("test_exit_code") == 0
    assert metrics.details.get("evaluation_exit_code") == 17
    assert not converged


def test_normal_generated_three_step_runner_converges(tmp_path, loop_cli):
    init_loop(tmp_path, "complete execution", test_cmd="printf '5 passed in 0.1s\\n'",
              lint_cmd="true", repro_cmd="printf '2 passed in 0.1s\\n'")
    metrics, converged, status = loop_cli.run_evaluation(tmp_path)
    assert converged
    assert status == "converged"
    assert metrics.passed_tests == metrics.total_tests == 5
    assert metrics.has_repro_test


def test_recorded_lint_baseline_still_permits_complete_execution(tmp_path, loop_cli):
    directory = controlled_runner(tmp_path, receipts=["TEST_EXIT=0", "LINT_EXIT=1"],
                                  lint_output="1 problem (1 error, 0 warnings)\n")
    write_baseline_lint(directory, 1)
    metrics, converged, _ = loop_cli.run_evaluation(tmp_path)
    assert metrics.lint_errors == 1
    assert metrics.new_lint_errors == 0
    assert converged


def test_stale_repro_from_removed_contract_is_not_reused(tmp_path, loop_cli):
    directory = controlled_runner(tmp_path)
    (directory / "logs" / "repro.log").write_text("1 failed, 0 passed in 0.1s\n")
    metrics, converged, _ = loop_cli.run_evaluation(tmp_path)
    assert not metrics.has_repro_test
    assert converged


def test_exhaustion_reports_execution_cause_to_coordinator(tmp_path, loop_cli):
    directory = controlled_runner(tmp_path, receipts=["TEST_EXIT=0"])
    write_state(directory, 0, 1, "initialized", False)
    _, converged, status = loop_cli.run_evaluation(tmp_path)
    assert not converged
    assert status == "exhausted"
    reason = "missing_exit_receipt:LINT_EXIT"
    assert reason in (directory / "EVALUATION.md").read_text()
    assert reason in (directory / "BLOCKER.md").read_text()


@pytest.mark.parametrize("details", [
    {"evaluation_exit_code": 17},
    {"evaluation_errors": ["missing_exit_receipt:LINT_EXIT"]},
    {"evaluation_exit_code": 17, "evaluation_errors": ["stale_step_log:test"]},
])
def test_invalid_execution_vetoes_even_a_full_cached_score(details):
    metrics = MetricVector(composite_score=100.0, details=details)
    assert not is_converged(metrics)
