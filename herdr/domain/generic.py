"""Generic Domain Adapter.

Pure neutral passthrough with zero side effects. Default for non-software tasks:
bidding/tender, customer support, legal drafting, research, etc.
"""

from pathlib import Path
from typing import Any, Dict, Optional

from .base import BaseDomainAdapter


class GenericDomainAdapter(BaseDomainAdapter):
    """Zero-side-effect default adapter for generic business domains."""

    @property
    def name(self) -> str:
        return "generic"

    def on_task_init(self, clone_path: Optional[Path], task_meta: Dict[str, Any]) -> Dict[str, Any]:
        # Generic tasks do not inject code/husky scaffolding
        return {"scaffolded": False}

    def on_task_finalize(self, clone_path: Optional[Path], task_meta: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    def prepare_delivery_contract(
        self, contract: Optional[Dict[str, Any]], task_meta: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        # Passthrough: zero modifications to generic contracts
        return contract

    def evaluate_differential_tests(
        self,
        candidate_output: str,
        base_output: Optional[str] = None,
        base_snapshot: Optional[Dict[str, Any]] = None,
        candidate_exit_code: int = 0,
        base_exit_code: int = 0,
    ) -> Dict[str, Any]:
        return {
            "verdict": "pass",
            "delta_failures": [],
            "pre_existing_failures": [],
            "domain": "generic",
        }

    def should_allow_rebase(
        self,
        task: Dict[str, Any],
        explicit_flag: bool = False,
        env_flag: bool = False,
    ) -> bool:
        return bool(explicit_flag or env_flag)
