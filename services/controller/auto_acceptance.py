"""Check task deliverables and apply evidence-backed non-gate acceptance.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""


from dataclasses import dataclass


# 规则化验收 (auto-accept) — 非门禁节点免除总指挥 LLM 回合
# 背景(lessons §61)：agent_done 后的总指挥验收回合实测占用 2.6h/8h。非门禁
# 节点(需求/计划/实现)的验收标准本就是"产物落盘 + 变更受控"，可由
# verify-baseline 铁证直接判定；门禁节点(test/review/wrapup)保留裁决语义。
def auto_accept_enabled(ports):
    value = ports.os.environ.get("HERDR_AUTO_ACCEPT", "1")
    return value.strip().lower() not in ("0", "false", "off", "no")

AUTO_REWORK_REASON_PREFIX = "auto_acceptance_rework:"
AUTO_ACCEPT_REWORK_LIMIT = 2


@dataclass(frozen=True)
class AutoAcceptResult:
    accepted: bool
    reason_code: str = ""
    detail: str = ""
    missing: tuple[str, ...] = ()

    def __bool__(self):
        return self.accepted


def auto_rework_attempts(ports, task):
    return sum(1 for entry in (task.get("status_history") or [])
               if isinstance(entry, dict) and entry.get("from") == "agent_done"
               and entry.get("to") == "rework"
               and str(entry.get("reason") or "").startswith(AUTO_REWORK_REASON_PREFIX))


def task_changes_evidence(ports, task):
    """Read baseline once; only an explicit empty changes list proves no changes."""
    if not task.get("task_id"):
        return "unavailable", "task_id missing"
    try:
        result = ports.subprocess.run(
            [ports.TASK_MANAGER, "verify-baseline", task["task_id"]],
            text=True, capture_output=True, timeout=60,
        )
    except Exception as exc:
        return "unavailable", type(exc).__name__
    if result.returncode != 0:
        return "unavailable", f"verify-baseline exit={result.returncode}"
    output = result.stdout or ""
    for line in output.splitlines():
        if line.startswith("HERDR_BASELINE_RESULT="):
            try:
                payload = ports.json.loads(line.split("=", 1)[1])
            except ValueError:
                return "unavailable", "invalid baseline JSON"
            if not isinstance(payload, dict) or not isinstance(payload.get("changes"), list):
                return "unavailable", "baseline changes list missing"
            return ("changed" if payload["changes"] else "no_changes"), ""
    if any(line == "TASK_CHANGED" or line.startswith("TASK_CHANGED:") for line in output.splitlines()):
        return "changed", ""
    return "unavailable", "baseline result missing"


def task_changes_recorded(ports, task):
    return ports.task_changes_evidence(task)[0] == "changed"

def try_auto_accept(ports, task_id):
    """非门禁节点规则化验收：变更铁证齐备即 completed，免总指挥回合。

    保守口径：只接管 agent_done 的非门禁节点；产物/变更证据不足、
    门禁节点、被显式关闭(HERDR_AUTO_ACCEPT=0)时返回 False，走原路径。
    """
    if not ports.auto_accept_enabled():
        return AutoAcceptResult(False, "auto_accept_disabled")

    task = ports.get_task(task_id)
    if not task or task.get("status") != "agent_done":
        return AutoAcceptResult(False, "task_not_agent_done")

    workflow_id = task.get("workflow_id")
    node_id = task.get("node") or task.get("stage")
    if not workflow_id or not node_id:
        return AutoAcceptResult(False, "task_identity_incomplete")

    if ports.node_is_gate(workflow_id, node_id):
        return AutoAcceptResult(False, "gate_node")
    if task.get("stage_verdict") == "blocked":
        return AutoAcceptResult(False, "stage_verdict_blocked")

    role_pattern = r"(?:^|[^a-z])(review(?:er)?|adversarial|challenger)(?:$|[^a-z])"
    explicit_role = " ".join(str(task.get(key) or "").lower() for key in ("dispatch_role", "agent_role", "role")).strip()
    if explicit_role:
        is_challenger = bool(ports.re.search(role_pattern, explicit_role))
    else:
        is_challenger = bool(ports.re.search(role_pattern, str(task.get("task_id") or "").lower()))

    wf_cfg = None
    if hasattr(ports, "workflow_config_for"):
        try:
            wf_cfg = ports.workflow_config_for(workflow_id)
        except Exception:
            wf_cfg = None

    from herdr.stage_skills import (
        is_staged_dev_flow_workflow,
        validate_requirements_deliverable_content,
        validate_plan_deliverable_content,
    )
    from herdr.workflow_docs import load_notes
    import os

    is_staged = is_staged_dev_flow_workflow(wf_cfg)

    if is_staged:
        if is_challenger and node_id != "plan":
            return AutoAcceptResult(False, "review_role")
        clone_path = task.get("clone_path") or ""
        role = "challenger" if is_challenger else "executor"

        if node_id == "requirements":
            deliverable_text = ""
            if clone_path:
                for fname in ("requirements.md", "spec.md", "PRD.md", "docs/requirements.md", "docs/spec.md"):
                    fpath = os.path.join(clone_path, fname)
                    if os.path.exists(fpath) and os.path.getsize(fpath) > 0:
                        try:
                            with open(fpath, "r", encoding="utf-8") as f:
                                deliverable_text = f.read()
                            break
                        except Exception:
                            pass
            if not deliverable_text:
                try:
                    notes = load_notes(workflow_id)
                    for n in notes:
                        if n.get("stale"):
                            continue
                        if n.get("task_id") == task_id or (task.get("agent") and n.get("agent") == task.get("agent")):
                            if n.get("node") == "requirements" or n.get("kind") in ("requirement", "spec"):
                                deliverable_text += "\n" + str(n.get("title", "")) + "\n" + str(n.get("body", ""))
                except Exception:
                    pass

            ok, missing = validate_requirements_deliverable_content(deliverable_text)
            if not ok:
                print(f"[AUTO ACCEPT REJECTED] task={task_id} node={node_id} requirements invalid: {missing}")
                return AutoAcceptResult(False, "requirements_missing_sections", missing=tuple(missing))

        elif node_id == "plan":
            from herdr.plan_parser import find_plan_deliverables
            task_copy = dict(task)
            task_copy.setdefault("task_id", task_id)
            task_copy.setdefault("node", "plan")
            task_copy.setdefault("workflow_id", workflow_id)
            if clone_path:
                task_copy.setdefault("clone_path", clone_path)

            deliv = find_plan_deliverables(workflow_id, tasks=[task_copy], project_root=clone_path)
            selected_note = getattr(deliv, "meta", {}).get("challenger_note" if is_challenger else "plan_note")
            if selected_note and selected_note.get("source") != "file":
                note_task = selected_note.get("task_id") or selected_note.get("author")
                if note_task and note_task != task_id:
                    print(f"[AUTO ACCEPT REJECTED] task={task_id} node={node_id} note {selected_note.get('note_id')} belongs to task {note_task}, not {task_id}")
                    return AutoAcceptResult(False, "plan_challenger_missing_sections" if is_challenger else "plan_missing_sections")

            deliverable_text = deliv.challenger_text if is_challenger else deliv.plan_text

            ok, missing = validate_plan_deliverable_content(deliverable_text, role=role)
            if not ok:
                print(f"[AUTO ACCEPT REJECTED] task={task_id} node={node_id} plan ({role}) invalid: {missing}")
                return AutoAcceptResult(False, "plan_challenger_missing_sections" if is_challenger else "plan_missing_sections", missing=tuple(missing))

            if is_challenger:
                from herdr.plan_parser import extract_challenger_verdict
                verdict, verdict_reason = extract_challenger_verdict(deliverable_text, task)
                if verdict == "blocked":
                    print(f"[AUTO ACCEPT REJECTED] task={task_id} node={node_id} challenger verdict blocked: {verdict_reason}")
                    return AutoAcceptResult(False, "challenger_blocked", detail=verdict_reason)
                if verdict != "pass":
                    print(f"[AUTO ACCEPT REJECTED] task={task_id} node={node_id} challenger verdict missing or non-pass: {verdict_reason}")
                    return AutoAcceptResult(False, "challenger_verdict_missing", detail=verdict_reason)

        else:
            evidence, detail = ports.task_changes_evidence(task)
            if evidence != "changed":
                reason = ("verification_unavailable" if evidence == "unavailable" else
                          "implementation_no_controlled_changes" if node_id == "implementation" else
                          "no_controlled_changes_other")
                return AutoAcceptResult(False, reason, detail)
    else:
        if is_challenger:
            return AutoAcceptResult(False, "review_role")
        evidence, detail = ports.task_changes_evidence(task)
        if evidence != "changed":
            return AutoAcceptResult(False, "verification_unavailable" if evidence == "unavailable"
                                    else "no_controlled_changes_other", detail)

    if not ports.set_task_status(
        task_id, "completed", expected_status="agent_done",
        expected_version=ports._task_version(task), source="herdr-controller:auto-accept",
        metadata={"auto_accept_reason": "controlled_changes", "acceptance_mode": "auto"},
    ):
        return AutoAcceptResult(False, "cas_mismatch")

    print(
        f"[AUTO ACCEPT] task={task_id} "
        f"node={node_id} -> completed"
    )
    return AutoAcceptResult(True)

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
 'auto_rework_attempts': (),
 'task_changes_evidence': ('TASK_MANAGER', 'json', 'subprocess'),
 'task_changes_recorded': ('task_changes_evidence',),
 'try_auto_accept': ('_task_version', 'auto_accept_enabled', 'get_task', 'node_is_gate', 're',
                     'set_task_status', 'task_changes_evidence', 'workflow_config_for'),
 'check_task_deliverables_ready': ('find_node', 'os', 'subprocess', 'workflow_config_for'),
 'gate_verdict_ready': ('node_is_gate', 'read_gate_verdict')}

__all__ = tuple(DEPENDENCIES)
