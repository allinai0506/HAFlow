"""Herdr Multi-Agent Workflow Platform.

Core Python package providing workflow engine, agent routing, pane allocation,
multi-project registry, and self-healing topology services.
"""

from . import workflow
from . import agent_router
from . import pane_pool
from . import projects
from . import topology
from . import preflight
from . import deep_preflight
from . import kernel
from . import steering
from . import projection
from . import mcp
from . import state_db
from . import state_store
from . import runtime_state
from .state_store import StateStore, SQLiteStateStore, get_state_store

__all__ = [
    "workflow",
    "agent_router",
    "pane_pool",
    "projects",
    "topology",
    "preflight",
    "deep_preflight",
    "kernel",
    "steering",
    "projection",
    "mcp",
    "state_db",
    "state_store",
    "runtime_state",
    "StateStore",
    "SQLiteStateStore",
    "get_state_store",
]

# Provide backwards-compatible module aliases so legacy flat-file imports
# (e.g., `import herdr_workflow` or `from herdr_workflow import ...`) continue to work seamlessly.
import sys

for _alias, _module in [
    ("herdr_workflow", workflow),
    ("herdr_agent_router", agent_router),
    ("herdr_pane_pool", pane_pool),
    ("herdr_projects", projects),
    ("herdr_topology", topology),
    ("herdr_preflight", preflight),
    ("herdr_deep_preflight", deep_preflight),
    ("herdr_kernel", kernel),
    ("herdr_steering", steering),
    ("herdr_projection", projection),
    ("herdr_mcp", mcp),
    ("herdr_state_db", state_db),
    ("herdr_state_store", state_store),
    ("herdr_runtime_state", runtime_state),
]:
    sys.modules.setdefault(_alias, _module)

