"""Domain Adapter Registry and Factory for HAFlow."""

from typing import Any, Dict, Optional, Type

from .base import BaseDomainAdapter
from .generic import GenericDomainAdapter
from .software import SoftwareDomainAdapter

_ADAPTER_CLASSES: Dict[str, Type[BaseDomainAdapter]] = {
    "generic": GenericDomainAdapter,
    "software": SoftwareDomainAdapter,
    # Aliases
    "code": SoftwareDomainAdapter,
    "dev": SoftwareDomainAdapter,
    "docs": GenericDomainAdapter,
    "bid": GenericDomainAdapter,
    "tender": GenericDomainAdapter,
    "cs": GenericDomainAdapter,
    "customer_service": GenericDomainAdapter,
    "legal": GenericDomainAdapter,
    "research": GenericDomainAdapter,
    "general": GenericDomainAdapter,
}

_SOFTWARE_TASK_TYPES = frozenset({
    "code", "test", "fix", "bugfix", "hotfix", "refactor", "impl", "craft"
})

_SOFTWARE_TEMPLATE_PREFIXES = ("software-", "code-", "tdd-")


def register_domain_adapter(name: str, adapter_cls: Type[BaseDomainAdapter]) -> None:
    """Register a custom domain adapter."""
    _ADAPTER_CLASSES[name.lower().strip()] = adapter_cls


def list_domain_adapters() -> Dict[str, str]:
    """Return dictionary of registered domain adapters and their classes."""
    return {k: v.__name__ for k, v in _ADAPTER_CLASSES.items()}


def get_domain_adapter(
    domain: Optional[str] = None,
    task_type: Optional[str] = None,
    template_name: Optional[str] = None,
    workflow_cfg: Optional[Dict[str, Any]] = None,
    task: Optional[Dict[str, Any]] = None,
) -> BaseDomainAdapter:
    """Resolve and return an appropriate BaseDomainAdapter instance.

    Resolution order:
    1. Explicit `domain` parameter (e.g. 'software', 'generic', etc.)
    2. `task['domain']` if present
    3. `workflow_cfg['domain']` if present
    4. Heuristic inference:
       - `task_type` in software task types ('code', 'test', 'fix', etc.)
       - `template_name` starts with software prefixes ('software-', 'code-', etc.)
    5. Default: GenericDomainAdapter (100% neutral, zero side-effects).
    """
    selected_name = None

    if domain:
        selected_name = str(domain).lower().strip()
    elif task and task.get("domain"):
        selected_name = str(task["domain"]).lower().strip()
    elif workflow_cfg and workflow_cfg.get("domain"):
        selected_name = str(workflow_cfg["domain"]).lower().strip()
    elif task_type and str(task_type).lower().strip() in _SOFTWARE_TASK_TYPES:
        selected_name = "software"
    elif template_name and any(template_name.lower().startswith(p) for p in _SOFTWARE_TEMPLATE_PREFIXES):
        selected_name = "software"
    elif task and task.get("task_type") and str(task["task_type"]).lower().strip() in _SOFTWARE_TASK_TYPES:
        selected_name = "software"

    if selected_name and selected_name in _ADAPTER_CLASSES:
        return _ADAPTER_CLASSES[selected_name]()

    # Default fallback is always generic (safe, neutral)
    return GenericDomainAdapter()
