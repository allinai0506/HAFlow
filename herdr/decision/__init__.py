#!/usr/bin/env python3
"""HAFlow decision layer: pluggable semantic judgment behind one interface.

Domain code depends on ``DecisionProvider`` / ``DecisionResult`` only;
concrete backends (Jev, rules) live in ``providers/`` and self-register.
"""

from .base import DecisionProvider, Question, normalize_question
from .models import (
    DecisionProviderError,
    DecisionResult,
    RETRYABLE_KINDS,
    clamp_probability,
)
from .registry import create_provider, provider_names, register_provider


def _register_builtins() -> None:
    from .providers.jev import JevDecisionProvider
    from .providers.rule import RuleDecisionProvider

    register_provider("jev", lambda cfg: JevDecisionProvider(cfg))
    register_provider("rule", lambda cfg: RuleDecisionProvider(cfg))


_register_builtins()

__all__ = [
    "DecisionProvider",
    "DecisionProviderError",
    "DecisionResult",
    "RETRYABLE_KINDS",
    "Question",
    "clamp_probability",
    "create_provider",
    "normalize_question",
    "provider_names",
    "register_provider",
]
