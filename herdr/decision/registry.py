#!/usr/bin/env python3
"""DecisionProvider registry (herdr/decision/registry.py).

Providers self-register by name; HAFlow domain code asks the registry for
``supervisor.provider`` and never imports a concrete backend.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional

from .base import DecisionProvider

_factories: Dict[str, Callable[[dict], DecisionProvider]] = {}


def register_provider(name: str, factory: Callable[[dict], DecisionProvider]) -> None:
    _factories[name] = factory


def provider_names() -> tuple:
    return tuple(sorted(_factories))


def create_provider(name: str, config: Optional[dict] = None) -> Optional[DecisionProvider]:
    """Instantiate a registered provider, or None for an unknown name."""
    factory = _factories.get(name or "")
    if factory is None:
        return None
    return factory(dict(config or {}))
