#!/usr/bin/env python3

"""Controller configuration, runtime resources and backwards-compatible service assembly."""

import json
import hashlib
import math
import re
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import threading
import time
import uuid
import sys


# Keep entry-relative paths and legacy import aliases stable.
HERDR_ROOT = Path(__file__).resolve().parent.parent
if str(HERDR_ROOT) not in sys.path:
    sys.path.insert(0, str(HERDR_ROOT))

SOCKET_PATH = os.path.expanduser("~/.config/herdr/herdr.sock")
TASKS_FILE = os.environ.get("TASKS_FILE") or os.path.expanduser("~/.herdr-controller/tasks.json")
_task_bin = HERDR_ROOT / "bin" / "herdr-task"
TASK_MANAGER = str(_task_bin) if _task_bin.exists() else os.path.expanduser("~/HAFlow/bin/herdr-task")

try:
    from herdr.projects import (
        project_for_workflow,
        workflow_config_for,
        ensure_coordinator_running,
    )
    from herdr.workflow import find_node, get_ready_nodes, is_workflow_completed
    from herdr.state_store import get_state_store
    from herdr.observation import ObservationStore, create_verification_observation_with_status
    from herdr.trajectory import (
        TrajectoryLedger,
        record_observation_created,
        record_trajectory_event,
    )
    from herdr import liveness
except ImportError:
    from herdr_projects import (
        project_for_workflow,
        workflow_config_for,
        ensure_coordinator_running,
    )
    from herdr_workflow import find_node, get_ready_nodes, is_workflow_completed
    from herdr_state_store import get_state_store
    from herdr_observation import ObservationStore, create_verification_observation_with_status
    from herdr.trajectory import (
        TrajectoryLedger,
        record_observation_created,
        record_trajectory_event,
    )
    from herdr import liveness


# Optional integrations retain their independent fallback behavior.
try:
    from herdr import direct_dispatch as direct_dispatch_planner
except Exception:
    # 纯函数决策模块缺失时退回总指挥路径,绝不阻塞控制面。
    direct_dispatch_planner = None

reverification_mod = None

try:
    from herdr import scheduler as scheduler_core
    from herdr import scheduler_facts as scheduler_facts_store
    from herdr import delivery_record as delivery_record_mod
    from herdr import workflow_docs as workflow_docs_mod
except Exception:
    # Scheduler v1 可选:缺失时退回原推进语义,绝不阻塞控制面。
    scheduler_core = None
    scheduler_facts_store = None
    delivery_record_mod = None
    workflow_docs_mod = None

try:
    from herdr import reverification as reverification_mod
except Exception:
    # Selective Reverification (PR #108) is optional on top of the scheduler:
    # without it every verifier re-runs, which is exactly the pre-#108
    # behaviour. A separate import block so a missing planner cannot also
    # disable the #107 candidate / join-gate machinery.
    reverification_mod = None

try:
    from herdr import selective_replan as selective_replan_core
except Exception:
    # Selective Replan (PR #110) is optional: without it every blocked gate
    # falls back to the legacy whole-node fix-loop, exactly the pre-#110
    # behaviour. A separate import block so a missing module cannot also
    # disable the #107/#108 machinery.
    selective_replan_core = None

try:
    from herdr.git_coordination import ensure_no_git_processes
except Exception:
    ensure_no_git_processes = None

try:
    from herdr.supervisor import harness as supervisor_harness
except Exception:
    # Semantic Supervisor 是可选观察层;缺失或异常时原有流程完全不变。
    supervisor_harness = None

try:
    from herdr.observer import harness as observer_harness
except Exception:
    # Trajectory Observer 是可选旁路观察层;缺失或异常时原有流程完全不变。
    observer_harness = None


# Configuration and state belong to this entry instance, not imported modules.
STAGE_STATE_FILE = os.environ.get("STAGE_STATE_FILE") or os.path.expanduser(
    "~/.herdr-controller/stage-state.json"
)
STAGE_POLICIES_FILE = os.path.expanduser(
    "~/.herdr-controller/stage-policies.json"
)
COORDINATOR_PANE = "w6:p1H"
WORKFLOWS_FILE = os.environ.get("WORKFLOWS_FILE") or os.path.expanduser("~/.herdr-controller/workflows.json")

# Liveness guard (SLA / attention episodes / hygiene)
ATTENTION_FILE = os.environ.get("HERDR_ATTENTION_FILE") or os.path.expanduser(
    "~/.herdr-controller/attention.json"
)

_attention_store = liveness.EpisodeStore(ATTENTION_FILE)

# 夹具/临时 workflow 只提示一次，避免每 2s sweep 刷屏。
_foreign_workflows_logged = set()

# 完成日志闩：close 失败也不得每 sweep 重刷 [WORKFLOW COMPLETE]。
_workflow_complete_logged = set()

# 监听订阅退避：task_id -> (attempt, next_allowed_at, status_signature)
_listener_backoff = {}
_listener_giveup_logged = set()

# 终化重试耗尽的任务:只告警一次,避免每轮 sweep 刷屏。
_finalize_retry_exhausted_logged = set()

# Blocked 观测的 CAS 风暴护栏：task_id -> 该样本的 observed_version。
# stale 只提示一次(等 Sentinel 补新样本)，非 stale 拒绝每样本只记一条事件。
_blocked_observation_stale = {}
_blocked_observation_rejected = {}

# git 终化未收敛而推迟 close 的 workflow:只提示一次,避免每 sweep 刷屏。
_close_deferred_logged = set()

# 已触发过 close-workflow 的 workflow,防止轮询期间重复派发。
_workflow_close_inflight = set()

# Keep blocked-SLA execution and deduplication local to this entry instance.
_BLOCKED_SLA_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="blocked-sla",
)
_blocked_sla_scheduled = set()
_blocked_sla_schedule_lock = threading.Lock()


# Gate verdicts & fix-loop
# 常见门禁阶段的内置默认;显式配置(workflow 节点 gate / stage-policies.json)
# 优先于这里。verdict 缺失(lenient)时门禁不生效,存量 workflow 行为不变。
GATE_DEFAULTS = {
    "test": {"retry_node": "implementation"},
    "review": {"retry_node": "implementation"},
    "wrapup": {"retry_node": "implementation"},
}
FIX_LOOP_MAX = int(os.environ.get("HERDR_FIX_LOOP_MAX", "3"))

# 可作废状态集合,必须与 herdr/transitions.py TASK_TRANSITIONS 中
# 允许 → superseded 的状态保持一致(pending/committed/integrated 除外)。
FIX_LOOP_SUPERSEDEABLE = {
    "dispatched",
    "working",
    "blocked",
    "paused",
    "interrupted",
    "agent_done",
    "rework",
    "cleaned",
    "failed",
}


class SelectiveInvalidationResult(list):
    """选择性作废的结构化返回(仍是 list,兼容 legacy 调用方)。

    P1-1: selective 作废不能只返回 invalidated=[...]。部分失败时
    调用方必须能判断 all_targets_applied / pending / failed,
    否则会带着 [B,C] 的 latch 去等一个永远不会产生的 C-r2。
    legacy 路径( selective_target_task_ids is None )仍返回普通 list。
    """

    def __new__(cls, invalidated=None, *, applied=None, pending=None,
                failed=None):
        obj = super().__new__(cls, list(invalidated or []))
        obj.applied = list(applied or [])
        obj.pending = list(pending or [])
        obj.failed = dict(failed or {})
        obj.all_targets_applied = not obj.pending
        return obj


REWORKABLE_STATUSES = {"blocked", "agent_done", "working", "rework", "paused", "interrupted"}

# 基础设施失败自动补派 (auto-recover)
# 仅这些失败原因属于"基础设施类"(投递熔断 / 进程崩溃),可自动作废补派;
# 质量类失败结论(如总指挥判定 failed)绝不自动翻案。
AUTO_RECOVER_REASONS = {"dispatch_delivery_fuse", "agent_process_crash"}
AUTO_RECOVER_MAX_ATTEMPTS = int(os.environ.get("HERDR_AUTO_RECOVER_MAX", "2"))

# 终化重试上限：commit 门禁瞬时失败(flaky)/集成冲突时按注意力退避自动重试；
# 达到上限后停止自动重试并升级人工(避免 gate 永久失败造成的无限重试风暴)。
FINALIZE_RETRY_MAX = int(os.environ.get("HERDR_FINALIZE_RETRY_MAX", "5"))

# Process-local queue, locking and worker resources preserve their identities.
listeners = {}
task_sockets = {}
lock = threading.Lock()
coordinator_queue = queue.Queue()
queued_events = set()

# Per-workflow concurrency: each workflow gets its own execution slot so that
# a blocked coordinator for workflow A cannot stall dispatch for workflow B.
from herdr.coordinator_runtime import CoordinatorDispatchRuntime

_coordinator_runtime = CoordinatorDispatchRuntime()
_wf_dispatch_locks = _coordinator_runtime.workflow_locks
_wf_dispatch_locks_meta = _coordinator_runtime.lock_creation_lock
_coordinator_executor = _coordinator_runtime.executor

_COMPLETION_SCHEDULING_CURSORS = {}
DEFAULT_GATE_HANDOFF_GRACE_SECONDS = 300.0
DEFAULT_MAX_CLOCK_SKEW_SECONDS = 5.0

# 单个 launch 必须有界:子进程僵死时不得永久占用调度线程
# 与 per-workflow 锁,超时后走既有 PARTIAL→总指挥回退路径。
DIRECT_DISPATCH_LAUNCH_TIMEOUT = 300
SUPERVISOR_VERIFY_DISPATCH_TIMEOUT = 120

# Selective Reverification v1:选择性重新验证 (HAFlow PR #108)
#
# Task states from which a recorded verdict is a settled result. ``superseded``
# belongs here too: superseding is a status change, not a rewrite — it means
# "no longer the current conclusion", and the record of what the task verified
# has to survive for the reuse evidence to be reachable at all. A task that
# never reached one of these states also has no verdict, so it is refused
# twice over (by this filter and by the verdict check).
_SETTLED_VERIFICATION_STATUSES = frozenset((
    "completed", "committed", "integrated", "cleanup_ready", "cleaned",
    "superseded",
))

#
#
# - _reverification_gate_facts:读当前候选上的 reuse 事实(只读,失败即空)。
# - _reverification_satisfies_node:该 verifier 是否已被复用事实满足。
# - _reverification_plan_for_rotation:候选轮换时构建并落盘重新验证计划。
# - _reverification_reused_node:该节点本次不派发(复用是调度决策,不是 Agent 决策)。
#: Per-sweep memo, keyed by workflow: the resolved policy context *and* the
#: reuse facts derived from it, resolved together.
#:
#: Memoised because ``workflow_config_for`` re-reads and re-parses the workflow
#: YAML from disk on every call, and this path runs once per node per 2-second
#: sweep — without the memo, readiness paid for a full YAML parse per node.
#: Cleared at the top of every sweep, so a fact written by this sweep (the plan)
#: is picked up by the readiness computation in the same pass and a later sweep
#: never trusts a previous pass's snapshot.
_REVERIFICATION_MEMO = {}

#: Bound on memoised workflows, so a long-lived controller that keeps sweeping
#: many workflows does not accumulate an entry for each forever.
_REVERIFICATION_MEMO_LIMIT = 256

# fix-loop 作废闩去重日志:闩存续期间只提示一次,清除后下轮可再提示。
_fix_latch_logged = set()

_CONTINUATION_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="continuation")
_continuation_scan_lock = threading.Lock()
_continuation_scan_at = 0.0


# 总指挥上下文卫生 & 效率纪律
#
# 背景(2026-09-17 复盘):wf-nexusarchive-0917-01 的总指挥会话上下文从
# 94K 一路涨到 684K,后期单回合纯 LLM 生成 27-58min;该工作流的控制面
# 事件全部串行等待这些长回合(累计 BUSY 2.35h)。阶段/fix-loop 边界压缩
# 上下文,可把后续回合耗时拉回分钟级;效率纪律约束"越权重活"与
# "决策后继续空转"。
COORDINATOR_COMPACT_AGENTS = {"opencode", "claude"}
COORDINATOR_DISCIPLINE = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
效率纪律(硬约束)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. 决策一旦落盘(herdr-task set ...),立即结束本回合;
   禁止在决策后继续探索、验证或执行任何命令。
2. 禁止运行重操作: herdr-task commit / integrate / cleanup、
   全量测试、npm/mvn 构建等;交付集成由 Controller 自动完成,
   你只做验收判定与必要修复指引。
3. 只读核验优先: verify-baseline / verify-metrics / 读取产物文件;
   禁止整段读取 Pane 全文。
4. 派发前核对任务文件范围与实际仓库提交/CI要求。将必要工程产物及已授权检查
   登记到节点 delivery_contract（version=1、allowed_paths、required_files、checks、auto_rework）；
   范围冲突须先明确裁决，不用“只改代码”排除门禁必需的测试或复盘。
   执行与配置说明见 docs/guides/task-delivery-contract.md；缺配置不宣称机器验收已覆盖仓库门禁。
5. 若你判断当前上下文已明显过大,先执行 /compact 再继续本事件。
""".strip()


# 门禁规则化裁决 (auto-verdict) — 门禁节点免除总指挥 LLM 回合
# 契约：门禁任务写入门禁结论文件，并在终端输出 HERDR_GATE_VERDICT: pass|blocked
# （可选 HERDR_GATE_NOTE: <原因>）。结论文件默认落在 clone 外状态目录
# （~/.herdr-controller/gate-verdicts/<task_id>.json），避免被 herdr-task commit
# 带进交付；为兼容旧契约与权限受限场景，clone 内 .herdr/gate-verdict.json 仍可读。
# 两个通道结论一致时直接落 verdict；缺失或互相矛盾一律回落总指挥。
GATE_VERDICT_FILE = ".herdr/gate-verdict.json"
GATE_VERDICT_MARKER = "HERDR_GATE_VERDICT:"
GATE_NOTE_MARKER = "HERDR_GATE_NOTE:"
_GATE_VERDICT_ALIASES = {
    "pass": "pass",
    "passed": "pass",
    "ok": "pass",
    "blocked": "blocked",
    "block": "blocked",
    "fail": "blocked",
    "failed": "blocked",
}
_NODE_DONE_STATUSES = frozenset({
    "completed", "committed", "integrated", "cleanup_ready", "cleaned",
})
_INTERVENTION_EXECUTION_OWNER = f"controller:{os.getpid()}:{uuid.uuid4()}"

# Awake guard (长跑 workflow 防休眠)
_awake_guard_proc = None



# Main
def report_integration_gaps():
    """集成健康检查:lifecycle 集成缺失时 herdr 退化为屏幕探测,
    残影文本会造成 agent 状态永久误判(卡死根因之一),必须在启动时大声暴露。"""
    try:
        result = subprocess.run(
            ["herdr", "integration", "status"],
            text=True,
            capture_output=True,
            timeout=30,
        )
        status_text = result.stdout or result.stderr
    except Exception as exc:
        print(f"[INTEGRATION CHECK ERROR] {exc}")
        return

    kinds = {t.get("agent") for t in load_tasks() if t.get("agent")}
    for gap in liveness.integration_gaps(status_text, kinds):
        print(
            f"[INTEGRATION GAP] "
            f"agent={gap['agent']} "
            f"integration={gap['integration']} "
            f"not installed ({gap['status']}) -> "
            f"状态将退化为屏幕探测,可能误判卡死; "
            f"修复: herdr integration install {gap['integration']}"
        )


def main():
    try:
        from herdr import service_release
        import_root = Path(service_release.__file__).resolve().parent.parent
        fingerprint = service_release.runtime_fingerprint(
            import_root, [Path(__file__).resolve(), Path(service_release.__file__).resolve(),
                          import_root / "herdr" / "state_store.py", import_root / "herdr" / "kernel.py",
                          *sorted((import_root / "services" / "controller").glob("*.py")),
                          import_root / "herdr" / "completion_controller.py",
                          *sorted((import_root / "herdr").glob("coordinator_*.py"))],
        )
    except Exception:
        fingerprint = {"running_sha": "unknown", "running_import_root": "unknown", "component_versions": {}}
    print("HERDR_RUNTIME_FINGERPRINT=" + json.dumps(
        {"service": "com.user.herdr-controller", "pid": os.getpid(), **fingerprint}, sort_keys=True,
    ), flush=True)
    print("[CONTROLLER V12] starting")
    print(f"[REGISTRY] {TASKS_FILE}")
    print(
        f"[COORDINATOR] "
        f"{COORDINATOR_PANE}"
    )
    print(
        "[QUEUE] coordinator event "
        "serialization enabled"
    )

    report_integration_gaps()

    reset_queued_stage_states()

    worker = threading.Thread(
        target=coordinator_worker,
        daemon=True
    )
    worker.start()

    registry_watcher()


# Compatibility assembly: implementations depend only on declared inputs.
from services.controller.bindings import install_modules

install_modules(globals(), (
    "advancement",
    "attention",
    "auto_acceptance",
    "auto_verdict",
    "blocked_actions",
    "blocked_claims",
    "blocked_records",
    "candidate",
    "collaboration",
    "context_compaction",
    "continuation",
    "coordinator_events",
    "coordinator_prompts",
    "coordinator_queue",
    "coordinator_transport",
    "direct_dispatch",
    "done_events",
    "finalize_retry",
    "finalize_support",
    "fix_delivery",
    "fix_invalidation",
    "fix_loop",
    "fix_notification",
    "git_finalize",
    "handoffs",
    "infra_recovery",
    "join_gates",
    "merged_fix_loop",
    "node_policy",
    "persistence",
    "registry",
    "reverification",
    "runtime_reconciliation",
    "selective_facts",
    "selective_plan",
    "stage_delivery",
    "stage_state",
    "supervisor_actions",
    "supervisor_checkpoints",
    "supervisor_evidence",
    "supervisor_execution",
    "task_events",
    "task_listener",
    "task_state",
    "task_testing",
    "verdict_evidence",
    "workflow_close",
    "workflow_recovery",
))

if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print(
            "\n[CONTROLLER V12] stopped"
        )
