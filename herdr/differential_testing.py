r"""差量测试引擎 (Differential Testing Engine).

核心职责：
1. 实现 Base 分支存量失败用例快照提取与持久化；
2. 候选分支测试结果的差量比对算法：
   \Delta F = F_{candidate} \setminus F_{base}
3. 门禁判定：
   - 当 \Delta F = \emptyset 时，门禁判定为 pass（带有存量缺陷告警记录 pre_existing_ignored），
     不阻断工作流交付；
   - 当 \Delta F \neq \emptyset 时，门禁判定为 blocked，阻断交付并输出新增失败清单。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, List, Optional, Set

from herdr.evaluator import parse_test_output


@dataclass
class DifferentialTestResult:
    """差量测试比对与门禁判定结论。"""
    candidate_failures: List[str] = field(default_factory=list)
    base_failures: List[str] = field(default_factory=list)
    delta_failures: List[str] = field(default_factory=list)
    pre_existing_failures: List[str] = field(default_factory=list)
    fixed_failures: List[str] = field(default_factory=list)
    verdict: str = "pass"  # "pass" | "blocked"
    pre_existing_ignored: bool = False
    warning: Optional[str] = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_differential_verdict(
    candidate_failures: Iterable[str],
    base_failures: Iterable[str],
) -> DifferentialTestResult:
    r"""计算候选分支相对于 Base 分支的失败差量并给出门禁结论。

    算法：
    \Delta F = F_{candidate} \setminus F_{base}
    若 \Delta F 为空集，则判定通过（若有存量失败则标注 pre_existing_ignored 告警）；
    若 \Delta F 非空，则判定 blocked。
    """
    f_cand: Set[str] = {str(s).strip() for s in (candidate_failures or []) if str(s).strip()}
    f_base: Set[str] = {str(s).strip() for s in (base_failures or []) if str(s).strip()}

    delta_f: Set[str] = f_cand - f_base
    pre_existing: Set[str] = f_cand & f_base
    fixed: Set[str] = f_base - f_cand

    if not delta_f:
        # 候选分支未引入任何新增失败用例
        has_pre_existing = bool(pre_existing)
        verdict = "pass"
        warning = "pre_existing_ignored" if has_pre_existing else None
        if has_pre_existing:
            message = (
                f"Differential gate PASS: 0 new failures introduced "
                f"({len(pre_existing)} pre-existing failures ignored)"
            )
        else:
            message = "Differential gate PASS: all tests passed (clean)"
        pre_existing_ignored = has_pre_existing
    else:
        # 候选分支引入了新增失败用例
        verdict = "blocked"
        warning = None
        pre_existing_ignored = False
        message = (
            f"Differential gate BLOCKED: {len(delta_f)} new failures introduced: "
            f"{', '.join(sorted(delta_f))}"
        )

    return DifferentialTestResult(
        candidate_failures=sorted(f_cand),
        base_failures=sorted(f_base),
        delta_failures=sorted(delta_f),
        pre_existing_failures=sorted(pre_existing),
        fixed_failures=sorted(fixed),
        verdict=verdict,
        pre_existing_ignored=pre_existing_ignored,
        warning=warning,
        message=message,
    )


def extract_failing_tests(test_output: str, exit_code: int = 0) -> List[str]:
    """从测试执行输出与退出码中提取失败用例标识符列表。"""
    _, _, failing = parse_test_output(test_output, exit_code)
    return failing


def write_base_test_snapshot(
    snapshot_path: Path | str,
    base_failures: Iterable[str],
    metadata: Optional[dict[str, Any]] = None,
) -> Path:
    """将 Base 分支存量失败列表持久化为 JSON 快照文件。"""
    path = Path(snapshot_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    failures_list = sorted(set(base_failures or []))
    payload = {
        "version": 1,
        "failing_tests": failures_list,
        "count": len(failures_list),
        "metadata": metadata or {},
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp_path = path.with_suffix(f"{path.suffix}.tmp.{os.getpid()}")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)
    return path


def read_base_test_snapshot(snapshot_path: Path | str) -> dict[str, Any]:
    """读取 Base 分支存量失败 JSON 快照文件。若文件不存在或损坏，安全降级为空列表。"""
    path = Path(snapshot_path)
    default_res: dict[str, Any] = {
        "version": 1,
        "failing_tests": [],
        "count": 0,
        "metadata": {},
    }
    if not path.is_file():
        return default_res
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default_res
        failing = data.get("failing_tests")
        if not isinstance(failing, list):
            failing = []
        return {
            "version": data.get("version", 1),
            "failing_tests": failing,
            "count": len(failing),
            "metadata": data.get("metadata", {}),
            "captured_at": data.get("captured_at"),
        }
    except Exception:
        return default_res


def capture_git_base_test_snapshot(
    repo_path: Path | str,
    base_ref: str,
    test_command: str | List[str],
    output_snapshot_path: Optional[Path | str] = None,
    timeout: float = 300,
) -> dict[str, Any]:
    """在 Git 仓库中执行 Base 分支测试命令并捕获失败快照。"""
    root = Path(repo_path)
    cmd = test_command if isinstance(test_command, list) else ["/bin/sh", "-c", test_command]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        output = proc.stdout + "\n" + proc.stderr
        exit_code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + "\n" + (exc.stderr or "")
        exit_code = 124
    except Exception as exc:
        output = str(exc)
        exit_code = 1

    failures = extract_failing_tests(output, exit_code)
    head_commit = None
    try:
        head_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            text=True,
            capture_output=True,
            timeout=10,
        )
        if head_proc.returncode == 0:
            head_commit = head_proc.stdout.strip()
    except Exception:
        pass

    metadata = {
        "base_ref": str(base_ref),
        "test_command": test_command if isinstance(test_command, str) else " ".join(test_command),
        "exit_code": exit_code,
        "head_commit": head_commit,
    }
    if output_snapshot_path:
        write_base_test_snapshot(output_snapshot_path, failures, metadata=metadata)

    return {
        "failing_tests": failures,
        "count": len(failures),
        "metadata": metadata,
    }
