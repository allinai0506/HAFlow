"""One process owns an evaluation namespace through execution and publication."""
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from herdr.evaluator import init_loop, write_baseline_lint

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "herdr-loop"


def active_evaluation(tmp_path):
    directory = init_loop(tmp_path, "namespace ownership", test_cmd="true")
    write_baseline_lint(directory, 1)
    script = directory / "EVALUATOR.sh"
    script.write_text("""#!/bin/bash
mkdir -p .herdr-loop/logs
printf '5 passed in 0.1s\\n' > .herdr-loop/logs/test.log
if mkdir first-owner 2>/dev/null; then
 printf '2 problems (2 errors, 0 warnings)\\n' > .herdr-loop/logs/lint.log
 touch first-ready
 while [ ! -f release-first ]; do sleep 0.01; done
else
 printf '1 problem (1 error, 0 warnings)\\n' > .herdr-loop/logs/lint.log
fi
echo TEST_EXIT=0
echo LINT_EXIT=1
exit 0
""")
    script.chmod(0o755)
    process = subprocess.Popen([sys.executable, str(CLI), "eval", "--dir", str(tmp_path)],
                               cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 10
    while not (tmp_path / "first-ready").exists() and time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(process.communicate())
        time.sleep(0.02)
    if not (tmp_path / "first-ready").exists():
        (tmp_path / "release-first").touch()
        process.communicate(timeout=10)
        raise AssertionError("controlled evaluation did not reach ready barrier")
    return directory, process


def snapshots(directory):
    names = ["GOAL.md", "STATE.md", "METRICS.json", "EVAL_DONE.json",
             "EVALUATOR.sh", "BASELINE_LINT.json", "logs/test.log", "logs/lint.log"]
    return {name: (directory / name).read_bytes() if (directory / name).exists() else None for name in names}


@pytest.mark.parametrize("operation", ["eval", "init", "baseline"])
def test_active_owner_rejects_parallel_writer_without_mutation(tmp_path, operation):
    directory, first = active_evaluation(tmp_path)
    try:
        before = snapshots(directory)
        if operation == "baseline":
            code = """import sys
from pathlib import Path
from herdr.evaluator import write_baseline_lint
try:
    write_baseline_lint(Path(sys.argv[1]), 10)
except RuntimeError as exc:
    print(exc)
    sys.exit(75)
"""
            argv = [sys.executable, "-c", code, str(directory)]
        else:
            argv = [sys.executable, str(CLI), operation, "--dir", str(tmp_path)]
            if operation == "init":
                argv += ["--goal", "must not overwrite active goal", "--test-cmd", "true"]
        second = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=10)
        after = snapshots(directory)
    finally:
        (tmp_path / "release-first").touch()
        first.communicate(timeout=10)
    assert second.returncode == 75
    if operation != "baseline":
        assert "HERDR_LOOP_BUSY" in second.stdout
    assert before == after
    assert first.returncode == 1
    final = json.loads((directory / "EVAL_DONE.json").read_text())
    assert final["lint_errors"] == 2
    assert final["new_lint_errors"] == 1
    assert not final["converged"]


def test_owner_process_death_releases_namespace_for_recovery(tmp_path):
    _, first = active_evaluation(tmp_path)
    try:
        first.terminate()  # Only the Popen child created by this test.
        first.communicate(timeout=10)
    finally:
        (tmp_path / "release-first").touch()
        if first.poll() is None:
            first.terminate()
            first.wait(timeout=10)
    init = subprocess.run([sys.executable, str(CLI), "init", "--dir", str(tmp_path),
                           "--goal", "recovery", "--test-cmd", "printf '1 passed in 0.1s\\n'"],
                          cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert init.returncode == 0
    evaluate = subprocess.run([sys.executable, str(CLI), "eval", "--dir", str(tmp_path)],
                              cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert evaluate.returncode == 0


def test_exception_releases_namespace_for_reinitialization(tmp_path):
    directory = init_loop(tmp_path, "exception recovery", test_cmd="true")
    (directory / "STATE.md").write_text("- **iteration**: broken\n")
    evaluate = subprocess.run([sys.executable, str(CLI), "eval", "--dir", str(tmp_path)],
                              cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert evaluate.returncode != 0
    init = subprocess.run([sys.executable, str(CLI), "init", "--dir", str(tmp_path), "--goal", "recovery"],
                          cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert init.returncode == 0
