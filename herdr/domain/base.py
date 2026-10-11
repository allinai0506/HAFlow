"""Base Domain Adapter SPI for HAFlow.

Defines the abstract interface for business and engineering domain extensions.
The HAFlow microkernel (Controller, Sentinel, StateDB, Worker runner, DAG Engine)
remains 100% neutral across domains (software development, tender/bidding, customer
service, legal research, general tasks, etc.). All domain-specific customizations
hook into the engine via this SPI.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Optional


class BaseDomainAdapter(ABC):
    """Abstract Base Class for Domain Adapters."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Domain adapter identifier (e.g., 'generic', 'software')."""
        pass

    def on_task_init(self, clone_path: Optional[Path], task_meta: Dict[str, Any]) -> Dict[str, Any]:
        """Lifecycle hook invoked when a task workspace is prepared.

        Args:
            clone_path: Local filesystem path to the task workspace clone (if any).
            task_meta: Dict containing task metadata (task_id, task_type, branch, agent, etc.).

        Returns:
            Dict describing artifacts created or setup status.
        """
        return {"scaffolded": False}

    def on_task_finalize(self, clone_path: Optional[Path], task_meta: Dict[str, Any]) -> Dict[str, Any]:
        """Lifecycle hook invoked when a task completes or workspace is torn down.

        Args:
            clone_path: Local filesystem path to the task workspace clone (if any).
            task_meta: Dict containing task metadata.

        Returns:
            Dict describing cleanup or finalization actions.
        """
        return {}

    def prepare_delivery_contract(
        self, contract: Optional[Dict[str, Any]], task_meta: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Hook to augment or validate delivery contracts before task launch.

        Args:
            contract: The raw or normalized delivery contract dict (if present).
            task_meta: Task metadata (task_id, task_type, node_id, branch, etc.).

        Returns:
            The prepared delivery contract dict.
        """
        return contract

    def evaluate_differential_tests(
        self,
        candidate_output: str,
        base_output: Optional[str] = None,
        base_snapshot: Optional[Dict[str, Any]] = None,
        candidate_exit_code: int = 0,
        base_exit_code: int = 0,
    ) -> Dict[str, Any]:
        """Hook to evaluate differential test results between candidate and base.

        Returns:
            Dict containing 'verdict', 'delta_failures', 'pre_existing_failures', etc.
        """
        return {
            "verdict": "pass",
            "delta_failures": [],
            "pre_existing_failures": [],
            "domain": self.name,
        }

    def should_allow_rebase(
        self,
        task: Dict[str, Any],
        explicit_flag: bool = False,
        env_flag: bool = False,
    ) -> bool:
        """Hook to decide if Git commit adoption allows rebase timestamp rewrite.

        Args:
            task: Task dictionary.
            explicit_flag: Explicit CLI flag (--allow-rebase).
            env_flag: Environment variable flag.

        Returns:
            Boolean indicating whether rebase adoption is permitted.
        """
        return bool(explicit_flag or env_flag)
