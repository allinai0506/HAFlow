"""Software Engineering Domain Adapter.

Encapsulates code-specific extensions:
- Git rebase adoption policy with Committer Date verification
- Husky compliance scaffolding & postmortem bug-report injection
- Differential testing engine (exempting base-branch pre-existing failures)
"""

from pathlib import Path
from typing import Any, Dict, Optional

from .base import BaseDomainAdapter


class SoftwareDomainAdapter(BaseDomainAdapter):
    """Domain adapter for software engineering tasks."""

    @property
    def name(self) -> str:
        return "software"

    def on_task_init(self, clone_path: Optional[Path], task_meta: Dict[str, Any]) -> Dict[str, Any]:
        """Scaffold compliance templates if applicable (e.g., bug reports for fix tasks)."""
        if not clone_path or not Path(clone_path).exists():
            return {"scaffolded": False}
        try:
            from herdr.compliance_scaffolding import ensure_compliance_scaffolding
            return ensure_compliance_scaffolding(Path(clone_path), task_meta)
        except Exception as exc:
            return {"scaffolded": False, "error": str(exc)}

    def on_task_finalize(self, clone_path: Optional[Path], task_meta: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    def prepare_delivery_contract(
        self, contract: Optional[Dict[str, Any]], task_meta: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Automatically whitelist bug reports in delivery contract for software fix tasks."""
        if not contract:
            return contract
        try:
            from herdr.compliance_scaffolding import (
                is_fix_task,
                build_bug_report_template,
                update_contract_whitelist,
            )
            task_id = task_meta.get("task_id")
            task_type = task_meta.get("task_type")
            node_id = task_meta.get("node_id")
            branch = task_meta.get("branch")

            if is_fix_task(task_id=task_id, task_type=task_type, node_id=node_id, branch=branch):
                rel_path, _ = build_bug_report_template({
                    "task_id": task_id,
                    "task_type": task_type,
                    "branch": branch,
                })
                return update_contract_whitelist(contract, rel_path=rel_path)
        except Exception:
            pass
        return contract

    def evaluate_differential_tests(
        self,
        candidate_output: str,
        base_output: Optional[str] = None,
        base_snapshot: Optional[Dict[str, Any]] = None,
        candidate_exit_code: int = 0,
        base_exit_code: int = 0,
    ) -> Dict[str, Any]:
        """Run differential test comparison between candidate and base."""
        from herdr.differential_testing import (
            compute_differential_verdict,
            extract_failing_tests,
            read_base_test_snapshot,
        )

        base_failures = []
        if base_snapshot is not None:
            if isinstance(base_snapshot, dict):
                base_failures = base_snapshot.get("failing_tests", [])
            elif isinstance(base_snapshot, (str, Path)):
                snap = read_base_test_snapshot(base_snapshot)
                base_failures = snap.get("failing_tests", [])
        elif base_output is not None:
            base_failures = extract_failing_tests(base_output, exit_code=base_exit_code)

        candidate_failures = extract_failing_tests(candidate_output, exit_code=candidate_exit_code)

        # Defensive: non-zero exit code without parsed test names is an unmasked defect
        if candidate_exit_code != 0 and not candidate_failures:
            candidate_failures = [f"test_process_exit_code_{candidate_exit_code}"]

        res = compute_differential_verdict(candidate_failures, base_failures)
        res_dict = res.to_dict()
        res_dict["domain"] = self.name
        return res_dict

    def should_allow_rebase(
        self,
        task: Dict[str, Any],
        explicit_flag: bool = False,
        env_flag: bool = False,
    ) -> bool:
        """For software domain tasks, allow rebase adoption if explicit, configured, or onto branch is present."""
        if explicit_flag:
            return True
        task_val = task.get("allow_rebase")
        if task_val is not None:
            return bool(task_val)
        if bool(task.get("onto")):
            return True
        return bool(env_flag)
