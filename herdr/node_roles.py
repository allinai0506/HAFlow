"""Authoritative semantic classification of workflow nodes.

Node role is declared by the template, not spelled by its id. code-craft-v1
names its gates ``cleanroom_test`` / ``cleanroom_review``; protections keyed on
the literal ids ``{'test', 'review'}`` silently skipped them and let both
cleanroom stations verify the baseline instead of the frozen candidate.
"""

from .workflow import execution_mode

# Templates that predate declared task types are still honoured by id.
LEGACY_VERIFIER_IDS = frozenset({"test", "review"})
_VERIFIER_TASK_TYPES = frozenset({"test", "review"})


def _resolves_to_test(declared_task_type, node_id=None):
    # Imported lazily: direct_dispatch is a caller of this module, and it owns
    # TASK_TYPE_ALIASES (review -> test) as the single authoritative resolver.
    from .direct_dispatch import resolve_task_type

    return resolve_task_type(declared_task_type, node_id) == "test"


def _declared_task_type(node):
    """The node's own declaration, from a raw template or a normalized dispatch node.

    normalize_node replaces default_task_type with the resolved task_type plus the
    raw declared_task_type, so dispatch-time callers never see the template key.
    """
    for key in ("default_task_type", "declared_task_type", "task_type"):
        value = str(node.get(key) or "").strip()
        if value:
            return value
    return ""


def is_verifier_node(node):
    """True when a node's declared contract is to verify a candidate, not produce one."""
    if not isinstance(node, dict):
        return False
    node_id = str(node.get("id") or "").strip()
    if node_id in LEGACY_VERIFIER_IDS:
        return True
    return _resolves_to_test(_declared_task_type(node), node_id or None)


def is_verifier_task(task):
    """Task-level classification for paths that only hold the persisted task."""
    if not isinstance(task, dict):
        return False
    node_id = str(task.get("node") or task.get("stage") or "").strip()
    if node_id in LEGACY_VERIFIER_IDS:
        return True
    declared = task.get("declared_task_type") or task.get("task_type")
    return str(declared or "").strip() in _VERIFIER_TASK_TYPES


def verifier_node_ids(config):
    """Verifier ids declared by a workflow config; empty when it declares none."""
    nodes = config.get("nodes") if isinstance(config, dict) else None
    return {
        str(node["id"]).strip()
        for node in (nodes or [])
        if isinstance(node, dict) and str(node.get("id") or "").strip()
        and is_verifier_node(node)
    }


def is_verifier_node_id(config, node_id):
    """Id-level classification for callers that never see the node dict.

    Legacy ids count without a config, so an unresolvable definition cannot
    silently downgrade a gate that used to be protected.
    """
    text = str(node_id or "").strip()
    return text in LEGACY_VERIFIER_IDS or text in verifier_node_ids(config)


def candidate_identity_applies(workflow):
    """A candidate SHA is a Git fact; a context workflow has nothing to prove."""
    return execution_mode(workflow) != "context"
