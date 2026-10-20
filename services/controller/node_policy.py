"""Resolve legacy node defaults, gate configuration and dispatch feature switches.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def resolve_gate_config(ports, node, node_id):
    gate = (node or {}).get("gate") or ports.get_stage_policy(node_id).get("gate")

    if gate is None and (node or {}).get("node_type") == "gate":
        gate = {}

    if gate is None:
        gate = ports.GATE_DEFAULTS.get(node_id)

    if not gate and (node or {}).get("node_type") != "gate":
        return None

    if not isinstance(gate, dict):
        gate = {}

    return {
        "retry_node": gate.get("retry_node") or gate.get("retry_target") or "implementation",
        "max_loops": int(gate.get("max_loops", ports.FIX_LOOP_MAX)),
    }

def gate_verdict(ports, workflow_id, node_id):
    """Fail-safe 门禁结论:任一未作废任务的 blocked 结论即 blocked。"""
    verdict = None

    for task in ports.load_tasks():
        if task.get("workflow_id") != workflow_id:
            continue
        if node_id not in (task.get("node"), task.get("stage")):
            continue
        if task.get("status") == "superseded":
            continue

        task_verdict = task.get("stage_verdict")

        if task_verdict == "blocked":
            return "blocked"
        if task_verdict == "pass":
            verdict = "pass"

    return verdict

def latest_branch_for_node(ports, workflow_id, node_id):
    best = None

    for task in ports.load_tasks():
        if task.get("workflow_id") != workflow_id:
            continue
        if node_id not in (task.get("node"), task.get("stage")):
            continue
        if not task.get("branch"):
            continue
        if best is None or task.get("updated_at", 0) > best.get("updated_at", 0):
            best = task

    return best.get("branch") if best else None

def blocked_verdict_dep(ports, workflow_id, node):
    """依赖中是否存在任务级 blocked 验收结论(不问 gate_cfg)。

    plan/requirements 等节点没有 gate 配置,sweep 的 fix-loop 看不见它们,
    但自动推进同样不得越过 blocked 结论。返回首个被阻断的依赖 id。
    """
    for dep in ((node or {}).get("depends_on") or []):
        if ports.gate_verdict(workflow_id, dep) == "blocked":
            return dep
    return None

def blocked_gate_dependency(ports, workflow_id, ready_node, workflow_cfg):
    """ready_node 的依赖中是否存在 verdict=blocked 的门禁节点。"""
    nodes_by_id = {
        n["id"]: n for n in workflow_cfg.get("nodes", [])
    }

    for dep in ready_node.get("depends_on", []):
        gate_cfg = ports.resolve_gate_config(nodes_by_id.get(dep), dep)

        if not gate_cfg:
            continue

        if ports.gate_verdict(workflow_id, dep) == "blocked":
            return dep, gate_cfg

    return None


# Stage policies
def load_stage_policies(ports):
    try:
        with open(
            ports.STAGE_POLICIES_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            return ports.json.load(f)

    except Exception as e:
        print(
            f"[STAGE POLICY ERROR] {e}"
        )
        return {}

def get_stage_policy(ports, stage_key):
    policies = ports.load_stage_policies()

    return policies.get(
        stage_key,
        {}
    )

def direct_stage_dispatch_enabled(ports):
    value = ports.os.environ.get("HERDR_DIRECT_STAGE_DISPATCH", "1")
    return value.strip().lower() not in ("0", "false", "off", "no")

def coordinator_intake_enabled(ports):
    """新工作流首个节点是否交由总指挥接单(默认开启)。

    产品期望:启动时先由总指挥接收需求、理解目标与约束,再派发第一个
    节点的 Task,而不是系统直接开始(旧直派路径跳过总指挥)。
    设 HERDR_COORDINATOR_INTAKE=0 退回直派快路径。
    """
    value = ports.os.environ.get("HERDR_COORDINATOR_INTAKE", "1")
    return value.strip().lower() not in ("0", "false", "off", "no")

def node_is_gate(ports, workflow_id, node_id):
    """节点是否带门禁配置(含 GATE_DEFAULTS 的 test/review/wrapup)。

    配置不可判定时保守返回 True(保留总指挥路径),绝不因配置异常
    误吞门禁裁决。
    """
    try:
        wf_cfg = ports.workflow_config_for(workflow_id) or {}
        nodes_by_id = {n["id"]: n for n in wf_cfg.get("nodes", [])}
        return ports.resolve_gate_config(nodes_by_id.get(node_id), node_id) is not None
    except Exception:
        return True


DEPENDENCIES = {'resolve_gate_config': ('FIX_LOOP_MAX', 'GATE_DEFAULTS', 'get_stage_policy'),
 'gate_verdict': ('load_tasks',),
 'latest_branch_for_node': ('load_tasks',),
 'blocked_verdict_dep': ('gate_verdict',),
 'blocked_gate_dependency': ('gate_verdict', 'resolve_gate_config'),
 'load_stage_policies': ('STAGE_POLICIES_FILE', 'json'),
 'get_stage_policy': ('load_stage_policies',),
 'direct_stage_dispatch_enabled': ('os',),
 'coordinator_intake_enabled': ('os',),
 'node_is_gate': ('resolve_gate_config', 'workflow_config_for')}

__all__ = tuple(DEPENDENCIES)
