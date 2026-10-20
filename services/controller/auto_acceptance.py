"""Check task deliverables and apply evidence-backed non-gate acceptance.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""


# 规则化验收 (auto-accept) — 非门禁节点免除总指挥 LLM 回合
# 背景(lessons §61)：agent_done 后的总指挥验收回合实测占用 2.6h/8h。非门禁
# 节点(需求/计划/实现)的验收标准本就是"产物落盘 + 变更受控"，可由
# verify-baseline 铁证直接判定；门禁节点(test/review/wrapup)保留裁决语义。
def auto_accept_enabled(ports):
    value = ports.os.environ.get("HERDR_AUTO_ACCEPT", "1")
    return value.strip().lower() not in ("0", "false", "off", "no")

def task_changes_recorded(ports, task):
    """verify-baseline 铁证：至少一个受控文件变更(TASK_CHANGED)。"""
    task_id = task.get("task_id")
    if not task_id:
        return False
    try:
        result = ports.subprocess.run(
            [ports.TASK_MANAGER, "verify-baseline", task_id],
            text=True,
            capture_output=True,
            timeout=60,
        )
    except Exception:
        return False

    if result.returncode != 0:
        return False
    output = result.stdout or ""
    for line in output.splitlines():
        if line.startswith("HERDR_BASELINE_RESULT="):
            try:
                payload = ports.json.loads(line.split("=", 1)[1])
            except (ValueError, IndexError):
                continue
            return bool(payload.get("changes"))
    return "TASK_CHANGED" in output

def try_auto_accept(ports, task_id):
    """非门禁节点规则化验收：变更铁证齐备即 completed，免总指挥回合。

    保守口径：只接管 agent_done 的非门禁节点；产物/变更证据不足、
    门禁节点、被显式关闭(HERDR_AUTO_ACCEPT=0)时返回 False，走原路径。
    """
    if not ports.auto_accept_enabled():
        return False

    task = ports.get_task(task_id)
    if not task or task.get("status") != "agent_done":
        return False

    workflow_id = task.get("workflow_id")
    node_id = task.get("node") or task.get("stage")
    if not workflow_id or not node_id:
        return False

    if ports.node_is_gate(workflow_id, node_id):
        return False
    if task.get("stage_verdict") == "blocked":
        return False
    # Review roles may share a requirements/plan node with its author.
    # Legacy task IDs also carry the role when dispatch_role was not recorded.
    role_text = " ".join(str(task.get(key) or "").lower() for key in
                         ("dispatch_role", "agent_role", "role", "task_id"))
    if ports.re.search(r"(?:^|[^a-z])(review(?:er)?|adversarial)(?:$|[^a-z])", role_text):
        return False

    if not ports.task_changes_recorded(task):
        return False

    if not ports.set_task_status(
        task_id, "completed", expected_status="agent_done",
        expected_version=ports._task_version(task), source="herdr-controller:auto-accept",
        metadata={"auto_accept_reason": "controlled_changes", "acceptance_mode": "auto"},
    ):
        return False

    print(
        f"[AUTO ACCEPT] task={task_id} "
        f"node={node_id} baseline=TASK_CHANGED -> completed"
    )
    return True

def check_task_deliverables_ready(ports, task):
    """Check whether deliverables required by the task or node are present and non-empty."""
    clone_path = task.get("clone_path")
    if not clone_path or not ports.os.path.exists(clone_path):
        return False
    wf_id = task.get("workflow_id")
    node_id = task.get("node") or task.get("stage")
    required_outputs = []
    if wf_id and node_id:
        try:
            wf_cfg = ports.workflow_config_for(wf_id)
            if wf_cfg:
                node = ports.find_node(wf_cfg, node_id)
                if node:
                    required_outputs = node.get("required_outputs", [])
        except Exception:
            pass

    if required_outputs:
        for rel in required_outputs:
            p = ports.os.path.join(clone_path, rel)
            if not ports.os.path.exists(p) or ports.os.path.getsize(p) == 0:
                return False
        return True

    # Fallback to checking git status for any changes
    try:
        res = ports.subprocess.run(
            ["git", "-C", str(clone_path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=3
        )
        if res.stdout.strip():
            return True
    except Exception:
        pass
    return False

def gate_verdict_ready(ports, task):
    """Return whether a gate task has a valid machine-readable verdict.

    Gate ``required_outputs`` are human-facing contract labels (for example,
    ``测试结论（PASS / FAIL）``), not repository-relative file paths. A valid
    verdict is therefore independent evidence that lets an idle gate task reach
    the normal ``agent_done`` -> auto-verdict/finalize flow.
    """
    workflow_id = task.get("workflow_id")
    node_id = task.get("node") or task.get("stage")
    if not workflow_id or not node_id:
        return False
    if not ports.node_is_gate(workflow_id, node_id):
        return False
    verdict, _, _ = ports.read_gate_verdict(task)
    return verdict in ("pass", "blocked")


DEPENDENCIES = {'auto_accept_enabled': ('os',),
 'task_changes_recorded': ('TASK_MANAGER', 'json', 'subprocess'),
 'try_auto_accept': ('_task_version', 'auto_accept_enabled', 'get_task', 'node_is_gate', 're',
                     'set_task_status', 'task_changes_recorded'),
 'check_task_deliverables_ready': ('find_node', 'os', 'subprocess', 'workflow_config_for'),
 'gate_verdict_ready': ('node_is_gate', 'read_gate_verdict')}

__all__ = tuple(DEPENDENCIES)
