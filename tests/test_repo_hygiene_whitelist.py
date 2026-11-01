import pytest
from herdr.clone_archive import BYPASS_IGNORED_DIRS, is_bypass_rel_path
from herdr.repo_hygiene import teardown_output_diagnosis


def test_test_and_coverage_directories_in_bypass_whitelist():
    expected_new_dirs = {
        "test-evidence",
        "test-results",
        "playwright-report",
        "coverage",
        ".coverage",
        "htmlcov",
    }
    assert expected_new_dirs.issubset(BYPASS_IGNORED_DIRS)


@pytest.mark.parametrize("rel_path", [
    "test-evidence",
    "test-evidence/junit.xml",
    "test-evidence/screenshots/failure.png",
    "test-results",
    "test-results/summary.json",
    "playwright-report",
    "playwright-report/index.html",
    "coverage",
    "coverage/lcov.info",
    ".coverage",
    "htmlcov",
    "htmlcov/index.html",
])
def test_is_bypass_rel_path_recognizes_test_artifacts(rel_path):
    assert is_bypass_rel_path(rel_path) is True


def test_teardown_output_diagnosis_ignores_untracked_test_artifacts():
    porcelain_test_artifacts_only = (
        "?? test-evidence/\n"
        "?? test-evidence/junit.xml\n"
        "?? test-evidence/trace.zip\n"
        "?? test-results/\n"
        "?? test-results/output.json\n"
        "?? playwright-report/\n"
        "?? playwright-report/index.html\n"
        "?? coverage/\n"
        "?? .coverage\n"
        "?? htmlcov/\n"
    )
    diag = teardown_output_diagnosis(porcelain_test_artifacts_only, task_id="task-test-clean")
    assert diag["reason"] == ""
    assert diag["blocked_files"] == []
    assert diag["blocked_total"] == 0


def test_teardown_output_diagnosis_surfaces_real_files_alongside_test_artifacts():
    porcelain_mixed = (
        "?? test-evidence/junit.xml\n"
        "?? src/critical_fix.py\n"
        "?? playwright-report/\n"
    )
    diag = teardown_output_diagnosis(porcelain_mixed, task_id="task-test-mixed")
    assert diag["reason"] == "unpreserved_worktree_output"
    assert "src/critical_fix.py" in diag["blocked_files"]
    assert "test-evidence/junit.xml" not in diag["blocked_files"]
    assert "playwright-report/" not in diag["blocked_files"]
    assert diag["blocked_total"] == 1


def test_teardown_output_diagnosis_blocks_tracked_modifications_under_test_dirs():
    porcelain_tracked = (
        "M  test-evidence/tracked_fixture.json\n"
        "?? test-evidence/untracked_evidence.xml\n"
    )
    diag = teardown_output_diagnosis(porcelain_tracked, task_id="task-test-tracked")
    assert diag["reason"] == "unpreserved_worktree_output"
    assert "test-evidence/tracked_fixture.json" in diag["blocked_files"]
    assert "test-evidence/untracked_evidence.xml" not in diag["blocked_files"]
