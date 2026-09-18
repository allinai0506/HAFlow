#!/usr/bin/env python3
"""DecisionProvider domain models (herdr/decision/models.py).

Functional Core: HAFlow-side typed results for semantic decisions. External
provider payloads (e.g. Jev noul/score/choice answers) are normalized into
``DecisionResult`` here and never enter HAFlow domain code unconverted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class DecisionProviderError(Exception):
    """Provider could not produce a usable decision.

    ``kind`` classifies the failure so callers can keep fail-safe behaviour
    uniform: timeout / auth / rate_limit / overloaded / invalid / unavailable.
    """

    def __init__(self, message: str, kind: str = "unavailable") -> None:
        super().__init__(message)
        self.kind = kind


# Failure kinds that mean "retrying now will not help; stay quiet".
RETRYABLE_KINDS = frozenset({"timeout", "rate_limit", "overloaded"})


@dataclass
class DecisionResult:
    """One semantic judgment returned by any DecisionProvider.

    value: float in [0,1] for judge/score, str option key for choose.
    confidence: only set when the provider genuinely reports it; judge
    (binary) providers such as Jev Noul report no confidence and leave it
    None rather than a fabricated number.
    probabilities: full distribution when the provider exposes one.
    """

    value: Any
    confidence: Optional[float] = None
    probabilities: Optional[Dict[str, float]] = None
    provider: str = ""
    latency_ms: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "value": self.value,
            "confidence": self.confidence,
            "probabilities": self.probabilities,
            "provider": self.provider,
            "latency_ms": self.latency_ms,
            "metadata": dict(self.metadata),
        }

    @property
    def certainty(self) -> Optional[float]:
        """Distance of a probability-like value from 0.5, in [0,1].

        A derived reading aid (NOT a provider-reported confidence): used by
        the policy engine only when the provider gives no confidence, to tell
        "model unsure" (value near 0.5) from "model decided" (value near 0/1).
        """
        if self.confidence is not None:
            return float(self.confidence)
        if isinstance(self.value, (int, float)):
            return abs(2.0 * float(self.value) - 1.0)
        return None


def clamp_probability(value: Any) -> Optional[float]:
    """Coerce a provider value into a probability float, or None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN guard
        return None
    return max(0.0, min(1.0, number))
