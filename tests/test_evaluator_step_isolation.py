"""Evaluation commands must not leak shell state or bypass their logs."""
import subprocess

import pytest

from herdr.evaluator import init_loop


def evaluate_script(tmp_path, **commands):
    loop = init_loop(tmp_path, "independent logged steps", **commands)
    result = subprocess.run([str(loop / "EVALUATOR.sh")], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    return loop, result


@pytest.mark.parametrize("step", ["test", "lint", "repro"])
def test_compound_command_has_complete_step_log(tmp_path, step):
    command = "printf 'first command output\\n'; printf 'second output\\n' >&2"
    loop, result = evaluate_script(tmp_path, test_cmd="true", **{step + "_cmd": command}) if step != "test" else evaluate_script(tmp_path, test_cmd=command)
    assert result.returncode == 0
    log = (loop / "logs" / (step + ".log")).read_text()
    assert "first command output" in log
    assert "second output" in log
    assert "first command output" not in result.stdout
    assert "second output" not in result.stderr


def test_test_cwd_does_not_change_quality_cwd(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "root-only").write_text("root marker")
    loop, result = evaluate_script(tmp_path, test_cmd="cd nested && printf '5 passed in 0.1s\\n'", lint_cmd="test -f root-only")
    assert "TEST_EXIT=0" in result.stdout
    assert "LINT_EXIT=0" in result.stdout
    assert "5 passed" in (loop / "logs" / "test.log").read_text()


def test_command_exit_does_not_skip_later_steps_or_its_receipt(tmp_path):
    loop, result = evaluate_script(tmp_path, test_cmd="printf 'before exit\\n'; exit 7", lint_cmd="printf 'quality ran\\n'", repro_cmd="printf 'repro ran\\n'")
    assert result.returncode == 0
    assert "TEST_EXIT=7" in result.stdout
    assert "LINT_EXIT=0" in result.stdout
    assert "REPRO_EXIT=0" in result.stdout
    assert "quality ran" in (loop / "logs" / "lint.log").read_text()
    assert "repro ran" in (loop / "logs" / "repro.log").read_text()
