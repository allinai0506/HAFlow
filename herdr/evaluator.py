#!/opt/homebrew/bin/python3
"""Herdr Autonomous Evaluation Engine for Agentic Loops.

Implements the 5-element paradigm (Goal, Metrics, Data, Markdown files, Cron):
- Computes 5-dimensional quantitative metric vector:
  (correctness, quality, scope, repro, convergence)
- Parses test runners (pytest, vitest, jest, etc.) and linter/typecheck outputs
- Generates structured Markdown reports (METRICS.md, EVALUATION.md, STATE.md)
"""

import hashlib
import json
import os
import re
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from herdr.git_coordination import GitOperationLock
from herdr.projection import strip_ansi_codes

LOOP_DIR_NAME = ".herdr-loop"
BASELINE_LINT_FILENAME = "BASELINE_LINT.json"
BASELINE_TEST_FILENAME = "BASELINE_TEST.json"



class EvaluationBusyError(RuntimeError):
    """Another process owns this loop's mutable evaluation artifacts."""


@contextmanager
def evaluation_lock(loop_dir: Path):
    # Reuse the existing kernel file-lock implementation, in a loop-local
    # namespace independent of repository Git operations and production state.
    loop_dir = Path(loop_dir)
    operation = GitOperationLock(loop_dir, lock_root=loop_dir / ".locks")
    if not operation.try_acquire():
        raise EvaluationBusyError(f"Evaluation namespace already active: {loop_dir}")
    try:
        yield
    finally:
        operation.release()


@contextmanager
def _owned_evaluation_signals():
    # Baseline capture runs in herdr-task as well as herdr-loop; scope the
    # catchable termination handler to this owned command, not the whole CLI.
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _signal_owned_group(process, signum):
    # start_new_session below makes this PID the owned process-group identity.
    # Never signal the caller's group or discover unrelated processes by name.
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Some hosts report EPERM for a group that has just disappeared.
        # Ignore only a verified empty/zombie-only group, never live denial.
        listing = subprocess.run(
            ["ps", "-axo", "pgid=,stat="], capture_output=True, text=True,
            timeout=1, check=True,
        )
        members = [row.split()[1] for row in listing.stdout.splitlines()
                   if len(row.split()) == 2 and row.split()[0] == str(process.pid)]
        if any(not state.startswith("Z") for state in members):
            raise


def _stop_owned_runner(process):
    _signal_owned_group(process, signal.SIGTERM)
    try:
        return process.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        _signal_owned_group(process, signal.SIGKILL)
        return process.communicate(timeout=1)
    finally:
        # Children may redirect their pipes and survive their already-reaped
        # parent. Terminate any remaining members before releasing ownership.
        _signal_owned_group(process, signal.SIGKILL)


def run_evaluation_command(command: List[str], cwd: Path, timeout: float) -> Tuple[int, str, str]:
    """Run one owned command; return exit/stdout/stderr after group cleanup.

    Timeout returns 124 and observed output. Catchable termination unwinds.
    The caller owns the artifact lock; this helper does not publish artifacts.
    """
    with _owned_evaluation_signals():
        process = subprocess.Popen(
            command, cwd=str(cwd), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        cleaned = False
        try:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
                return process.returncode, stdout, stderr
            except subprocess.TimeoutExpired:
                stdout, stderr = _stop_owned_runner(process)
                cleaned = True
                return 124, stdout, stderr
        finally:
            # Also covers normal shell return with background children and Python
            # exceptions/interrupts. Cleanup stays inside the evaluation lock.
            if not cleaned:
                _stop_owned_runner(process)


def effective_defects(current: int, baseline: int) -> int:
    """New defects introduced in this loop (never negative).

    Baseline debt is transparent but non-blocking; only the delta gates.
    """
    try:
        cur = int(current or 0)
    except (TypeError, ValueError):
        cur = 0
    try:
        base = int(baseline or 0)
    except (TypeError, ValueError):
        base = 0
    return max(0, cur - max(0, base))


def write_baseline_lint(loop_dir: Path, lint_errors: int, type_errors: int = 0) -> Path:
    with evaluation_lock(loop_dir):
        return _write_baseline_lint_unlocked(loop_dir, lint_errors, type_errors)


def capture_lint_baseline(loop_dir: Path, command: str, cwd: Path) -> Path:
    """Own pre-edit lint execution and baseline publication as one lifecycle."""
    with evaluation_lock(loop_dir):
        return _capture_lint_baseline_unlocked(loop_dir, command, cwd)


def _capture_lint_baseline_unlocked(loop_dir: Path, command: str, cwd: Path) -> Path:
    exit_code, stdout, stderr = run_evaluation_command(
        ["/bin/sh", "-c", command], cwd, timeout=120,
    )
    if exit_code == 124:
        raise TimeoutError("Lint baseline capture timed out after 120 seconds")
    if static_check_execution_failed(exit_code):
        raise RuntimeError(f"Lint baseline command failed to execute: exit {exit_code}")
    lint_errors = parse_lint_output(stdout + stderr, exit_code)
    return _write_baseline_lint_unlocked(loop_dir, lint_errors, 0)


def _write_baseline_lint_unlocked(loop_dir: Path, lint_errors: int, type_errors: int = 0) -> Path:
    """Persist pre-edit lint baseline once at loop init (best-effort)."""
    loop_dir = Path(loop_dir)
    loop_dir.mkdir(parents=True, exist_ok=True)
    path = loop_dir / BASELINE_LINT_FILENAME
    payload = {
        "lint_errors": int(lint_errors or 0),
        "type_errors": int(type_errors or 0),
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp_path = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)
    return path


def read_baseline_lint(loop_dir: Path) -> Tuple[int, int]:
    """Return (baseline_lint, baseline_type); (0, 0) when absent/corrupt."""
    try:
        data = json.loads((Path(loop_dir) / BASELINE_LINT_FILENAME).read_text(encoding="utf-8"))
    except Exception:
        return 0, 0
    if not isinstance(data, dict):
        return 0, 0
    try:
        lint_errors = int(data.get("lint_errors") or 0)
    except (TypeError, ValueError):
        lint_errors = 0
    try:
        type_errors = int(data.get("type_errors") or 0)
    except (TypeError, ValueError):
        type_errors = 0
    return max(0, lint_errors), max(0, type_errors)


def read_baseline_test(loop_dir: Path) -> List[str]:
    """Return list of failing test names from BASELINE_TEST.json; [] when absent/corrupt."""
    try:
        data = json.loads((Path(loop_dir) / BASELINE_TEST_FILENAME).read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    failing = data.get("failing_tests")
    if not isinstance(failing, list):
        return []
    return [str(t).strip() for t in failing if str(t).strip()]



def get_loop_dir(base_dir: Path) -> Path:
    return Path(base_dir) / LOOP_DIR_NAME


def read_repro_requirement(goal_contract: str) -> bool:
    """Read typed producer metadata, or an unambiguous legacy config section.

    Free goal/acceptance text is never configuration. New producer metadata
    is appended last, after every raw command literal, so body examples cannot
    override it. Ambiguous old multiline display contracts require re-init.
    """
    prefix = "<!-- HERDR_REPRO_REQUIRED: "
    lines = goal_contract.rstrip().splitlines()
    last = lines[-1] if lines else ""
    if last.startswith(prefix):
        if last == prefix + "true -->":
            return True
        if last == prefix + "false -->":
            return False
        raise ValueError("Invalid typed repro requirement")
    sections = goal_contract.split("\n## 评估指令配置\n")
    if len(sections) != 2:
        raise ValueError("Missing or ambiguous legacy configuration section")
    configuration = sections[1]
    fields = {}
    for line in configuration.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"- \*\*(测试命令|代码质量|靶向复现用例)\*\*: `(.*)`", line)
        if match is None or match.group(1) in fields:
            raise ValueError("Ambiguous legacy configuration section")
        fields[match.group(1)] = match.group(2)
    if not {"测试命令", "代码质量"}.issubset(fields):
        raise ValueError("Incomplete legacy configuration section")
    return "靶向复现用例" in fields


def select_default_test_command(markers: Dict[str, bool]) -> Dict[str, str]:
    """Choose the auto-init test command from repo stack markers (C05b).

    Explicit ``--test-cmd`` contracts are decided by the caller and never
    reach this function.  When the caller would have to guess, ambiguity is
    a recoverable refusal — never a silent default: a root ``package.json``
    outranking everything else launched frontend tests against backend tasks
    in multi-stack repos, and inventing a Java default would do the same in
    reverse.  An empty marker set keeps the historical ``pytest`` fallback.

    Strong markers name a stack on their own.  A bare ``tests/`` directory
    is a weak python signal: JS and Rust repos routinely carry one, so it
    only forms a python stack when no strong marker is present (matching
    the pre-C05b first-match order, where ``package.json`` was checked
    first).
    """
    stack_files: Dict[str, List[str]] = {}
    if markers.get("package_json"):
        stack_files.setdefault("javascript", []).append("package.json")
    if markers.get("cargo_toml"):
        stack_files.setdefault("rust", []).append("Cargo.toml")
    if markers.get("pytest_ini"):
        stack_files.setdefault("python", []).append("pytest.ini")
    java_files = [
        label for name, label in
        (("pom_xml", "pom.xml"), ("build_gradle", "build.gradle"))
        if markers.get(name)
    ]
    if java_files:
        stack_files.setdefault("java", []).extend(java_files)
    if markers.get("tests_dir") and not stack_files:
        stack_files.setdefault("python", []).append("tests/")

    if len(stack_files) > 1:
        detected = "; ".join(
            f"{stack} [{', '.join(files)}]"
            for stack, files in sorted(stack_files.items())
        )
        return {
            "refused": "multi_stack_ambiguous",
            "detail": (
                f"multiple stacks detected at repo root: {detected}; "
                "relaunch with an explicit --test-cmd for the task's stack"
            ),
        }
    if "java" in stack_files:
        return {
            "refused": "java_without_explicit_contract",
            "detail": (
                "no trusted default test command for Java; "
                "relaunch with an explicit --test-cmd"
            ),
        }
    if "javascript" in stack_files:
        return {"command": "CI=1 npm test"}
    if "python" in stack_files:
        return {"command": "pytest"}
    if "rust" in stack_files:
        return {"command": "cargo test"}
    return {"command": "pytest"}


def init_loop(
    target_dir: Path,
    goal: str,
    acceptance: str = "",
    test_cmd: str = "",
    lint_cmd: str = "",
    max_iterations: int = 5,
    repro_cmd: str = "",
    capture_baseline: bool = False,
) -> Path:
    """Own contract replacement and optional baseline capture as one lifecycle."""
    with evaluation_lock(get_loop_dir(target_dir)):
        loop_dir = _init_loop_unlocked(
            target_dir=target_dir, goal=goal, acceptance=acceptance,
            test_cmd=test_cmd, lint_cmd=lint_cmd, max_iterations=max_iterations,
            repro_cmd=repro_cmd,
        )
        command = (lint_cmd or "").strip()
        if capture_baseline and command and command != "true":
            _capture_lint_baseline_unlocked(loop_dir, command, Path(target_dir))
        return loop_dir


def _init_loop_unlocked(
    target_dir: Path,
    goal: str,
    acceptance: str = "",
    test_cmd: str = "",
    lint_cmd: str = "",
    max_iterations: int = 5,
    repro_cmd: str = "",
) -> Path:
    """Initialize .herdr-loop directory structure and contracts."""
    loop_dir = get_loop_dir(target_dir)
    loop_dir.mkdir(parents=True, exist_ok=True)

    # EVAL_DONE is the reader's single authoritative execution snapshot.
    # Archive the old receipt before invalidating it. If publication fails,
    # the unchanged old contract keeps its recoverable current receipt. Once
    # archived, invalidate before replacing inputs so failed new initialization
    # cannot expose old success as evidence for the new contract.
    snapshot_path = loop_dir / "EVAL_DONE.json"
    previous_snapshot = snapshot_path.read_bytes() if snapshot_path.exists() else None
    if previous_snapshot is not None:
        # Historical receipt, never consulted as the current execution view.
        history = loop_dir / "history"
        history.mkdir(exist_ok=True)
        receipt_sha = hashlib.sha256(previous_snapshot).hexdigest()
        receipt = history / f"EVAL_DONE-{receipt_sha}.json"
        if not receipt.exists():
            receipt_tmp = receipt.with_suffix(f".json.tmp.{os.getpid()}")
            receipt_tmp.write_bytes(previous_snapshot)
            receipt_tmp.replace(receipt)
        elif receipt.read_bytes() != previous_snapshot:
            raise RuntimeError("Conflicting historical evaluation receipt")

    reset_path = snapshot_path.with_suffix(f".json.tmp.{os.getpid()}")
    reset_path.write_text(json.dumps({
        "iteration": 0, "completed_at": None, "status": "initialized",
        "converged": False, "max_iterations": max_iterations,
        "total_tests": 0, "passed_tests": 0, "failing_tests": [],
        "composite_score": 0.0,
    }), encoding="utf-8")
    reset_path.replace(snapshot_path)

    # Debt belongs to the previous contract until a new capture succeeds.
    # An absent baseline has the existing conservative zero-debt semantics.
    (loop_dir / BASELINE_LINT_FILENAME).unlink(missing_ok=True)

    # 1. Write GOAL.md
    goal_md = loop_dir / "GOAL.md"
    goal_content = f"""# 任务目标契约 (Node Goal Contract)

- **初始化时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}
- **最大循环轮次**: {max_iterations}

## 核心目标
{goal.strip()}

## 验收标准 (DoD)
{acceptance.strip() or '- [ ] 所有测试 100% 绿灯\n- [ ] 零 Lint / 语法错误'}

## 评估指令配置
- **测试命令**: `{test_cmd or 'pytest'}`
- **代码质量**: `{lint_cmd or 'true'}`
"""
    if repro_cmd:
        goal_content += f"- **靶向复现用例**: `{repro_cmd}`\n"
    goal_content += f"<!-- HERDR_REPRO_REQUIRED: {str(bool(repro_cmd)).lower()} -->\n"
    goal_md.write_text(goal_content, encoding="utf-8")

    # 2. Write EVALUATOR.sh
    repro_block = ""
    if repro_cmd:
        repro_block = f"""
echo "=== [3/3] RUNNING REPRO CASE ==="
(
{repro_cmd}
) > "$LOG_DIR/repro.log" 2>&1
REPRO_EXIT=$?
echo "REPRO_EXIT=$REPRO_EXIT"
"""

    evaluator_sh = loop_dir / "EVALUATOR.sh"
    evaluator_content = f"""#!/usr/bin/env bash
# Herdr Evaluation Runner
# Output streams are parsed into quantitative metrics.

set -o pipefail

ROOT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")/.." && pwd)"
cd "$ROOT_DIR" || exit 1

LOG_DIR="$ROOT_DIR/{LOOP_DIR_NAME}/logs"
mkdir -p "$LOG_DIR"

echo "=== [1/3] RUNNING TESTS ==="
(
{test_cmd or 'pytest'}
) > "$LOG_DIR/test.log" 2>&1
TEST_EXIT=$?
echo "TEST_EXIT=$TEST_EXIT"

echo "=== [2/3] RUNNING LINT / QUALITY ==="
(
{lint_cmd or 'true'}
) > "$LOG_DIR/lint.log" 2>&1
LINT_EXIT=$?
echo "LINT_EXIT=$LINT_EXIT"
{repro_block}
exit 0
"""
    evaluator_sh.write_text(evaluator_content, encoding="utf-8")
    evaluator_sh.chmod(0o755)

    # 3. Write STATE.md
    state_file = loop_dir / "STATE.md"
    state_file.write_text(f"""# 循环执行状态 (Loop State)

- **iteration**: 0
- **max_iterations**: {max_iterations}
- **status**: initialized
- **last_updated**: {time.strftime('%Y-%m-%d %H:%M:%S')}
- **converged**: false
""", encoding="utf-8")

    # 4. Write initial placeholder METRICS.json
    metrics_json = loop_dir / "METRICS.json"
    init_metrics = MetricVector(
        correctness=0.0,
        quality=0.0,
        scope=100.0,
        repro=0.0 if repro_cmd else 100.0,
        composite_score=0.0,
        has_repro_test=bool(repro_cmd),
    )
    metrics_json.write_text(json.dumps(init_metrics.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    # 5. Write initial METRICS.md and EVALUATION.md
    (loop_dir / "METRICS.md").write_text(render_metrics_markdown(init_metrics, 0, max_iterations), encoding="utf-8")
    (loop_dir / "EVALUATION.md").write_text("# 评估器就绪\n\n请开始执行实现工作。执行后运行 `herdr-loop eval` 进行评分。\n", encoding="utf-8")
    return loop_dir


def read_state(loop_dir: Path) -> dict:
    state_file = loop_dir / "STATE.md"
    if not state_file.exists():
        return {"iteration": 0, "max_iterations": 5, "status": "unknown", "converged": False}
    text = state_file.read_text(encoding="utf-8")
    res = {}
    for line in text.splitlines():
        if line.startswith("- **") and "**:" in line:
            k = line.split("**")[1].strip()
            v = line.split("**:", 1)[1].strip()
            if v.isdigit():
                res[k] = int(v)
            elif v.lower() == "true":
                res[k] = True
            elif v.lower() == "false":
                res[k] = False
            else:
                res[k] = v
    return res


def write_state(loop_dir: Path, iteration: int, max_iter: int, status: str, converged: bool) -> None:
    state_file = loop_dir / "STATE.md"
    state_file.write_text(f"""# 循环执行状态 (Loop State)

- **iteration**: {iteration}
- **max_iterations**: {max_iter}
- **status**: {status}
- **last_updated**: {time.strftime('%Y-%m-%d %H:%M:%S')}
- **converged**: {'true' if converged else 'false'}
""", encoding="utf-8")



@dataclass
class MetricVector:
    correctness: float = 100.0   # 0.0 - 100.0 (test pass rate)
    quality: float = 100.0       # 0.0 - 100.0 (lint, typecheck, static analysis)
    scope: float = 100.0         # 0.0 - 100.0 (boundary adherence, no unneeded edits)
    repro: float = 100.0         # 0.0 or 100.0 (reproduction/blocker test status)
    composite_score: float = 100.0
    passed_tests: int = 0
    total_tests: int = 0
    failing_tests: List[str] = field(default_factory=list)
    lint_errors: int = 0
    type_errors: int = 0
    has_repro_test: bool = False
    baseline_lint_errors: int = 0
    baseline_type_errors: int = 0
    new_lint_errors: Optional[int] = None
    new_type_errors: Optional[int] = None
    baseline_failing_tests: List[str] = field(default_factory=list)
    new_failing_tests: Optional[List[str]] = None
    details: Dict[str, Any] = field(default_factory=dict)
    business_acceptance: str = 'unknown'  # Generic metrics never prove a business gate.

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# vitest/Jest 用行首标记区分通过与失败（✓/√ 通过，✕ 失败）。
# 子串匹配会误判：用例名本身可能直写 "FAIL "/"✕"（例如中文用例名里
# 写 "...展示 FAIL 状态..."），此时一条绿色用例会被当成失败项，使
# failing_tests 非空、内循环永不收敛，把满分 100 的绿色套件压到 95 分
# 并最终误报 inner_loop_exhausted。故必须先排除带通过标记的行。
_PASS_MARKERS = ("✓", "√")


def _is_failing_test_line(line: str) -> bool:
    """True only for test lines that actually denote a failure."""
    stripped = line.strip()
    if not stripped or stripped.startswith(_PASS_MARKERS):
        return False
    return "✕" in line or "FAIL " in line


_SUREFIRE_RE = re.compile(
    r"Tests run:\s*(\d+)"
    r"(?:,\s*Failures:\s*(\d+))?"
    r"(?:,\s*Errors:\s*(\d+))?"
    r"(?:,\s*Skipped:\s*(\d+))?"
)


def parse_test_output(output: str, exit_code: int) -> Tuple[int, int, List[str]]:
    """Extract passed, total, and failing test names from test output.

    Supports pytest, vitest, jest, Maven Surefire/JUnit, and generic runners.
    Every recognizable summary in the log is aggregated instead of
    first-match-wins: a combined frontend(vitest)+backend(surefire) run must
    count both sides, otherwise JUnit parameterized instances are invisible
    to the managed metrics and frontend counts get mistaken for the whole
    increment. Returns: (passed_count, total_count, failing_tests_list)
    """
    # Normalize only the parser view; the runner keeps original log bytes.
    output = strip_ansi_codes(output)
    failing: List[str] = []
    passed_sum = 0
    total_sum = 0
    matched = False

    # 1. pytest summaries: "1 failed, 4 passed in 0.15s" (one per invocation)
    pytest_matched = False
    for m in re.finditer(r"(?:=+)?\s*([\d\w\s,]+)\s+in\s+[\d\.]+s", output):
        summary_str = m.group(1)
        passed_m = re.search(r"(\d+)\s+passed", summary_str)
        failed_m = re.search(r"(\d+)\s+failed", summary_str)
        error_m = re.search(r"(\d+)\s+error", summary_str)
        if not (passed_m or failed_m or error_m):
            continue
        matched = True
        pytest_matched = True
        passed = int(passed_m.group(1)) if passed_m else 0
        failed = int(failed_m.group(1)) if failed_m else 0
        errors = int(error_m.group(1)) if error_m else 0
        passed_sum += passed
        total_sum += passed + failed + errors
        for line in output.splitlines():
            if line.startswith("FAILED ") or line.startswith("ERROR "):
                parts = line.split(maxsplit=1)
                if len(parts) > 1:
                    target = parts[1].split(" - ")[0].strip()
                    if target not in failing:
                        failing.append(target)


    # 2. vitest summaries: "Tests  1 failed | 11 passed (12)"
    vitest_matched = False
    for m in re.finditer(
            r"Tests\s+(?:(\d+)\s+failed\s*\|\s*)?(?:(\d+)\s+passed\s*)?\((\d+)\)",
            output):
        vitest_matched = True
        matched = True
        failed = int(m.group(1)) if m.group(1) else 0
        passed = int(m.group(2)) if m.group(2) else 0
        total = int(m.group(3)) if m.group(3) else (passed + failed)
        passed_sum += passed
        total_sum += total
    if vitest_matched:
        for line in output.splitlines():
            if _is_failing_test_line(line):
                cleaned = line.replace("FAIL", "").replace("✕", "").strip()
                if cleaned and cleaned not in failing:
                    failing.append(cleaned)

    # 3. jest summaries: "Tests: X failed, Y passed, Z total"
    for m in re.finditer(
            r"Tests:\s*(?:(\d+)\s+failed,\s*)?(?:(\d+)\s+passed,\s*)?(\d+)\s+total",
            output):
        matched = True
        failed = int(m.group(1)) if m.group(1) else 0
        passed = int(m.group(2)) if m.group(2) else 0
        total = int(m.group(3)) if m.group(3) else (passed + failed)
        passed_sum += passed
        total_sum += total
        for line in output.splitlines():
            if _is_failing_test_line(line):
                cleaned = line.replace("FAIL", "").replace("✕", "").strip()
                if cleaned and cleaned not in failing:
                    failing.append(cleaned)

    # 4. Maven Surefire / JUnit totals: "Tests run: N, Failures: F, Errors: E,
    # Skipped: S". Per-class lines carry "Time elapsed" and are excluded:
    # summing them against the Results totals would double count. Surefire's
    # run count includes skipped, so passed = run - failures - errors - skipped.
    for line in output.splitlines():
        m = _SUREFIRE_RE.search(line)
        if not m or "Time elapsed" in line:
            continue
        matched = True
        run = int(m.group(1))
        failures = int(m.group(2) or 0)
        errors = int(m.group(3) or 0)
        skipped = int(m.group(4) or 0)
        passed_sum += max(0, run - failures - errors - skipped)
        total_sum += run

    # 5. Python standard unittest: "Ran X test(s) in Ys" (+OK / failures=N)
    unittest_segments = list(re.finditer(r"Ran\s+(\d+)\s+tests?\s+in\s+[\d\.]+s", output))
    if unittest_segments:
        matched = True
        ut_total = sum(int(m.group(1)) for m in unittest_segments)
        ut_ok = exit_code == 0 and ("\nOK" in output or output.endswith("OK"))
        if ut_ok:
            passed_sum += ut_total
            total_sum += ut_total
        else:
            failed_cnt = 0
            fail_m = re.search(r"failures=(\d+)", output)
            err_m = re.search(r"errors=(\d+)", output)
            if fail_m:
                failed_cnt += int(fail_m.group(1))
            if err_m:
                failed_cnt += int(err_m.group(1))
            if failed_cnt == 0:
                failed_cnt = 1
            passed_sum += max(0, ut_total - failed_cnt)
            total_sum += ut_total
            for line in output.splitlines():
                if line.startswith("FAIL: ") or line.startswith("ERROR: "):
                    target = line.split(maxsplit=1)[1].strip()
                    if target not in failing:
                        failing.append(target)

    # 6. Fallback runner: pytest short summary without timing (e.g. "4248 passed" or "= 1 failed, 4 passed =")
    # Only evaluated if none of the explicit multi-runner summaries above matched.
    if not matched:
        for line in output.splitlines():
            line_clean = line.strip(" =\t")
            if line_clean.startswith("Test Files") or line_clean.startswith("Tests") or line_clean.startswith("[INFO]"):
                continue
            passed_m = re.search(r"\b(\d+)\s+passed\b", line_clean)
            failed_m = re.search(r"\b(\d+)\s+failed\b", line_clean)
            error_m = re.search(r"\b(\d+)\s+error\b", line_clean)
            if passed_m or failed_m or error_m:
                matched = True
                passed = int(passed_m.group(1)) if passed_m else 0
                failed = int(failed_m.group(1)) if failed_m else 0
                errors = int(error_m.group(1)) if error_m else 0
                passed_sum += passed
                total_sum += passed + failed + errors
                break

    if matched:
        return passed_sum, max(total_sum, 1 if exit_code != 0 else 0), failing

    # 5. Fallback: generic exit code
    # If no recognizable test summary was found, do not fabricate a passing count.
    # Empty output with exit 0 means no evidence of any test running.
    output_stripped = output.strip()
    if exit_code == 0:
        if not output_stripped:
            return 0, 0, []
        # Non-empty but unrecognizable output with exit 0: treat as failure
        # (likely a test that produced no recognizable summary)
        return 0, 1, [output_stripped[:200]]
    else:
        # Non-zero exit: extract probable failure line
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        err_msg = lines[-1] if lines else "Process failed with non-zero exit code"
        if err_msg.startswith("FAILED ") or err_msg.startswith("ERROR "):
            parts = err_msg.split(maxsplit=1)
            if len(parts) > 1:
                err_msg = parts[1].split(" - ")[0].strip()
        return 0, 1, [err_msg]


def static_check_execution_failed(exit_code: int) -> bool:
    """Reserved timeout/shell/signal exits are not static-analysis debt.

    Ordinary tool defect exits (including TypeScript's 2) retain the existing
    baseline contract. The shell reserves 126/127 for execution failures and
    reports signal termination as 128+signal; the owned runner uses 124 timeout.
    """
    return exit_code < 0 or exit_code in (124, 126, 127) or exit_code >= 128


def parse_lint_output(output: str, exit_code: int) -> int:
    """Parse linter / typecheck error count from output."""
    if exit_code == 0:
        return 0
        
    # Check ESLint format: "X problems (Y errors, Z warnings)"
    eslint_match = re.search(r"(\d+)\s+problems?\s*\((\d+)\s+errors?", output)
    if eslint_match:
        return int(eslint_match.group(2))
        
    # Check TypeScript tsc format: "Found X errors"
    tsc_match = re.search(r"Found\s+(\d+)\s+errors?", output)
    if tsc_match:
        return int(tsc_match.group(1))

    # Count lines containing "error:" or "Error:"
    count = 0
    for line in output.splitlines():
        if re.search(r"\b(?:error|Error|ERROR)\b", line) and not re.search(r"\b(?:0 error|0 errors)\b", line):
            count += 1
            
    if count == 0 and exit_code != 0:
        count = 1
    return count


def calculate_metrics(
    test_output: str,
    test_exit_code: int,
    lint_output: str = "",
    lint_exit_code: int = 0,
    type_output: str = "",
    type_exit_code: int = 0,
    repro_output: Optional[str] = None,
    repro_exit_code: Optional[int] = None,
    modified_files: Optional[List[str]] = None,
    allowed_patterns: Optional[List[str]] = None,
    baseline_lint_errors: int = 0,
    baseline_type_errors: int = 0,
    evaluation_exit_code: int = 0,
    evaluation_errors: Optional[List[str]] = None,
    baseline_failing_tests: Optional[List[str]] = None,
) -> MetricVector:
    """Compute the 5-dimensional metric vector from execution outputs."""
    passed, total, failing = parse_test_output(test_output, test_exit_code)
    correctness = (float(passed) / float(total) * 100.0) if total > 0 else (100.0 if test_exit_code == 0 else 0.0)

    # Differential testing: pre-existing test failures on base branch are ignored
    # when computing delta failures and zero-defect gate convergence.
    if baseline_failing_tests is not None:
        base_set = set(baseline_failing_tests)
        new_failing = [t for t in failing if t not in base_set]
        pre_existing_ignored = (len(failing) > 0 and len(new_failing) == 0 and test_exit_code != 0)
        has_test_defects = bool(new_failing) or (test_exit_code != 0 and not pre_existing_ignored)
        # If all failures are pre-existing, correctness score reflects pre-existing debt tolerance
        if pre_existing_ignored and total > 0:
            correctness = 100.0
    else:
        new_failing = None
        pre_existing_ignored = False
        has_test_defects = (test_exit_code != 0 or bool(failing))


    lint_errs = parse_lint_output(lint_output, lint_exit_code)
    type_errs = parse_lint_output(type_output, type_exit_code)

    # Baseline-aware quality: pre-existing debt is recorded but only new
    # defects penalize quality and gate convergence.
    try:
        baseline_lint = max(0, int(baseline_lint_errors or 0))
    except (TypeError, ValueError):
        baseline_lint = 0
    try:
        baseline_type = max(0, int(baseline_type_errors or 0))
    except (TypeError, ValueError):
        baseline_type = 0
    new_lint = effective_defects(lint_errs, baseline_lint)
    new_type = effective_defects(type_errs, baseline_type)

    # Quality penalty: 10 points per NEW lint error, 15 per NEW type error
    quality_penalty = (new_lint * 10.0) + (new_type * 15.0)
    quality = max(0.0, 100.0 - quality_penalty)
    
    # Scope check: Check if files modified exceed allowed patterns
    scope = 100.0
    out_of_bounds = []
    if modified_files and allowed_patterns:
        for f in modified_files:
            matched = any(re.search(pat, f) for pat in allowed_patterns)
            if not matched:
                out_of_bounds.append(f)
        if out_of_bounds:
            # Deduct 15 points per out-of-bounds file
            scope = max(0.0, 100.0 - (len(out_of_bounds) * 15.0))
            
    # Repro test: if present, must pass completely (100.0 or 0.0)
    has_repro = repro_output is not None
    if has_repro:
        repro_val = 100.0 if repro_exit_code == 0 else 0.0
    else:
        repro_val = 100.0

    # Calculate composite score
    if has_repro:
        weights = {"correctness": 0.35, "quality": 0.25, "scope": 0.10, "repro": 0.30}
    else:
        weights = {"correctness": 0.50, "quality": 0.35, "scope": 0.15, "repro": 0.00}
        
    composite = (
        weights["correctness"] * correctness +
        weights["quality"] * quality +
        weights["scope"] * scope +
        weights["repro"] * repro_val
    )
    
    static_execution_failed = (static_check_execution_failed(lint_exit_code) or
                               static_check_execution_failed(type_exit_code))

    # Absolute zero-defect rule: cannot score 100.0 if any NEW failures exist.
    # Pre-existing baseline debt is transparent in lint_errors/type_errors/failing_tests
    # but does not cap the score; only the delta gates.
    if (static_execution_failed or evaluation_exit_code != 0 or evaluation_errors or has_test_defects or new_lint > 0 or new_type > 0 or (has_repro and repro_val < 100.0) or out_of_bounds) and composite >= 100.0:
        composite = 95.0

    return MetricVector(
        correctness=round(correctness, 2),
        quality=round(quality, 2),
        scope=round(scope, 2),
        repro=round(repro_val, 2),
        composite_score=round(composite, 2),
        passed_tests=passed,
        total_tests=total,
        failing_tests=failing,
        lint_errors=lint_errs,
        type_errors=type_errs,
        has_repro_test=has_repro,
        baseline_lint_errors=baseline_lint,
        baseline_type_errors=baseline_type,
        new_lint_errors=new_lint,
        new_type_errors=new_type,
        baseline_failing_tests=list(baseline_failing_tests or []),
        new_failing_tests=new_failing,
        details={
            "out_of_bounds_files": out_of_bounds,
            "test_exit_code": test_exit_code,
            "coverage_scope": "selected_test_runner_only",
            "business_acceptance": "unknown",
            "evaluation_exit_code": evaluation_exit_code,
            "evaluation_errors": list(evaluation_errors or []),
            "lint_exit_code": lint_exit_code,
            "type_exit_code": type_exit_code,
            "repro_exit_code": repro_exit_code,
            "baseline_lint_errors": baseline_lint,
            "baseline_type_errors": baseline_type,
            "new_lint_errors": new_lint,
            "new_type_errors": new_type,
            "baseline_failing_tests": list(baseline_failing_tests or []),
            "new_failing_tests": new_failing,
            "pre_existing_ignored": pre_existing_ignored,
        }
    )


def is_converged(metrics: MetricVector) -> bool:
    """True if the configured generic scoring conditions are met."""
    if metrics.details.get("evaluation_exit_code", 0) != 0 or metrics.details.get("evaluation_errors"):
        return False
    if any(static_check_execution_failed(metrics.details.get(key, 0))
           for key in ("lint_exit_code", "type_exit_code")):
        return False
    if metrics.new_failing_tests is not None:
        if metrics.new_failing_tests:
            return False
        if metrics.details.get("test_exit_code", 0) != 0 and not metrics.details.get("pre_existing_ignored", False):
            return False
    else:
        if metrics.details.get("test_exit_code", 0) != 0:
            return False
        if metrics.failing_tests:
            return False

    if metrics.composite_score < 99.9:
        return False
    eff_lint = metrics.new_lint_errors if metrics.new_lint_errors is not None else metrics.lint_errors
    eff_type = metrics.new_type_errors if metrics.new_type_errors is not None else metrics.type_errors
    if eff_lint > 0 or eff_type > 0:
        return False
    if metrics.has_repro_test and metrics.repro < 99.9:
        return False
    return True


def render_metrics_markdown(metrics: MetricVector, iteration: int, max_iterations: int) -> str:
    """Render a human and agent-readable metrics table."""
    status_icon = "🟢 通用评分收敛 (CONVERGED)" if is_converged(metrics) else "🔴 需继续修复 (ITERATION NEEDED)"
    repro_line = f"| 靶向复现用例 (Repro) | `{metrics.repro}%` | `100.0%` | {'✅' if metrics.repro == 100 else '❌'} |" if metrics.has_repro_test else ""
    if metrics.new_lint_errors is not None or metrics.new_type_errors is not None:
        new_lint = metrics.new_lint_errors if metrics.new_lint_errors is not None else metrics.lint_errors
        new_type = metrics.new_type_errors if metrics.new_type_errors is not None else metrics.type_errors
        quality_cell = (
            f"`{metrics.quality}%` "
            f"(Lint: {metrics.lint_errors} "
            f"[baseline {metrics.baseline_lint_errors}, new {new_lint}], "
            f"Type: {metrics.type_errors} "
            f"[baseline {metrics.baseline_type_errors}, new {new_type}])"
        )
    else:
        quality_cell = f"`{metrics.quality}%` (Lint: {metrics.lint_errors}, Type: {metrics.type_errors})"

    return f"""# 量化评估指标卡 (Metrics Scorecard)

> 轮次: {iteration} / {max_iterations}  
> 状态: {status_icon}  
> 综合适应度得分: **{metrics.composite_score} / 100.0**

## 核心指标明细

| 指标维度 | 当前值 | 目标值 | 判定 |
|---|---|---|---|
| 所选测试通过率 (Correctness) | `{metrics.correctness}%` ({metrics.passed_tests}/{metrics.total_tests}) | `100.0%` | {'✅' if metrics.correctness == 100 else '❌'} |
| 代码质量 (Quality) | {quality_cell} | `100.0%` | {'✅' if metrics.quality == 100 else '❌'} |
| 边界控制 (Scope) | `{metrics.scope}%` | `100.0%` | {'✅' if metrics.scope == 100 else '❌'} |
{repro_line}

业务验收：unknown。通用评分只描述所选测试命令；验收以候选绑定的 checkpoint 回执为准。

*更新时间: {time.strftime('%Y-%m-%d %H:%M:%S')}*
""".strip()


def _execution_failure_block(metrics: MetricVector) -> str:
    errors = list(metrics.details.get("evaluation_errors") or [])
    exit_code = metrics.details.get("evaluation_exit_code", 0)
    exit_error = f"evaluator_exit:{exit_code}"
    if exit_code != 0 and exit_error not in errors:
        errors.append(exit_error)
    if not errors:
        return ""
    return "\n### 评估执行证据未满足\n" + "\n".join(f"- `{error}`" for error in errors) + "\n"


def render_evaluation_markdown(
    metrics: MetricVector,
    iteration: int,
    max_iterations: int,
    raw_error_snippet: str = ""
) -> str:
    """Render diagnostic evaluation feedback for the pane agent."""
    if is_converged(metrics):
        return f"""# 第 {iteration} 轮评估诊断报告 (Evaluation Succeeded)

✅ **所选命令满足通用评分条件。**
- 所选测试通过数：{metrics.passed_tests}/{metrics.total_tests}
- 质量/静态扫描：满足配置的评分条件

业务验收：unknown。业务标准以当前候选、Run 和 epoch 绑定的 checkpoint 回执为准。
本轮通用评分已收敛；提交与工单完成仍需各自的验收及授权依据。
""".strip()

    failing_list = "\n".join(f"- ❌ `{t}`" for t in metrics.failing_tests) or "- 无单测失败（可能是质量或静态分析未通过）"
    repro_block = ""
    if metrics.has_repro_test and metrics.repro < 99.9:
        repro_block = "\n### 靶向复现用例 (Repro Defect)\n- ❌ 阻断复现用例仍未通过（缺陷未完全解决）\n"

    if metrics.new_lint_errors is not None or metrics.new_type_errors is not None:
        new_lint = metrics.new_lint_errors if metrics.new_lint_errors is not None else metrics.lint_errors
        new_type = metrics.new_type_errors if metrics.new_type_errors is not None else metrics.type_errors
        lint_block = (
            f"- Lint 错误数: `{metrics.lint_errors}` "
            f"(baseline {metrics.baseline_lint_errors}, new {new_lint})"
        )
        type_block = (
            f"- 类型检查错误数: `{metrics.type_errors}` "
            f"(baseline {metrics.baseline_type_errors}, new {new_type})"
        )
    else:
        lint_block = f"- Lint 错误数: `{metrics.lint_errors}`"
        type_block = f"- 类型检查错误数: `{metrics.type_errors}`"
    
    snippet_block = ""
    if raw_error_snippet.strip():
        snippet_block = f"""
## 报错上下文详情
```
{raw_error_snippet.strip()[:2000]}
```
"""

    return f"""# 第 {iteration} 轮评估诊断报告 (Evaluation Feedback)

> **综合得分**: `{metrics.composite_score} / 100.0` (未达到及格线 100.0)  
> **剩余循环轮次**: {max_iterations - iteration}

## 待修复阻断项 (Blockers)
{_execution_failure_block(metrics)}
### 1. 失败测试清单
{failing_list}
{repro_block}
### 2. 静态分析与类型检查
{lint_block}
{type_block}

### 3. 越界修改文件 (如有)
{', '.join(f'`{f}`' for f in metrics.details.get('out_of_bounds_files', [])) or '无（边界合规）'}
{snippet_block}
## 智能体行动指南
1. 仔细阅读上方失败用例与报错上下文；
2. 仅修改与当前目标相关的代码文件；
3. 保存代码后，等待评估器重新评分，直至综合得分达到 100.0。
""".strip()


def generate_blocker_report(loop_dir: Path, metrics: MetricVector, iteration: int, max_iter: int) -> Path:
    """Generate BLOCKER.md escalation report when inner loop exhausts all retries.

    Returns the path to the written BLOCKER.md file.
    Called automatically by herdr-loop when STATE.md status == 'exhausted'.
    """
    failing_list = "\n".join(f"- `{t}`" for t in metrics.failing_tests) or \
        "- (测试框架无具体失败用例名，请查看 logs/test.log)"

    repro_status = "✅ 通过 / 无复现用例"
    if metrics.has_repro_test and metrics.repro < 99.9:
        repro_status = "❌ 未通过"

    if metrics.new_lint_errors is not None or metrics.new_type_errors is not None:
        new_lint = metrics.new_lint_errors if metrics.new_lint_errors is not None else metrics.lint_errors
        new_type = metrics.new_type_errors if metrics.new_type_errors is not None else metrics.type_errors
        lint_line = (
            f"- Lint 错误数: `{metrics.lint_errors}` "
            f"(baseline {metrics.baseline_lint_errors}, new {new_lint})"
        )
        type_line = (
            f"- 类型检查错误数: `{metrics.type_errors}` "
            f"(baseline {metrics.baseline_type_errors}, new {new_type})"
        )
    else:
        lint_line = f"- Lint 错误数: `{metrics.lint_errors}`"
        type_line = f"- 类型检查错误数: `{metrics.type_errors}`"

    blocker_content = f"""# 工位求助单 (Escalation Blocker Report)

> **生成时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}  
> **状态**: 内循环已耗尽全部 {max_iter} 次重试，自愈失败  
> **综合得分**: `{metrics.composite_score} / 100.0`

## 当前阻断项
{_execution_failure_block(metrics)}
### 失败测试
{failing_list}

### 静态分析
{lint_line}
{type_line}

### 复现测试
- 状态: `{repro_status}`

## 自愈尝试记录
- 已执行 {iteration} 轮自检修复，达到最大上限 ({max_iter} 轮)
- 详细日志请查看 `.herdr-loop/logs/`

## 请求总指挥仲裁
工位已无法通过内部自愈解决以上问题，可能原因：
1. 外部依赖或环境配置问题（非代码本身）
2. 验收标准定义有歧义，需要总指挥重新明确
3. 需要更换 Agent 或调整策略

**Agent 操作**: 输出 `HERDR_TASK_BLOCKER:<task_id>` 信号并静默等待总指挥仲裁。
"""
    blocker_file = loop_dir / "BLOCKER.md"
    tmp = blocker_file.with_suffix(".md.tmp")
    tmp.write_text(blocker_content, encoding="utf-8")
    tmp.replace(blocker_file)
    return blocker_file
