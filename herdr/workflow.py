#!/opt/homebrew/bin/python3
"""Herdr Universal Workflow & Node Engine.

Manages Workflow Definitions, Templates, DAG dependency calculation,
and normalization between Node-centric workflows and legacy Stage representations.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

try:
    import yaml
except ImportError:
    yaml = None

HOME = Path.home()
USER_TEMPLATES_DIR = HOME / ".herdr-controller" / "templates"
BUNDLED_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "workflow_templates"
if not BUNDLED_TEMPLATES_DIR.exists():
    BUNDLED_TEMPLATES_DIR = Path(__file__).resolve().parent / "workflow_templates"


def _load_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Template file not found: {path}")

    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        if yaml is None:
            raise RuntimeError("PyYAML is required to parse YAML workflow templates.")
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)

    if not isinstance(data, dict):
        raise ValueError(f"Invalid workflow format in {path}: expected dict, got {type(data)}")
    return data


def list_templates() -> Dict[str, Dict[str, Any]]:
    """List all available workflow templates from user and bundled directories."""
    templates = {}
    search_dirs = [USER_TEMPLATES_DIR, BUNDLED_TEMPLATES_DIR]

    for directory in search_dirs:
        if not directory.exists():
            continue
        for file in directory.glob("*.*"):
            if file.suffix.lower() in (".yaml", ".yml", ".json"):
                base_name = file.stem
                if base_name in templates:
                    continue
                try:
                    data = _load_file(file)
                    templates[base_name] = {
                        "name": data.get("name", base_name),
                        "label": data.get("label", base_name),
                        "version": str(data.get("version", "1.0")),
                        "description": data.get("description", ""),
                        "node_count": len(data.get("nodes", [])),
                        "path": str(file),
                    }
                except Exception:
                    continue

    return templates


DEFAULT_TEMPLATE_NAME = "software-development-v1"


def load_template(name_or_path: Optional[str] = None) -> Dict[str, Any]:
    """Load a workflow template by name or file path."""
    if not name_or_path:
        name_or_path = DEFAULT_TEMPLATE_NAME

    explicit_path = Path(name_or_path).expanduser()
    if explicit_path.exists():
        return normalize_workflow(_load_file(explicit_path))

    candidates = [
        USER_TEMPLATES_DIR / f"{name_or_path}.yaml",
        USER_TEMPLATES_DIR / f"{name_or_path}.yml",
        USER_TEMPLATES_DIR / f"{name_or_path}.json",
        BUNDLED_TEMPLATES_DIR / f"{name_or_path}.yaml",
        BUNDLED_TEMPLATES_DIR / f"{name_or_path}.yml",
        BUNDLED_TEMPLATES_DIR / f"{name_or_path}.json",
    ]

    for candidate in candidates:
        if candidate.exists():
            data = _load_file(candidate)
            return normalize_workflow(data)

    available = list(list_templates().keys())
    raise FileNotFoundError(
        f"Workflow template '{name_or_path}' not found. Available templates: {', '.join(available)}"
    )


def validate_workflow_dag(nodes: List[Dict[str, Any]]) -> None:
    """Validate that the node DAG has no unknown dependencies or cycles."""
    node_map = {n["id"]: n for n in nodes if "id" in n}
    if len(node_map) != len(nodes):
        raise ValueError("Duplicate node IDs found in workflow definition.")

    for node in nodes:
        node_id = node.get("id")
        deps = node.get("depends_on", [])
        if not isinstance(deps, list):
            raise ValueError(f"Node '{node_id}' depends_on must be a list, got {type(deps)}")
        for dep in deps:
            if dep not in node_map:
                raise ValueError(
                    f"Node '{node_id}' depends on unknown node '{dep}'."
                )

        # Validate gate retry_target if present
        gate = node.get("gate") or {}
        if isinstance(gate, dict):
            retry_target = gate.get("retry_target")
            if retry_target and retry_target not in node_map:
                raise ValueError(
                    f"Node '{node_id}' gate retry_target references unknown node '{retry_target}'."
                )

        # Validate inputs node references if present
        inputs = node.get("inputs") or []
        if isinstance(inputs, list):
            for inp in inputs:
                ref = inp.get("ref") if isinstance(inp, dict) else (inp if isinstance(inp, str) else "")
                if ref.startswith("nodes."):
                    parts = ref.split(".")
                    ref_node = parts[1]
                    if ref_node not in node_map:
                        raise ValueError(
                            f"Node '{node_id}' input references unknown node '{ref_node}'."
                        )

    # Topological cycle detection using Kahn's algorithm
    in_degree = {n["id"]: 0 for n in nodes}
    for node in nodes:
        for dep in node.get("depends_on", []):
            pass  # in-degree is number of prerequisites
        in_degree[node["id"]] = len(node.get("depends_on", []))

    zero_in = [n_id for n_id, deg in in_degree.items() if deg == 0]
    visited_count = 0
    adj = {n["id"]: [] for n in nodes}
    for node in nodes:
        for dep in node.get("depends_on", []):
            adj[dep].append(node["id"])

    queue = list(zero_in)
    while queue:
        curr = queue.pop(0)
        visited_count += 1
        for downstream in adj[curr]:
            in_degree[downstream] -= 1
            if in_degree[downstream] == 0:
                queue.append(downstream)

    if visited_count != len(nodes):
        raise ValueError("Workflow DAG contains a circular dependency / cycle.")


def normalize_workflow(workflow: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize workflow dict and apply the Execution & Context contract."""
    return _normalize_execution_contract(_normalize_workflow_body(workflow))


EXECUTION_MODES = {"git", "context"}
_CONTEXT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def _normalize_context_entry(entry: Any) -> Dict[str, str]:
    if isinstance(entry, str):
        entry = {"id": entry}
    if not isinstance(entry, dict):
        raise ValueError(f"invalid context entry: {entry!r}")
    ctx_id = str(entry.get("id") or "").strip()
    if not _CONTEXT_ID_RE.match(ctx_id):
        raise ValueError(f"invalid context id: {entry.get('id')!r}")
    return {"id": ctx_id, "label": str(entry.get("label") or ctx_id)}


def _normalize_execution_contract(result: Dict[str, Any]) -> Dict[str, Any]:
    execution = result.get("execution") or {}
    if not isinstance(execution, dict):
        raise ValueError("execution must be a mapping")
    mode = str(execution.get("mode") or "git")
    if mode not in EXECUTION_MODES:
        raise ValueError(
            f"unknown execution.mode: {mode!r} (supported: {', '.join(sorted(EXECUTION_MODES))})"
        )
    result["execution"] = {"mode": mode}

    context = result.get("context") or {}
    if not isinstance(context, dict):
        raise ValueError("context must be a mapping")
    normalized: Dict[str, List[Dict[str, str]]] = {}
    seen = set()
    for key in ("required", "optional"):
        items = [_normalize_context_entry(e) for e in (context.get(key) or [])]
        for item in items:
            if item["id"] in seen:
                raise ValueError(f"duplicate context id: {item['id']}")
            seen.add(item["id"])
        normalized[key] = items
    if normalized["required"] or normalized["optional"]:
        result["context"] = normalized
    else:
        result.pop("context", None)
    return result


def execution_mode(workflow: Optional[Dict[str, Any]]) -> str:
    """Return the execution mode of a (normalized or raw) workflow; legacy defaults to git."""
    execution = (workflow or {}).get("execution") or {}
    mode = str(execution.get("mode") or "git")
    return mode if mode in EXECUTION_MODES else "git"


def context_contract_ids(workflow: Optional[Dict[str, Any]]) -> tuple:
    context = (workflow or {}).get("context") or {}
    def _ids(key: str) -> List[str]:
        return [
            e["id"] if isinstance(e, dict) else str(e).strip()
            for e in (context.get(key) or [])
        ]
    return _ids("required"), _ids("optional")


def validate_context_contract(
    workflow: Dict[str, Any],
    bindings: Dict[str, str],
) -> None:
    """Fail fast on missing required / unknown context bindings; optional may be absent."""
    bindings = bindings or {}
    required, optional = context_contract_ids(workflow)
    declared = set(required) | set(optional)

    missing = [cid for cid in required if not str(bindings.get(cid) or "").strip()]
    if missing:
        raise ValueError("Missing required context: " + ", ".join(missing))

    unknown = sorted(str(k) for k in bindings if str(k).strip() and k not in declared)
    if unknown:
        declared_text = ", ".join(sorted(declared)) if declared else "none"
        raise ValueError(
            f"Unknown context binding(s): {', '.join(unknown)}; "
            f"template declares: {declared_text}"
        )


def parse_context_binding_args(values: Optional[List[str]]) -> Dict[str, str]:
    """Parse repeated CLI '--context id=path' arguments into a binding dict."""
    bindings: Dict[str, str] = {}
    for raw in values or []:
        text = str(raw)
        if "=" not in text:
            raise ValueError(f"invalid --context '{raw}', expected id=path")
        key, _, path = text.partition("=")
        key = key.strip()
        path = path.strip()
        if not key or not path:
            raise ValueError(f"invalid --context '{raw}', expected id=path")
        if not _CONTEXT_ID_RE.match(key):
            raise ValueError(f"invalid context id in --context '{raw}'")
        if key in bindings:
            raise ValueError(f"duplicate --context for id: {key}")
        bindings[key] = path
    return bindings


def render_context_reference_block(bindings: Optional[Dict[str, str]]) -> str:
    """Context Reference only: ids + absolute paths. Never expand file contents."""
    if not bindings:
        return ""
    rule = "━" * 20
    lines = [rule, "Workflow Context", rule]
    for ctx_id in sorted(bindings):
        lines.append(f"{ctx_id}:")
        lines.append(f"  {bindings[ctx_id]}")
    lines.append("")
    lines.append("请根据当前任务需要自行读取相关文件。")
    lines.append("不要假设不存在的事实。")
    return "\n".join(lines)


def validate_integration_for_execution(mode: str, integration_mode_value: Optional[str]) -> None:
    """execution.mode and integration_mode are orthogonal; context must never take the git path."""
    if mode == "context" and (integration_mode_value or "none") == "git":
        raise ValueError(
            "execution.mode=context 的任务不支持 integration_mode=git；"
            "context 任务产出只写入 Task Workspace（integration_mode=none）"
        )


def validate_onto_for_execution(mode: str, onto_branch: Optional[str]) -> None:
    """--onto 是 git 分支续接语义，context 任务没有分支，必须显式拒绝而非静默忽略。"""
    if mode == "context" and onto_branch:
        raise ValueError(
            f"execution.mode=context 的任务没有 git 分支，不支持 --onto {onto_branch}"
        )


def _normalize_workflow_body(workflow: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize workflow dict to ensure both 'nodes' and 'stages' are present and consistent."""
    result = dict(workflow)

    # Case 1: has 'nodes'
    if "nodes" in result and isinstance(result["nodes"], list):
        nodes = []
        stages = []
        for idx, node in enumerate(result["nodes"]):
            node_id = str(node.get("id") or node.get("key") or f"node_{idx+1}")
            label = str(node.get("label") or node_id)
            node_type = str(node.get("node_type") or "agent")
            depends_on = list(node.get("depends_on") or [])
            worker_policy = dict(node.get("worker_policy") or node.get("agent_policy") or {})
            if "capabilities" not in worker_policy and "capabilities" in node:
                worker_policy["capabilities"] = list(node["capabilities"])
            if "permissions" not in worker_policy and "permissions" in node:
                worker_policy["permissions"] = list(node["permissions"])

            norm_node = {
                "id": node_id,
                "label": label,
                "node_type": node_type,
                "depends_on": depends_on,
                "parallel": bool(node.get("parallel", False)),
                "purpose": node.get("purpose", ""),
                "default_integration_mode": node.get("default_integration_mode", "none"),
                "default_task_type": node.get("default_task_type", "docs"),
                "agent_policy": dict(node.get("agent_policy") or worker_policy),
                "worker_policy": worker_policy,
                "inputs": list(node.get("inputs") or []),
                "required_outputs": list(node.get("required_outputs") or []),
                "rules": list(node.get("rules") or []),
                "gate": dict(node.get("gate") or {}),
            }
            if "tab_id" in node:
                norm_node["tab_id"] = node["tab_id"]
            if "anchor_pane_id" in node:
                norm_node["anchor_pane_id"] = node["anchor_pane_id"]

            nodes.append(norm_node)

            # Build synchronized stage for backward-compatibility
            stage = {
                "key": node_id,
                "label": label,
                "order": idx + 1,
                "next": None,
            }
            if "tab_id" in norm_node:
                stage["tab_id"] = norm_node["tab_id"]
            if "anchor_pane_id" in norm_node:
                stage["anchor_pane_id"] = norm_node["anchor_pane_id"]
            stages.append(stage)

        # Set stage 'next' pointers based on sequence
        for i in range(len(stages) - 1):
            stages[i]["next"] = stages[i + 1]["key"]

        validate_workflow_dag(nodes)
        result["nodes"] = nodes
        result["stages"] = stages
        return result

    # Case 2: legacy 'stages' only
    if "stages" in result and isinstance(result["stages"], list):
        nodes = []
        stages = []
        raw_stages = list(result["stages"])
        stage_policies = dict(result.get("stage_policies") or {})

        for idx, raw_stage in enumerate(raw_stages):
            if isinstance(raw_stage, str):
                node_id = raw_stage
                label = raw_stage
                tab_id = None
                anchor_pane_id = None
            elif isinstance(raw_stage, dict):
                node_id = str(raw_stage.get("key") or raw_stage.get("id") or f"node_{idx+1}")
                label = str(raw_stage.get("label") or node_id)
                tab_id = raw_stage.get("tab_id")
                anchor_pane_id = raw_stage.get("anchor_pane_id")
            else:
                node_id = f"node_{idx+1}"
                label = node_id
                tab_id = None
                anchor_pane_id = None

            prev_id = nodes[idx - 1]["id"] if idx > 0 else None
            depends_on = [prev_id] if prev_id else []

            policy = stage_policies.get(node_id, {})
            norm_node = {
                "id": node_id,
                "label": label,
                "node_type": "agent",
                "depends_on": depends_on,
                "parallel": False,
                "purpose": policy.get("purpose", ""),
                "default_integration_mode": policy.get("default_integration_mode", "none"),
                "default_task_type": policy.get("default_task_type", "docs"),
                "agent_policy": dict(policy.get("agent_policy") or {}),
                "required_outputs": list(policy.get("required_outputs") or []),
                "rules": list(policy.get("rules") or []),
                "gate": dict(policy.get("gate") or {}),
            }
            if tab_id:
                norm_node["tab_id"] = tab_id
            if anchor_pane_id:
                norm_node["anchor_pane_id"] = anchor_pane_id
            nodes.append(norm_node)

            stage_entry = {
                "key": node_id,
                "label": label,
                "order": idx + 1,
                "next": None,
            }
            if tab_id:
                stage_entry["tab_id"] = tab_id
            if anchor_pane_id:
                stage_entry["anchor_pane_id"] = anchor_pane_id
            stages.append(stage_entry)

        for i in range(len(stages) - 1):
            stages[i]["next"] = stages[i + 1]["key"]

        result["nodes"] = nodes
        result["stages"] = stages
        return result

    # Fallback if empty
    result.setdefault("nodes", [])
    result.setdefault("stages", [])
    return result


def find_node(workflow: Dict[str, Any], node_id: str) -> Optional[Dict[str, Any]]:
    """Find a node by id or stage key in normalized workflow dict."""
    normalized = normalize_workflow(workflow)
    for node in normalized.get("nodes", []):
        if node["id"] == node_id:
            return node
    return None


def get_ready_nodes(workflow: Dict[str, Any], completed_node_ids: Set[str]) -> List[Dict[str, Any]]:
    """Determine which nodes in the workflow DAG are ready to execute.

    A node is ready if:
    1. It is not already in completed_node_ids.
    2. All nodes listed in its 'depends_on' are in completed_node_ids.
    """
    normalized = normalize_workflow(workflow)
    ready = []

    for node in normalized.get("nodes", []):
        node_id = node["id"]
        if node_id in completed_node_ids:
            continue

        deps = set(node.get("depends_on", []))
        if deps.issubset(completed_node_ids):
            ready.append(node)

    return ready


def is_workflow_completed(workflow: Dict[str, Any], completed_node_ids: Set[str]) -> bool:
    """Return True if every node in the workflow has been completed."""
    normalized = normalize_workflow(workflow)
    nodes = normalized.get("nodes", [])
    if not nodes:
        return False
    all_node_ids = {n["id"] for n in nodes}
    return all_node_ids.issubset(completed_node_ids)
