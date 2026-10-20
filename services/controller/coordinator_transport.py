"""Inspect, heal and compact coordinator panes; wait for and retry decisions.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def coordinator_pane_for_workflow(ports, workflow_id=None):
    if workflow_id:
        project = ports.project_for_workflow(workflow_id) or {}
        pane = project.get("coordinator_pane_id")
        if pane:
            return pane

    return None

def coordinator_status(ports, workflow_id=None, auto_heal=True):
    pane_id = ports.coordinator_pane_for_workflow(
        workflow_id
    )

    if not pane_id:
        return "unknown"

    try:
        output = ports.subprocess.check_output(
            [
                "herdr",
                "agent",
                "get",
                pane_id
            ],
            text=True
        )

        data = ports.json.loads(output)

        return (
            data["result"]["agent"]
            .get("agent_status", "unknown")
        )

    except Exception as e:
        print(
            f"[COORDINATOR STATUS ERROR] "
            f"workflow={workflow_id} "
            f"pane={pane_id}: {e}"
        )
        if auto_heal and workflow_id:
            try:
                proj = ports.project_for_workflow(workflow_id) or {}
                proj_id = proj.get("project_id")
                if proj_id and ports.ensure_coordinator_running(proj_id, pane_id):
                    print(
                        f"[COORDINATOR AUTO-HEAL SUCCESS] "
                        f"workflow={workflow_id} "
                        f"pane={pane_id}"
                    )
                    # Re-check status after starting
                    try:
                        out = ports.subprocess.check_output(
                            ["herdr", "agent", "get", pane_id],
                            text=True,
                        )
                        return ports.json.loads(out)["result"]["agent"].get("agent_status", "unknown")
                    except Exception:
                        pass
            except Exception as heal_exc:
                print(
                    f"[COORDINATOR AUTO-HEAL FAILED] "
                    f"workflow={workflow_id} "
                    f"pane={pane_id}: {heal_exc}"
                )
        return "unknown"

def coordinator_compact_enabled(ports):
    value = ports.os.environ.get("HERDR_COORDINATOR_COMPACT", "1")
    return value.strip().lower() not in ("0", "false", "off", "no")

def coordinator_agent_kind(ports, pane_id):
    """pane 内 agent 种类(herdr agent get -> result.agent.agent)。"""
    try:
        output = ports.subprocess.check_output(
            ["herdr", "agent", "get", pane_id],
            text=True
        )
        agent = (ports.json.loads(output).get("result") or {}).get("agent") or {}
        return agent.get("agent") or ""
    except Exception:
        return ""

def maybe_compact_coordinator(ports, workflow_id, reason="stage_advance"):
    """阶段/fix-loop 边界给总指挥下发 /compact(有界等待,失败不阻断)。

    仅对支持 /compact 的 agent kind 生效;总指挥忙时跳过(下个边界再补)。
    这一调用把长工作流的上下文峰值压回可控区间,直接缩短后续回合耗时。
    """
    if not ports.coordinator_compact_enabled():
        return False

    pane_id = ports.coordinator_pane_for_workflow(workflow_id)
    if not pane_id:
        return False

    kind = ports.coordinator_agent_kind(pane_id)
    if kind not in ports.COORDINATOR_COMPACT_AGENTS:
        print(
            f"[COORDINATOR COMPACT SKIP] "
            f"workflow={workflow_id} pane={pane_id} "
            f"kind={kind or 'unknown'} reason={reason}"
        )
        return False

    if ports.coordinator_status(workflow_id) not in ("idle", "done"):
        print(
            f"[COORDINATOR COMPACT SKIP] "
            f"workflow={workflow_id} pane={pane_id} "
            f"busy reason={reason}"
        )
        return False

    try:
        result = ports.subprocess.run(
            [
                "herdr",
                "agent",
                "prompt",
                pane_id,
                "/compact",
                "--wait",
                "--timeout",
                "300000"
            ],
            text=True,
            capture_output=True
        )
    except Exception as exc:
        print(
            f"[COORDINATOR COMPACT ERROR] "
            f"workflow={workflow_id} pane={pane_id}: {exc}"
        )
        return False

    if result.returncode == 0:
        print(
            f"[COORDINATOR COMPACT] "
            f"workflow={workflow_id} pane={pane_id} "
            f"kind={kind} reason={reason}"
        )
        return True

    # 空会话/无可压缩内容时 TUI 本地即时完成,herdr 观测不到 working,
    # 返回 agent_prompt_stalled——这是良性情形,不是故障(2026-09-18 实测)。
    detail = (result.stderr.strip() or result.stdout.strip())
    if "agent_prompt_stalled" in detail:
        print(
            f"[COORDINATOR COMPACT SKIP] "
            f"workflow={workflow_id} pane={pane_id} "
            f"no_activity reason={reason}"
        )
        return False

    print(
        f"[COORDINATOR COMPACT ERROR] "
        f"workflow={workflow_id} pane={pane_id}: {detail}"
    )
    return False

def handle_coordinator_delivery_stall(ports, item, task, elapsed):
    """总指挥投递 SLA 到期:记录 attention、通知、让位(不再无限 BUSY)。

    registry watcher 会依据 episode 的 next_retry_at 做慢速重试;
    总指挥恢复后事件会自然补投,不需要人工重启。
    """
    from herdr.coordinator_decisions import handle_delivery_stall

    return handle_delivery_stall(
        item,
        task,
        elapsed,
        attention_get=ports.attention_get,
        attention_note=ports.attention_note,
        attention_retry_interval=ports.liveness.attention_retry_interval,
        coordinator_pane_for_workflow=ports.coordinator_pane_for_workflow,
        notify_attention=ports.notify_attention,
    )

def wait_for_coordinator_decision(ports, task_id, timeout=None):
    """等待总指挥判定落盘(completed/rework/...)。

    默认预算与真实回合尾部耗时对齐(env HERDR_COORDINATOR_DECISION_TIMEOUT);
    超时不立即重试:记录 attention 退避,避免再烧一整轮 10 分钟级回合。
    """
    from herdr.coordinator_decisions import wait_for_decision

    return wait_for_decision(
        task_id,
        timeout,
        get_task=ports.get_task,
        attention_get=ports.attention_get,
        attention_note=ports.attention_note,
        coordinator_decision_timeout=ports.liveness.coordinator_decision_timeout,
        attention_retry_interval=ports.liveness.attention_retry_interval,
        clock=ports.time.time,
        sleep=ports.time.sleep,
    )

def retry_coordinator_decision(ports, task_id):
    from herdr.coordinator_decisions import retry_decision

    return retry_decision(
        task_id,
        get_task=ports.get_task,
        coordinator_pane_for_workflow=ports.coordinator_pane_for_workflow,
        coordinator_discipline=ports.COORDINATOR_DISCIPLINE,
        run_prompt=ports.subprocess.run,
        wait_for_decision=ports.wait_for_coordinator_decision,
    )


DEPENDENCIES = {'coordinator_pane_for_workflow': ('project_for_workflow',),
 'coordinator_status': ('coordinator_pane_for_workflow', 'ensure_coordinator_running', 'json',
                        'project_for_workflow', 'subprocess'),
 'coordinator_compact_enabled': ('os',),
 'coordinator_agent_kind': ('json', 'subprocess'),
 'maybe_compact_coordinator': ('COORDINATOR_COMPACT_AGENTS', 'coordinator_agent_kind',
                               'coordinator_compact_enabled', 'coordinator_pane_for_workflow',
                               'coordinator_status', 'subprocess'),
 'handle_coordinator_delivery_stall': ('attention_get', 'attention_note',
                                       'coordinator_pane_for_workflow', 'liveness',
                                       'notify_attention'),
 'wait_for_coordinator_decision': ('attention_get', 'attention_note', 'get_task', 'liveness',
                                   'time'),
 'retry_coordinator_decision': ('COORDINATOR_DISCIPLINE', 'coordinator_pane_for_workflow',
                                'get_task', 'subprocess', 'wait_for_coordinator_decision')}

__all__ = tuple(DEPENDENCIES)
