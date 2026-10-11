"""HAFlow Domain Adapters SPI Package.

Maintains strict separation between the neutral HAFlow microkernel and
specialized business/engineering domains.
"""

from .base import BaseDomainAdapter
from .generic import GenericDomainAdapter
from .software import SoftwareDomainAdapter
from .registry import (
    get_domain_adapter,
    list_domain_adapters,
    register_domain_adapter,
)

__all__ = [
    "BaseDomainAdapter",
    "GenericDomainAdapter",
    "SoftwareDomainAdapter",
    "get_domain_adapter",
    "list_domain_adapters",
    "register_domain_adapter",
]
