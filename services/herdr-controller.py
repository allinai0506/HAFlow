#!/usr/bin/env python3

import json
import hashlib
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

try:
    from herdr import direct_dispatch as direct_dispatch_planner
except Exception:
    # 纯函数决策模块缺失时退回总指挥路径,绝不阻塞控制面。
    direct_dispatch_planner = None

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

STAGE_STATE_FILE = os.environ.get("STAGE_STATE_FILE") or os.path.expanduser(
    "~/.herdr-controller/stage-state.json"
)

STAGE_POLICIES_FILE = os.path.expanduser(
    "~/.herdr-controller/stage-policies.json"
)

COORDINATOR_PANE = "w6:p1H"

WORKFLOWS_FILE = os.environ.get("WORKFLOWS_FILE") or os.path.expanduser("~/.herdr-controller/workflows.json")

# ============================================================
# Liveness guard (SLA / attention episodes / hygiene)
# ============================================================

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

# git 终化未收敛而推迟 close 的 workflow:只提示一次,避免每 sweep 刷屏。
_close_deferred_logged = set()

# 已触发过 close-workflow 的 workflow,防止轮询期间重复派发。
_workflow_close_inflight = set()


def attention_get(key):
    return _attention_store.get(key)


def attention_note(key, task, event_type, reason, attempts=None, next_retry_at=None, detail=None):
    """Record/refresh an attention episode for a stalled or undeliverable event."""
    episode = _attention_store.get(key) or {}
    now = time.time()
    fields = {
        "task_id": task.get("task_id"),
        "workflow_id": task.get("workflow_id"),
        "event_type": event_type,
        "reason": reason,
        "last_attempt_at": now,
    }
    if attempts is not None:
        fields["attempts"] = int(attempts)
    if next_retry_at is not None:
        fields["next_retry_at"] = float(next_retry_at)
    if detail:
        fields["detail"] = detail
    if not episode.get("first_seen_at"):
        fields["first_seen_at"] = now
    return _attention_store.upsert(key, fields)


def attention_clear(key):
    return _attention_store.clear(key)


def attention_blocks_retry(key, now=None):
    return liveness.blocks_retry(_attention_store, key, now)


def attention_throttle(key, interval=None, now=None):
    liveness.throttle_retry(_attention_store, key, interval=interval, now=now)


def notify_attention(title, task, message, reason):
    """Best-effort macOS notification; never let notifier failure break control flow."""
    try:
        import importlib

        notifier = importlib.import_module("services.herdr-notifier")
        url = notifier.build_console_url(
            workflow_id=task.get("workflow_id"), task_id=task.get("task_id")
        )
        notifier.notify(title, f"{task.get('workflow_id', 'unknown')} · {reason}", message, url=url)
    except Exception as exc:
        print(f"[ATTENTION NOTIFY ERROR] {exc}")


def _get_store():
    if os.environ.get("HERDR_STATE_DB"):
        return get_state_store(Path(os.environ["HERDR_STATE_DB"]))
    t_file = globals().get("TASKS_FILE") or os.environ.get("TASKS_FILE")
    if t_file:
        p = Path(t_file).parent / "state.db"
        if p.parent.exists():
            return get_state_store(db_path=p)
    w_file = globals().get("WORKFLOWS_FILE") or os.environ.get("WORKFLOWS_FILE")
    if w_file:
        p = Path(w_file).parent / "state.db"
        if p.parent.exists():
            return get_state_store(db_path=p)
    return get_state_store()


def _workflow_entry(workflow_id):
    store = _get_store()
    wf = store.get_workflow(workflow_id)
    if wf:
        return wf
    return {}


def workflow_closed(workflow_id):
    """Registry entry reached a terminal (completed) status."""
    return _workflow_entry(workflow_id).get("status") == "completed"


def git_finalize_pending_tasks(workflow_id):
    """completed/committed 且 git 集成的任务仍在 commit/integrate 终化管线中。

    自动 close 若抢先推进到 cleaned,在跑的 `herdr-task commit` 子进程随后
    撞 'cleaned -> committed' 非法转移,交付分支落不进集成链路
    (2026-09-17 wf-nexusarchive-0917-01 wrapup 实测)。终化由其有界重试
    自会收敛(耗尽则升级总指挥);收敛后下一轮 sweep 再 close。
    """
    try:
        tasks = load_tasks()
    except Exception:
        return []

    return [
        t.get("task_id")
        for t in (tasks or [])
        if t.get("workflow_id") == workflow_id
        and t.get("status") in ("completed", "committed")
        and (t.get("integration_mode") or "none") == "git"
        and not t.get("finalize_escalated")
    ]


def git_escalated_tasks(workflow_id):
    """H-1: machine-escalated git tasks still holding undelivered commits.

    Escalation is observability + retry-stop, never a closability proof.
    Auto-close must defer on these; manual close requires explicit
    ``--accept-escalated``/``--force``/``--abandon`` or ``supersede``.
    """
    try:
        tasks = load_tasks()
    except (OSError, ValueError, RuntimeError, AttributeError,
            subprocess.SubprocessError):
        return []
    return [
        t.get("task_id")
        for t in (tasks or [])
        if t.get("workflow_id") == workflow_id
        and t.get("status") in ("completed", "committed")
        and (t.get("integration_mode") or "none") == "git"
        and t.get("finalize_escalated")
    ]


def maybe_close_completed_workflow(workflow_id):
    """Workflow 全部节点完成后,自动执行物理收尾(关 pane/删 clone/归档)。

    close-workflow 自带幂等与终态闸门;这里只负责防重入派发。
    """
    if not workflow_id or workflow_id in _workflow_close_inflight:
        return

    entry = _workflow_entry(workflow_id)

    # 夹具/临时 workflow 严禁物理收尾(会去操作不存在的 clone/pane)。
    if liveness.workflow_is_foreign(entry):
        return

    if entry.get("status") == "completed":
        return
    # Fix-loop reopen 闩:重开后的 workflow 在首个任务进入 ACTIVE
    # 之前,旧任务仍全为完成系,必须挡住 sweep 的自消除 close。
    if entry.get("suppress_auto_close"):
        return

    # git 终化在途:必须等 commit/integrate 收敛,close 不得抢先物理收尾。
    pending_git = git_finalize_pending_tasks(workflow_id)
    if pending_git:
        if workflow_id not in _close_deferred_logged:
            _close_deferred_logged.add(workflow_id)
            print(
                f"[CLOSE DEFERRED] workflow={workflow_id} "
                f"waiting git finalize: {','.join(pending_git)}"
            )
        return

    # H-1: auto-close never carries human confirmation, so escalated git
    # tasks (retry-exhausted / refused / main-dirty) must also defer.
    # Silent delivered with a never-integrated committed task is the exact
    # failure this blocks.
    escalated_git = git_escalated_tasks(workflow_id)
    if escalated_git:
        if workflow_id not in _close_deferred_logged:
            _close_deferred_logged.add(workflow_id)
            print(
                f"[CLOSE DEFERRED] workflow={workflow_id} "
                f"waiting human confirm for escalated: "
                f"{','.join(escalated_git)}"
            )
        return

    _workflow_close_inflight.add(workflow_id)

    def _run():
        try:
            result = subprocess.run(
                [TASK_MANAGER, "close-workflow", workflow_id],
                text=True,
                capture_output=True,
                timeout=900,
            )
            if result.stdout.strip():
                print(result.stdout.strip())
            if result.returncode != 0:
                print(
                    f"[WORKFLOW CLOSE ERROR] "
                    f"workflow={workflow_id}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
        finally:
            _workflow_close_inflight.discard(workflow_id)

    threading.Thread(
        target=_run,
        daemon=True,
        name=f"wf-close-{workflow_id}",
    ).start()


# ============================================================
# Gate verdicts & fix-loop
# ============================================================

# 常见门禁阶段的内置默认;显式配置(workflow 节点 gate / stage-policies.json)
# 优先于这里。verdict 缺失(lenient)时门禁不生效,存量 workflow 行为不变。
GATE_DEFAULTS = {
    "test": {"retry_node": "implementation"},
    "review": {"retry_node": "implementation"},
    "wrapup": {"retry_node": "implementation"},
}

FIX_LOOP_MAX = int(os.environ.get("HERDR_FIX_LOOP_MAX", "3"))

# 可作废状态集合,必须与 bin/herdr-task TRANSITIONS 中
# 允许 → superseded 的状态保持一致(pending/committed/integrated 除外)。
FIX_LOOP_SUPERSEDEABLE = {
    "dispatched",
    "working",
    "blocked",
    "agent_done",
    "rework",
    "cleaned",
    "failed",
}


def resolve_gate_config(node, node_id):
    gate = (node or {}).get("gate") or get_stage_policy(node_id).get("gate")

    if gate is None:
        gate = GATE_DEFAULTS.get(node_id)

    if not gate:
        return None

    return {
        "retry_node": gate.get("retry_node", "implementation"),
        "max_loops": int(gate.get("max_loops", FIX_LOOP_MAX)),
    }


def gate_verdict(workflow_id, node_id):
    """Fail-safe 门禁结论:任一未作废任务的 blocked 结论即 blocked。"""
    verdict = None

    for task in load_tasks():
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


def latest_branch_for_node(workflow_id, node_id):
    best = None

    for task in load_tasks():
        if task.get("workflow_id") != workflow_id:
            continue
        if node_id not in (task.get("node"), task.get("stage")):
            continue
        if not task.get("branch"):
            continue
        if best is None or task.get("updated_at", 0) > best.get("updated_at", 0):
            best = task

    return best.get("branch") if best else None


def shared_docs_block(workflow_id, node_id, related_nodes=(), project_ctx=None):
    """跨节点共享文档区注入块:只读上下文 + 追加写入指引(best-effort)。"""
    try:
        from herdr import workflow_docs as wd
        notes = wd.load_notes(workflow_id)
    except Exception as exc:
        print(f"[SHARED DOCS WARN] load {workflow_id}: {exc}")
        return ""

    current_base_sha = None
    ctx = project_ctx or {}
    project_root = ctx.get("project_root") or ""
    base_branch = ctx.get("base_branch") or ""
    if project_root and base_branch:
        try:
            result = subprocess.run(
                ["git", "-C", project_root, "rev-parse", "--short", base_branch],
                text=True,
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                current_base_sha = result.stdout.strip() or None
        except Exception:
            current_base_sha = None

    try:
        return wd.render_context_block(
            workflow_id,
            notes,
            node_id=node_id,
            related_nodes=related_nodes,
            current_base_sha=current_base_sha,
        )
    except Exception as exc:
        print(f"[SHARED DOCS WARN] render {workflow_id}: {exc}")
        return ""


def _record_invalidation_note(workflow_id, gate_node_id, retry_node, nodes, invalidated):
    """fix-loop 作废时写下 controller 机器证据,供 stale 计算与下游阅读。"""
    try:
        from herdr import workflow_docs as wd
        wd.append_note(
            workflow_id,
            kind="invalidation",
            title=(
                f"fix-loop 作废: gate={gate_node_id} "
                f"retry={retry_node or gate_node_id}"
            ),
            body="被作废任务: " + ", ".join(sorted(invalidated)),
            node=gate_node_id,
            source=wd.SOURCE_CONTROLLER,
            invalidates=sorted(node for node in nodes if node),
        )
    except Exception as exc:
        print(f"[FIX LOOP NOTE WARN] {exc}")


def _collect_downstream_nodes(nodes_by_id, root_id):
    """root 节点自身 + 传递闭包的全部下游节点。"""
    dependents = {}

    for node in nodes_by_id.values():
        for dep in node.get("depends_on", []):
            dependents.setdefault(dep, set()).add(node["id"])

    seen = {root_id}
    frontier = [root_id]

    while frontier:
        current = frontier.pop()
        for nxt in dependents.get(current, ()):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)

    return seen


def _fix_loop_keepable(task):
    """verdict=pass 的任务可否保留:需已落定或无需 Git 集成。"""
    status = task.get("status")
    if status in ("cleaned", "cleanup_ready", "integrated", "committed"):
        return True
    if status == "completed" and (task.get("integration_mode") or "none") != "git":
        return True
    return False


def invalidate_for_fix_loop(workflow_id, gate_node_id, workflow_cfg, retry_node=None):
    """作废 gate 节点及其全部下游、以及 retry_node 到 gate 间全部中间节点的非 superseded 任务(fix-loop 回流前提)。

    completed/cleanup_ready 中间态先 finalize 规范化到 cleaned——
    completed→superseded 会被状态机拒绝;pending 不可作废,跳过。
    """
    nodes_by_id = {
        n["id"]: n for n in workflow_cfg.get("nodes", [])
    }

    if not nodes_by_id and workflow_cfg.get("stages"):
        # normalize_workflow 总会合成 nodes;此处兜底纯 stages 配置:
        # 用 next 链重建线性依赖,保证下游作废闭包仍然成立。
        prev = None
        for stage in workflow_cfg.get("stages", []):
            stage_id = stage.get("key") or stage.get("id")
            nodes_by_id[stage_id] = {
                "id": stage_id,
                "depends_on": [prev] if prev else [],
            }
            prev = stage_id

    node_ids = set(_collect_downstream_nodes(nodes_by_id, gate_node_id))
    if retry_node and retry_node in nodes_by_id:
        downstream_retry = _collect_downstream_nodes(nodes_by_id, retry_node) - {retry_node}
        node_ids = node_ids | downstream_retry

    supersedeable = FIX_LOOP_SUPERSEDEABLE
    invalidated = []
    invalidated_nodes = set()

    for task in load_tasks():
        if task.get("workflow_id") != workflow_id:
            continue
        if (
            task.get("node") not in node_ids
            and task.get("stage") not in node_ids
        ):
            continue

        status = task.get("status")
        task_id = task["task_id"]
        task_node = task.get("node") or task.get("stage")

        if status == "superseded":
            continue

        # 返工只重跑受影响子集:仅在门禁自身重跑(not retry_node 或 retry_node == gate_node_id)时,
        # 门禁节点内 verdict=pass 且已落定的任务保留;
        # 若跨阶段回流(如 review 回流 implementation),上游已变更,门禁与中间节点任务不可复用。
        if (
            task_node == gate_node_id
            and (not retry_node or retry_node == gate_node_id)
            and task.get("stage_verdict") == "pass"
            and _fix_loop_keepable(task)
        ):
            print(
                f"[FIX LOOP SUBSET KEEP] task={task_id} "
                f"verdict=pass preserved"
            )
            continue

        if status in ("completed", "cleanup_ready"):
            result = subprocess.run(
                [TASK_MANAGER, "finalize", task_id],
                text=True,
                capture_output=True,
            )
            if result.returncode != 0:
                print(
                    f"[FIX LOOP INVALIDATE ERROR] finalize {task_id}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
                continue
            status = (get_task(task_id) or {}).get("status")

        if status not in supersedeable:
            print(
                f"[FIX LOOP INVALIDATE SKIP] task={task_id} "
                f"status={status} cannot be superseded"
            )
            continue

        result = subprocess.run(
            [
                TASK_MANAGER, "supersede", task_id,
                "--reason", f"fix-loop: gate {gate_node_id} blocked",
            ],
            text=True,
            capture_output=True,
        )

        if result.returncode == 0:
            invalidated.append(task_id)
            if task_node:
                invalidated_nodes.add(task_node)
        else:
            print(
                f"[FIX LOOP INVALIDATE ERROR] supersede {task_id}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

    if invalidated:
        void_nodes = invalidated_nodes | {gate_node_id}
        if retry_node:
            void_nodes.add(retry_node)
        _record_invalidation_note(
            workflow_id, gate_node_id, retry_node, void_nodes, invalidated
        )

    return invalidated


def _bump_fix_loop_count(workflow_id, retry_node):
    with lock:
        state = load_stage_state()
        key = f"{workflow_id}|fixloop|{retry_node}"
        count = int(state.get(key, 0)) + 1
        state[key] = count
        save_stage_state(state)
    return count


def _fix_loop_count(workflow_id, retry_node):
    with lock:
        state = load_stage_state()
        try:
            return int(state.get(f"{workflow_id}|fixloop|{retry_node}", 0))
        except (TypeError, ValueError):
            return 0


def _fix_loop_exhaustion_episode(workflow_id, gate_node_id):
    return attention_get(f"{workflow_id}:fix_loop_exhausted:{gate_node_id}")


def _exhaustion_fingerprint(episode):
    if not isinstance(episode, dict):
        return None
    try:
        detail = json.loads(episode.get("detail") or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(detail, dict):
        return None
    return detail.get("fingerprint")


def _record_fix_loop_exhaustion(
    workflow_id, gate_node_id, retry_node, reason, summary, fingerprint
):
    import json as _json

    record = dict(summary)
    record["fingerprint"] = fingerprint
    attention_note(
        f"{workflow_id}:fix_loop_exhausted:{gate_node_id}",
        {"task_id": f"fix_loop:{gate_node_id}:{retry_node}",
         "workflow_id": workflow_id},
        "fix_loop",
        reason=reason,
        attempts=_fix_loop_count(workflow_id, retry_node),
        detail=_json.dumps(record, ensure_ascii=False)[:2000],
    )


# ============================================================
# 基础设施失败自动补派 (auto-recover)
# ============================================================

# 仅这些失败原因属于"基础设施类"(投递熔断 / 进程崩溃),可自动作废补派;
# 质量类失败结论(如总指挥判定 failed)绝不自动翻案。
AUTO_RECOVER_REASONS = {"dispatch_delivery_fuse", "agent_process_crash"}
AUTO_RECOVER_MAX_ATTEMPTS = int(os.environ.get("HERDR_AUTO_RECOVER_MAX", "2"))

# 终化重试上限：commit 门禁瞬时失败(flaky)/集成冲突时按注意力退避自动重试；
# 达到上限后停止自动重试并升级人工(避免 gate 永久失败造成的无限重试风暴)。
FINALIZE_RETRY_MAX = int(os.environ.get("HERDR_FINALIZE_RETRY_MAX", "5"))


def should_retry_finalize(status, episode, now, max_attempts=None):
    """(should_retry, reason, exhausted) — committed/completed 的终化重试决策。

    `completed` 且 integration_mode=git 的任务若 commit 门禁瞬时失败，此前
    没有任何重试路径，只能人工或总指挥手工重试（实测一次 flaky gate 浪费
    55min 并烧掉总指挥大回合）。本函数给出统一的退避重试 + 上限判定。
    """
    if status not in ("committed", "completed"):
        return False, "", False

    limit = FINALIZE_RETRY_MAX if max_attempts is None else int(max_attempts)
    attempts = int((episode or {}).get("attempts") or 0)
    if attempts >= limit:
        return False, "", True

    if float((episode or {}).get("next_retry_at") or 0) > now:
        return False, "", False
    reason = "integration_retry" if status == "committed" else "commit_retry"
    return True, reason, False


def recover_infra_failed_tasks(workflow_id, tasks=None):
    """基础设施失败任务自动作废,交给 sweep 重新派发替代任务。

    此前 failed 任务只能等人工 relaunch(--supersedes),实测造成 28 分钟级
    空等;这里复用既有 superseded->replace 派发管线:作废后清掉该节点的
    stage-advance 闩,下一轮 sweep 的 direct dispatch 会自动补派 -rN。
    谱系级次数上限由纯函数 select_infra_failures_for_recovery 保证,
    达到上限后不再自动处置(只留日志与告警)。
    """
    if not workflow_id:
        return False

    if workflow_closed(workflow_id):
        return False

    wf_st = _workflow_entry(workflow_id).get("status")
    if wf_st in ("completed", "paused"):
        return False

    if tasks is None:
        tasks = load_tasks()

    candidates = liveness.select_infra_failures_for_recovery(
        [t for t in tasks if t.get("workflow_id") == workflow_id],
        AUTO_RECOVER_REASONS,
        max_attempts=AUTO_RECOVER_MAX_ATTEMPTS,
    )

    recovered = False
    for task in candidates:
        task_id = task.get("task_id")
        node_id = task.get("node") or task.get("stage")

        result = subprocess.run(
            [
                TASK_MANAGER, "supersede", task_id,
                "--reason", "auto-recover: infrastructure failure",
            ],
            text=True,
            capture_output=True,
        )

        if result.returncode != 0:
            print(
                f"[AUTO RECOVER ERROR] supersede {task_id}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
            continue

        if node_id:
            clear_stage_advance(workflow_id, node_id)

        recovered = True
        print(
            f"[AUTO RECOVER] workflow={workflow_id} "
            f"node={node_id} task={task_id} "
            "superseded -> pending redispatch by sweep"
        )

    return recovered


def blocked_verdict_dep(workflow_id, node):
    """依赖中是否存在任务级 blocked 验收结论(不问 gate_cfg)。

    plan/requirements 等节点没有 gate 配置,sweep 的 fix-loop 看不见它们,
    但自动推进同样不得越过 blocked 结论。返回首个被阻断的依赖 id。
    """
    for dep in ((node or {}).get("depends_on") or []):
        if gate_verdict(workflow_id, dep) == "blocked":
            return dep
    return None


def blocked_gate_dependency(workflow_id, ready_node, workflow_cfg):
    """ready_node 的依赖中是否存在 verdict=blocked 的门禁节点。"""
    nodes_by_id = {
        n["id"]: n for n in workflow_cfg.get("nodes", [])
    }

    for dep in ready_node.get("depends_on", []):
        gate_cfg = resolve_gate_config(nodes_by_id.get(dep), dep)

        if not gate_cfg:
            continue

        if gate_verdict(workflow_id, dep) == "blocked":
            return dep, gate_cfg

    return None


def handle_fix_loop(workflow_id, gate_node_id, gate_cfg, workflow_cfg):
    """原子作废 + 计数 + 投递 fix_loop 事件;幂等(无作废即不重发)。"""
    retry_node = gate_cfg.get("retry_node", "implementation")

    nodes_by_id = {
        n["id"]: n for n in workflow_cfg.get("nodes", [])
    }
    if nodes_by_id and retry_node not in nodes_by_id:
        gate_deps = nodes_by_id.get(gate_node_id, {}).get("depends_on", [])
        fallback = next(
            (dep for dep in gate_deps if dep in nodes_by_id),
            None,
        ) or next(iter(nodes_by_id))
        print(
            f"[FIX LOOP RETRY NODE FALLBACK] "
            f"{retry_node} not in workflow nodes, using {fallback}"
        )
        retry_node = fallback

    blockers = []

    for task in load_tasks():
        if task.get("workflow_id") != workflow_id:
            continue
        if gate_node_id not in (task.get("node"), task.get("stage")):
            continue
        if task.get("status") == "superseded":
            continue
        if task.get("stage_verdict") == "blocked":
            blockers.append(
                {
                    "task_id": task.get("task_id"),
                    "note": task.get("stage_verdict_note", ""),
                }
            )

    from herdr import fix_loop as fix_loop_core

    try:
        max_loops = int((gate_cfg or {}).get("max_loops", FIX_LOOP_MAX))
    except (TypeError, ValueError):
        max_loops = FIX_LOOP_MAX
    suggested_branch = latest_branch_for_node(workflow_id, retry_node)
    fingerprint = fix_loop_core.verdict_fingerprint(
        suggested_branch, blockers
    )

    with lock:
        _state = load_stage_state()
        prior_count = _state.get(f"{workflow_id}|fixloop|{retry_node}", 0)
        try:
            prior_count = int(prior_count)
        except (TypeError, ValueError):
            prior_count = 0
        stored_fp = _state.get(f"{workflow_id}|fixloop|{retry_node}|fp")

    if _exhaustion_fingerprint(
        _fix_loop_exhaustion_episode(workflow_id, gate_node_id)
    ) == fingerprint:
        return

    if fix_loop_core.fix_loop_exhausted(
        prior_count, max_loops
    ) or fix_loop_core.is_repeat_verdict(fingerprint, stored_fp):
        reason = (
            "max_loops_exhausted"
            if fix_loop_core.fix_loop_exhausted(prior_count, max_loops)
            else "repeat_verdict"
        )
        summary = fix_loop_core.summarize_fix_loop_item(
            {
                "workflow_id": workflow_id,
                "gate_stage": gate_node_id,
                "retry_node": retry_node,
                "loop_count": prior_count,
                "max_loops": max_loops,
                "suggested_branch": suggested_branch,
                "blockers": blockers,
                "exhausted": True,
            }
        )
        _record_fix_loop_exhaustion(
            workflow_id, gate_node_id, retry_node, reason, summary,
            fingerprint,
        )
        with lock:
            _state = load_stage_state()
            _state[f"{workflow_id}|fixloop|{retry_node}|pending_redo"] = {
                "ts": time.time(),
                "gate": gate_node_id,
            }
            save_stage_state(_state)
        coordinator_queue.put(
            {
                "kind": "fix_loop",
                "workflow_id": workflow_id,
                "gate_stage": gate_node_id,
                "retry_node": retry_node,
                "blockers": blockers,
                "invalidated": [],
                "loop_count": prior_count,
                "max_loops": max_loops,
                "suggested_branch": suggested_branch,
                "exhausted": True,
                "escalation_reason": reason,
            }
        )
        print(
            f"[FIX LOOP EXHAUSTED] "
            f"workflow={workflow_id} "
            f"gate={gate_node_id} "
            f"retry={retry_node} "
            f"reason={reason} "
            f"loops={prior_count}/{max_loops}"
        )
        return

    invalidated = invalidate_for_fix_loop(
        workflow_id, gate_node_id, workflow_cfg, retry_node=retry_node
    )

    if not invalidated:
        return

    loop_count = _bump_fix_loop_count(workflow_id, retry_node)

    with lock:
        _state = load_stage_state()
        _state[f"{workflow_id}|fixloop|{retry_node}|pending_redo"] = {
            "ts": time.time(),
            "gate": gate_node_id,
        }
        _state[f"{workflow_id}|fixloop|{retry_node}|fp"] = fingerprint
        save_stage_state(_state)

    coordinator_queue.put(
        {
            "kind": "fix_loop",
            "workflow_id": workflow_id,
            "gate_stage": gate_node_id,
            "retry_node": retry_node,
            "blockers": blockers,
            "invalidated": invalidated,
            "loop_count": loop_count,
            "max_loops": gate_cfg.get("max_loops", FIX_LOOP_MAX),
            "suggested_branch": suggested_branch,
        }
    )

    print(
        f"[FIX LOOP QUEUED] "
        f"workflow={workflow_id} "
        f"gate={gate_node_id} "
        f"retry={retry_node} "
        f"loop={loop_count} "
        f"invalidated={len(invalidated)}"
    )


def coordinator_pane_for_workflow(workflow_id=None):
    if workflow_id:
        project = project_for_workflow(workflow_id) or {}
        pane = project.get("coordinator_pane_id")
        if pane:
            return pane

    return None

listeners = {}
task_sockets = {}

lock = threading.Lock()

coordinator_queue = queue.Queue()
queued_events = set()

# Per-workflow concurrency: each workflow gets its own execution slot so that
# a blocked coordinator for workflow A cannot stall dispatch for workflow B.
_wf_dispatch_locks: dict = {}
_wf_dispatch_locks_meta = threading.Lock()
_coordinator_executor = ThreadPoolExecutor(
    max_workers=16,
    thread_name_prefix="coord-worker"
)


def _workflow_dispatch_lock(workflow_id: str) -> threading.Lock:
    """Return the per-workflow serialization lock (created on first use)."""
    with _wf_dispatch_locks_meta:
        if workflow_id not in _wf_dispatch_locks:
            _wf_dispatch_locks[workflow_id] = threading.Lock()
        return _wf_dispatch_locks[workflow_id]


# ============================================================
# Registry
# ============================================================

def load_tasks():
    store = _get_store()
    return store.list_tasks()


def get_task(task_id):
    store = _get_store()
    return store.get_task(task_id)


def set_task_status(task_id, status):
    result = subprocess.run(
        [
            TASK_MANAGER,
            "set",
            task_id,
            status
        ],
        text=True,
        capture_output=True
    )

    if result.returncode != 0:
        print(
            f"[STATE ERROR] {task_id}: "
            f"{result.stdout.strip() or result.stderr.strip()}"
        )
        return False

    print(
        f"[STATE] "
        f"{result.stdout.strip()}"
    )

    return True


# ============================================================
# Stage policies
# ============================================================

def load_stage_policies():
    try:
        with open(
            STAGE_POLICIES_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f)

    except Exception as e:
        print(
            f"[STAGE POLICY ERROR] {e}"
        )
        return {}


def get_stage_policy(stage_key):
    policies = load_stage_policies()

    return policies.get(
        stage_key,
        {}
    )



# ============================================================
# Workflow stage state
# ============================================================

def load_stage_state():
    if not os.path.exists(STAGE_STATE_FILE):
        return {}

    try:
        with open(
            STAGE_STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f)
    except Exception:
        return {}


def save_stage_state(data):
    tmp = STAGE_STATE_FILE + ".tmp"

    with open(
        tmp,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )

    os.replace(
        tmp,
        STAGE_STATE_FILE
    )


def stage_advance_key(
    workflow_id,
    stage
):
    return f"{workflow_id}:{stage}"


def mark_stage_advance_queued(
    workflow_id,
    stage
):
    key = stage_advance_key(
        workflow_id,
        stage
    )

    with lock:
        state = load_stage_state()

        if state.get(key) in (
            "queued",
            "notified"
        ):
            return False

        state[key] = "queued"
        save_stage_state(state)

    return True


def mark_stage_advance_notified(
    workflow_id,
    stage
):
    key = stage_advance_key(
        workflow_id,
        stage
    )

    with lock:
        state = load_stage_state()
        state[key] = "notified"
        save_stage_state(state)


def clear_stage_advance(
    workflow_id,
    stage
):
    key = stage_advance_key(
        workflow_id,
        stage
    )

    with lock:
        state = load_stage_state()
        state.pop(key, None)
        save_stage_state(state)
def reset_queued_stage_states():
    with lock:
        state = load_stage_state()
        changed = False
        for k in list(state.keys()):
            if state[k] == "queued":
                del state[k]
                changed = True
        if changed:
            save_stage_state(state)



# ============================================================
# Coordinator queue
# ============================================================

def get_stage_status(
    workflow_id,
    stage
):
    try:
        output = subprocess.check_output(
            [
                TASK_MANAGER,
                "stage-status",
                workflow_id,
                stage
            ],
            text=True
        )

        return json.loads(output)

    except Exception as e:
        print(
            f"[STAGE STATUS ERROR] "
            f"workflow={workflow_id} "
            f"stage={stage}: {e}"
        )
        return None


def is_node_complete(workflow_id, node_id):
    tasks = [
        t for t in load_tasks()
        if t.get("workflow_id") == workflow_id
        and (t.get("node") == node_id or t.get("stage") == node_id)
    ]
    if not tasks:
        return False

    # Superseded tasks are excluded from completion calculation — they were
    # replaced by another task whose outcome is the authoritative result.
    active = [
        t for t in tasks
        if t.get("status") != "superseded" and not t.get("superseded_by")
    ]

    # A node with only superseded tasks and no replacements is incomplete.
    if not active:
        return False

    return all(
        t.get("status") in (
            "completed", "committed", "integrated", "cleanup_ready", "cleaned"
        )
        for t in active
    )


def reconcile_stage_advance_states(workflow_id, workflow_cfg):
    """Revoke 'notified' stage-state entries when their predecessor nodes
    have regressed (e.g., a task failed or was superseded with no replacement).

    Without this, a stage whose predecessor regresses after the coordinator
    was already notified would never be re-triggered — because
    mark_stage_advance_queued returns False for 'notified' entries and the
    node never appears in get_ready_nodes again.
    """
    with lock:
        state = load_stage_state()
        changed = False

        nodes_by_id = {
            n["id"]: n
            for n in workflow_cfg.get("nodes", [])
        }

        for key in list(state.keys()):
            if not key.startswith(f"{workflow_id}:"):
                continue
            if state[key] != "notified":
                continue

            node_id = key.split(":", 1)[1]
            node = nodes_by_id.get(node_id)
            if not node:
                continue

            # Check whether all predecessors are still complete.
            deps = node.get("depends_on", [])
            predecessors_complete = all(
                is_node_complete(workflow_id, dep)
                for dep in deps
            )
            if not predecessors_complete:
                del state[key]
                changed = True
                print(
                    f"[STAGE REVOKE] "
                    f"workflow={workflow_id} node={node_id}: "
                    "predecessors no longer complete, revoking 'notified' lock"
                )

        if changed:
            save_stage_state(state)


def enqueue_stage_advance(task):
    workflow_id = task.get("workflow_id")
    if workflow_id:
        check_workflow_stage_advance(workflow_id)


def direct_stage_dispatch_enabled():
    value = os.environ.get("HERDR_DIRECT_STAGE_DISPATCH", "1")
    return value.strip().lower() not in ("0", "false", "off", "no")


def coordinator_intake_enabled():
    """新工作流首个节点是否交由总指挥接单(默认开启)。

    产品期望:启动时先由总指挥接收需求、理解目标与约束,再派发第一个
    节点的 Task,而不是系统直接开始(旧直派路径跳过总指挥)。
    设 HERDR_COORDINATOR_INTAKE=0 退回直派快路径。
    """
    value = os.environ.get("HERDR_COORDINATOR_INTAKE", "1")
    return value.strip().lower() not in ("0", "false", "off", "no")


# 单个 launch 必须有界:子进程僵死时不得永久占用调度线程
# 与 per-workflow 锁,超时后走既有 PARTIAL→总指挥回退路径。
DIRECT_DISPATCH_LAUNCH_TIMEOUT = 300
SUPERVISOR_VERIFY_DISPATCH_TIMEOUT = 120


def _dispatch_candidate_ready(project_root, base_branch, specs):
    """候选非空预检:onto 分支相对基线无提交时拒绝派发(转总指挥)。

    r6 曾直派测试 main 空候选并恒 blocked，白烧内环。未知情况
    （缺 refs、git 失败）一律 fail-open 照常派发。
    """
    ontos = sorted(
        {s.get("onto_branch") for s in (specs or []) if s.get("onto_branch")}
    )
    if not ontos:
        return True
    base = (base_branch or "dev").strip() or "dev"
    checked = 0
    empty = 0
    for onto in ontos:
        try:
            result = subprocess.run(
                ["git", "-C", project_root, "rev-list", "--count",
                 f"{base}..{onto}", "--"],
                text=True,
                capture_output=True,
                timeout=10,
            )
        except Exception:
            return True
        if result.returncode != 0:
            return True
        try:
            is_empty = int(result.stdout.strip()) == 0
        except (TypeError, ValueError):
            return True
        checked += 1
        if is_empty:
            empty += 1
    if checked and empty == checked:
        print(
            f"[DIRECT DISPATCH CANDIDATE EMPTY] "
            f"onto={','.join(ontos)} has no commits beyond {base} "
            "-> fallback to coordinator for adjudication"
        )
        return False
    return True


def try_direct_stage_advance(item):
    """常规推进会:按节点模板规则化直接派发,失败回落总指挥。

    返回 True 表示事件已被处理(含 wait),False 表示必须走总指挥原路径。
    """
    if direct_dispatch_planner is None or not direct_stage_dispatch_enabled():
        return False

    node = item.get("node")
    workflow_id = item.get("workflow_id")
    ready_id = item.get("node_id") or item.get("next_stage")

    if not workflow_id or not ready_id:
        return False

    project_ctx = project_for_workflow(workflow_id) or {}
    if project_ctx.get("startup_ready") is False:
        return False

    project_root = project_ctx.get("project_root") or ""
    if not project_root or not project_ctx.get("coordinator_pane_id"):
        return False

    requirement = (project_ctx.get("requirement") or "").strip()

    # 历史 workflow.json 的节点字段可能为空(旧模板快照),
    # 与总指挥路径一致回退 stage-policies 后再做规则化决策。
    node = direct_dispatch_planner.merge_node_policy(
        node if node is not None else {"id": ready_id},
        get_stage_policy(ready_id),
    )
    if not node.get("id"):
        node["id"] = ready_id

    # 上游存在 blocked 验收结论时不得自动派发(与 sweep 对称),
    # 回退总指挥裁决。依赖未知时保持原行为(fail-open)。
    dep_ids = (item.get("node") or {}).get("depends_on")
    if dep_ids is None:
        try:
            wf_cfg = workflow_config_for(workflow_id) or {}
            found = find_node(wf_cfg, ready_id) if wf_cfg.get("nodes") else None
            dep_ids = (found or {}).get("depends_on") or []
        except ValueError:
            dep_ids = []
    verdict_dep = blocked_verdict_dep(workflow_id, {"depends_on": dep_ids})
    if verdict_dep:
        print(
            f"[DIRECT DISPATCH BLOCKED] "
            f"workflow={workflow_id} "
            f"node={ready_id} "
            f"blocked_dep={verdict_dep} "
            "-> fallback to coordinator for adjudication"
        )
        return False

    gate_task = node_is_gate(workflow_id, ready_id)

    docs_block = shared_docs_block(
        workflow_id,
        ready_id,
        related_nodes=dep_ids,
        project_ctx=project_ctx,
    )

    # 门禁节点在派发时注入结论契约(状态目录 gate-verdicts/<task_id>.json +
    # 终端标记),由 try_auto_verdict 直接采纳,免除总指挥裁决回合。
    candidate_branch = direct_dispatch_planner.candidate_branch_for_node(
        load_tasks(), workflow_id, ready_id, dep_ids
    )
    plan = direct_dispatch_planner.plan_stage_dispatch(
        workflow_id,
        node,
        load_tasks(),
        requirement,
        context_branch=candidate_branch,
        gate_contract=gate_task,
        docs_block=docs_block,
    )

    if gate_task and plan.get("mode") == "dispatch":
        # 状态目录必须先于 Agent 写入存在(结论文件落 clone 外,交付零污染)。
        try:
            direct_dispatch_planner.gate_verdict_dir().mkdir(
                parents=True, exist_ok=True
            )
        except Exception as exc:
            print(f"[GATE VERDICT DIR WARN] {exc}")

    mode = plan.get("mode")

    if mode == "fallback":
        print(
            f"[DIRECT DISPATCH FALLBACK] "
            f"workflow={workflow_id} "
            f"node={ready_id} "
            f"reason={plan.get('reason')}"
        )
        return False

    if mode == "wait":
        # 节点已有活跃任务:推进会自然由状态机完成,不再唤醒总指挥。
        mark_stage_advance_notified(workflow_id, ready_id)
        print(
            f"[DIRECT DISPATCH WAIT] "
            f"workflow={workflow_id} "
            f"node={ready_id} "
            f"reason={plan.get('reason')}"
        )
        return True

    specs = plan.get("specs") or []
    if not specs:
        return False

    if not _dispatch_candidate_ready(
        project_root, project_ctx.get("base_branch"), specs
    ):
        return False

    launched = []

    for spec in specs:
        cmd = [
            TASK_MANAGER,
            "launch",
            "--task-id", spec["task_id"],
            "--workflow-id", workflow_id,
            "--node", ready_id,
            "--source", project_root,
            "--agent", "auto",
            "--task-type", spec["task_type"],
            "--integration-mode", spec["integration_mode"],
            "--goal", spec["goal"],
            "--prompt", spec["prompt"],
        ]

        if spec.get("onto_branch"):
            cmd += ["--onto", spec["onto_branch"]]

        for line in spec["acceptance"]:
            cmd += ["--acceptance", line]

        try:
            result = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=DIRECT_DISPATCH_LAUNCH_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            print(
                f"[DIRECT DISPATCH TIMEOUT] "
                f"task={spec['task_id']} "
                f"after={DIRECT_DISPATCH_LAUNCH_TIMEOUT}s"
            )
            result = None

        if result is None or result.returncode != 0:
            if result is not None:
                print(
                    f"[DIRECT DISPATCH ERROR] "
                    f"task={spec['task_id']}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
            if launched:
                print(
                    f"[DIRECT DISPATCH PARTIAL] "
                    f"launched={','.join(launched)} "
                    "-> fallback to coordinator for reconciliation"
                )
            return False

        launched.append(spec["task_id"])

    mark_stage_advance_notified(workflow_id, ready_id)

    print(
        f"[STAGE ADVANCED DIRECT] "
        f"workflow={workflow_id} "
        f"node={ready_id} "
        f"tasks={','.join(launched)}"
    )

    # Collaboration accelerator: deterministic edges emit one HANDOFF each.
    # Best-effort only: the launched tasks already carry their own prompts,
    # so a handoff failure must never roll back the stage advance.
    try:
        for handoff in maybe_dispatch_node_handoffs(
            workflow_id=workflow_id, ready_id=ready_id,
            dep_ids=dep_ids, launched=launched,
        ):
            print(f"[COLLABORATION HANDOFF] {handoff}")
    except Exception as exc:
        print(
            f"[COLLABORATION HANDOFF SKIPPED] "
            f"workflow={workflow_id} node={ready_id}: {type(exc).__name__}"
        )

    maybe_compact_coordinator(workflow_id, reason=f"stage_advance:{ready_id}")

    return True


def check_workflow_stage_advance(workflow_id):
    if not workflow_id:
        return

    # Closed workflows must never advance: a zero-task closed workflow is
    # vacuously "complete" at every stage and would ghost-advance forever.
    if workflow_closed(workflow_id):
        return

    pane = coordinator_pane_for_workflow(workflow_id)
    if not pane:
        return

    workflow_cfg = workflow_config_for(workflow_id)
    if not workflow_cfg:
        return

    workflow_record = project_for_workflow(workflow_id) or {}
    # New factory starts persist the requirement and keep this gate closed
    # until Deep Preflight has completed. This prevents the controller from
    # delivering a stage event before the startup request is ready.
    if workflow_record.get("startup_ready") is False:
        print(f"[STARTUP WAIT] workflow={workflow_id} preflight/request not ready")
        return

    # 已关闭(含 abandoned)的 workflow 不再参与任何推进/回流判定;
    # 否则 abandoned 残留的 blocked verdict 会被 sweep 反复回流。
    wf_st = _workflow_entry(workflow_id).get("status")
    if wf_st in ("completed", "paused"):
        return

    if workflow_cfg.get("nodes"):
        # Revoke stale 'notified' locks before computing ready nodes,
        # so that regressed stages can be re-triggered.
        reconcile_stage_advance_states(workflow_id, workflow_cfg)

        completed_nodes = {
            n["id"]
            for n in workflow_cfg.get("nodes", [])
            if is_node_complete(workflow_id, n["id"])
        }

        if is_workflow_completed(workflow_cfg, completed_nodes):
            # reopen 闩在先:重开现场(sweep 每 2s 一次)不得刷
            # [WORKFLOW COMPLETE] 噪音,更不得触发 close。
            if _workflow_entry(workflow_id).get("suppress_auto_close"):
                return

            if _workflow_entry(workflow_id).get("status") == "completed":
                return

            # 交付终态门禁:任一门禁节点 verdict=blocked 时不得关闭,
            # 回流 fix-loop(由 handle_fix_loop 原子作废并派发事件)。
            nodes_by_id = {
                n["id"]: n for n in workflow_cfg.get("nodes", [])
            }
            blocked_gates = []

            for node_id in sorted(completed_nodes):
                gate_cfg = resolve_gate_config(
                    nodes_by_id.get(node_id), node_id
                )
                if (
                    gate_cfg
                    and gate_verdict(workflow_id, node_id) == "blocked"
                ):
                    blocked_gates.append((node_id, gate_cfg))

            if blocked_gates:
                for gate_node_id, gate_cfg in blocked_gates:
                    handle_fix_loop(
                        workflow_id, gate_node_id, gate_cfg, workflow_cfg
                    )
                return

            # 完成日志闩:close 可能失败或需要多轮,日志只允许出现一次,
            # 否则 sweep 会把 [WORKFLOW COMPLETE] 刷成日志风暴。
            if workflow_id not in _workflow_complete_logged:
                _workflow_complete_logged.add(workflow_id)
                print(
                    f"[WORKFLOW COMPLETE] "
                    f"workflow={workflow_id}"
                )

            maybe_close_completed_workflow(workflow_id)
            return

        ready_nodes = get_ready_nodes(workflow_cfg, completed_nodes)
        for ready_node in ready_nodes:
            latched_dep = next(
                (
                    dep
                    for dep in (ready_node.get("depends_on") or [])
                    if _fix_loop_latch_blocks(workflow_id, dep)
                ),
                None,
            )
            if latched_dep:
                latch_key = f"{workflow_id}:{latched_dep}"
                if latch_key not in _fix_latch_logged:
                    _fix_latch_logged.add(latch_key)
                    print(
                        f"[FIX LOOP LATCH] "
                        f"workflow={workflow_id} "
                        f"{ready_node['id']} waits for redo of {latched_dep}"
                    )
                continue
            blocked_dep = blocked_gate_dependency(
                workflow_id, ready_node, workflow_cfg
            )
            if blocked_dep:
                gate_node_id, gate_cfg = blocked_dep
                handle_fix_loop(
                    workflow_id, gate_node_id, gate_cfg, workflow_cfg
                )
                continue

            ready_id = ready_node["id"]
            verdict_dep = blocked_verdict_dep(workflow_id, ready_node)
            if verdict_dep:
                # 有门禁结论无门禁配置的依赖:不销毁下游,只暂停自动推进
                # 并 funnel 给总指挥裁决;作废过期结论后自动恢复。
                verdict_key = f"{workflow_id}:upstream_blocked:{ready_id}"
                if not attention_blocks_retry(verdict_key):
                    episode = attention_get(verdict_key) or {}
                    attempts = int(episode.get("attempts") or 0) + 1
                    attention_note(
                        verdict_key,
                        {"task_id": f"stage_advance:{ready_id}",
                         "workflow_id": workflow_id},
                        "stage_advance",
                        reason="upstream_blocked",
                        attempts=attempts,
                        next_retry_at=time.time() + liveness.attention_retry_interval(),
                        detail=f"blocked_dep={verdict_dep}",
                    )
                    notify_attention(
                        "Herdr Factory · 上游门禁阻断",
                        {"task_id": f"stage_advance:{ready_id}",
                         "workflow_id": workflow_id},
                        f"节点 {ready_id} 的上游 {verdict_dep} 存在 blocked 验收结论，"
                        "已暂停自动推进等待裁决（作废过期结论或返工后自动恢复）。",
                        "upstream_blocked",
                    )
                    print(
                        f"[STAGE ADVANCE BLOCKED] "
                        f"workflow={workflow_id} "
                        f"{ready_id} blocked_dep={verdict_dep} "
                        "-> awaiting adjudication"
                    )
                continue
            if attention_blocks_retry(f"{workflow_id}:stage_advance:{ready_id}"):
                continue

            if not mark_stage_advance_queued(workflow_id, ready_id):
                continue

            deps = ready_node.get("depends_on", [])
            source_stage = deps[-1] if deps else "start"

            coordinator_queue.put(
                {
                    "kind": "stage_advance",
                    "workflow_id": workflow_id,
                    "stage": source_stage,
                    "node_id": ready_id,
                    "next_stage": ready_id,
                    "stage_label": ready_node.get("label", ready_id),
                    "node": ready_node,
                }
            )

            print(
                f"[STAGE ADVANCE QUEUED] "
                f"workflow={workflow_id} "
                f"{source_stage} -> {ready_id}"
            )
        return

    # Fallback to legacy single-step stage advance
    for stage in workflow_cfg.get("stages", []):
        stage_key = stage.get("key") or stage.get("id")
        if not is_node_complete(workflow_id, stage_key):
            continue

        next_stage = stage.get("next")
        if not next_stage:
            continue

        gate_cfg = resolve_gate_config(None, stage_key)
        if gate_cfg and gate_verdict(workflow_id, stage_key) == "blocked":
            handle_fix_loop(workflow_id, stage_key, gate_cfg, workflow_cfg)
            continue

        if attention_blocks_retry(f"{workflow_id}:stage_advance:{next_stage}"):
            continue

        if _fix_loop_latch_blocks(workflow_id, stage_key):
            latch_key = f"{workflow_id}:{stage_key}"
            if latch_key not in _fix_latch_logged:
                _fix_latch_logged.add(latch_key)
                print(
                    f"[FIX LOOP LATCH] "
                    f"workflow={workflow_id} "
                    f"{next_stage} waits for redo of {stage_key}"
                )
            continue

        if not mark_stage_advance_queued(workflow_id, next_stage):
            continue

        coordinator_queue.put(
            {
                "kind": "stage_advance",
                "workflow_id": workflow_id,
                "stage": stage_key,
                "next_stage": next_stage,
                "stage_label": stage.get("label", stage_key),
            }
        )

        print(
            f"[STAGE ADVANCE QUEUED] "
            f"workflow={workflow_id} "
            f"{stage_key} -> {next_stage}"
        )


def active_registered_workflows():
    workflows = set()
    store = _get_store()
    for wf in store.list_workflows():
        wid = wf.get("workflow_id")
        if not wid or wf.get("status") == "completed":
            continue

        # 夹具/临时残留(pytest tmp、已删除的 workflow_file)绝不允许进入调度 sweep,
        # 否则会对不存在的 pane 无限重试并淹没日志。
        if liveness.workflow_is_foreign(wf):
            if wid not in _foreign_workflows_logged:
                _foreign_workflows_logged.add(wid)
                print(
                    f"[WORKFLOW FOREIGN SKIPPED] "
                    f"workflow={wid} "
                    f"file={wf.get('workflow_file')} "
                    f"(fixture/temp residue; excluded from scheduling)"
                )
            continue

        workflows.add(wid)
    return workflows


# fix-loop 作废闩去重日志:闩存续期间只提示一次,清除后下轮可再提示。
_fix_latch_logged = set()


def _fix_loop_latch_info(workflow_id, node_id):
    """Return (latch_ts, gate_node_id); accepts legacy float values."""
    with lock:
        state = load_stage_state()
        raw = state.get(f"{workflow_id}|fixloop|{node_id}|pending_redo")
    gate = None
    ts = 0.0
    if isinstance(raw, dict):
        gate = raw.get("gate")
        try:
            ts = float(raw.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
    else:
        try:
            ts = float(raw or 0)
        except (TypeError, ValueError):
            ts = 0.0
    return ts, gate


def _fix_loop_latch_blocks(workflow_id, node_id, tasks=None):
    """作废闩:被回流作废的节点在出现真正重做完成前挡住自动推进。

    有重做完成时顺带清除闩、指纹、计数与升级记录,下一轮阻断重新计数。
    """
    latch_ts, latch_gate = _fix_loop_latch_info(workflow_id, node_id)
    if not latch_ts:
        return False
    if tasks is None:
        tasks = load_tasks()
    from herdr import fix_loop as fix_loop_core

    if not fix_loop_core.latch_blocks_advance(tasks, node_id, latch_ts):
        with lock:
            state = load_stage_state()
            state.pop(
                f"{workflow_id}|fixloop|{node_id}|pending_redo", None
            )
            state.pop(f"{workflow_id}|fixloop|{node_id}|fp", None)
            state.pop(f"{workflow_id}|fixloop|{node_id}", None)
            save_stage_state(state)
        if latch_gate:
            attention_clear(
                f"{workflow_id}:fix_loop_exhausted:{latch_gate}"
            )
        _fix_latch_logged.discard(f"{workflow_id}:{node_id}")
        return False
    return True


def redeliver_pending_fix_loop(workflow_id):
    """补投因总指挥忙而持久化的 fix-loop 通知(sweep 每轮调用,有界)。"""
    if workflow_closed(workflow_id):
        return False
    from herdr import fix_loop as fix_loop_core

    try:
        episodes = _attention_store.all()
    except Exception:
        return False
    now = time.time()
    redelivered = False
    for key, episode in episodes.items():
        if not key.startswith(f"{workflow_id}:fix_loop:"):
            continue
        if not isinstance(episode, dict):
            continue
        if (episode.get("event_type") or "") != "fix_loop":
            continue
        if (episode.get("reason") or "") != "coordinator_busy":
            continue
        try:
            summary = json.loads(episode.get("detail") or "{}")
        except (TypeError, ValueError):
            attention_clear(key)
            continue
        if not isinstance(summary, dict) or not summary.get("retry_node"):
            attention_clear(key)
            continue
        tasks = load_tasks()
        if fix_loop_core.redelivery_handled(
            tasks, summary["retry_node"], episode.get("first_seen_at")
        ):
            attention_clear(key)
            print(
                f"[FIX LOOP REDELIVERY SKIP] "
                f"workflow={workflow_id} "
                f"retry={summary['retry_node']} "
                "coordinator already dispatched follow-up"
            )
            continue
        if not fix_loop_core.redelivery_due(episode, now):
            continue
        if coordinator_status(workflow_id) not in ("idle", "done"):
            continue
        coordinator_queue.put(
            {
                "kind": "fix_loop",
                "workflow_id": workflow_id,
                "gate_stage": summary.get("gate_stage", ""),
                "retry_node": summary["retry_node"],
                "blockers": summary.get("blockers") or [],
                "invalidated": [],
                "loop_count": summary.get("loop_count", 0),
                "max_loops": summary.get("max_loops", FIX_LOOP_MAX),
                "suggested_branch": summary.get("suggested_branch"),
                "exhausted": bool(summary.get("exhausted")),
                "redelivered": True,
            }
        )
        attention_note(
            key,
            {"task_id": f"fix_loop:{summary.get('gate_stage')}",
             "workflow_id": workflow_id},
            "fix_loop",
            reason="coordinator_busy",
            attempts=int(episode.get("attempts") or 0),
            next_retry_at=now + liveness.attention_retry_interval(),
            detail=episode.get("detail"),
        )
        print(
            f"[FIX LOOP REDELIVERED] "
            f"workflow={workflow_id} "
            f"gate={summary.get('gate_stage')} "
            f"retry={summary['retry_node']}"
        )
        redelivered = True
    return redelivered


def check_all_workflows_stage_advance():
    for wf in active_registered_workflows():
        try:
            redeliver_pending_fix_loop(wf)
        except Exception as e:
            print(f"[REDELIVER ERROR] workflow={wf}: {e}")
        try:
            check_workflow_stage_advance(wf)
        except Exception as e:
            print(f"[ADVANCE CHECK ERROR] workflow={wf}: {e}")



def coordinator_status(workflow_id=None):
    pane_id = coordinator_pane_for_workflow(
        workflow_id
    )

    if not pane_id:
        return "unknown"

    try:
        output = subprocess.check_output(
            [
                "herdr",
                "agent",
                "get",
                pane_id
            ],
            text=True
        )

        data = json.loads(output)

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
        return "unknown"


# ============================================================
# 总指挥上下文卫生 & 效率纪律
# ============================================================
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
4. 若你判断当前上下文已明显过大,先执行 /compact 再继续本事件。
""".strip()


def coordinator_compact_enabled():
    value = os.environ.get("HERDR_COORDINATOR_COMPACT", "1")
    return value.strip().lower() not in ("0", "false", "off", "no")


def coordinator_agent_kind(pane_id):
    """pane 内 agent 种类(herdr agent get -> result.agent.agent)。"""
    try:
        output = subprocess.check_output(
            ["herdr", "agent", "get", pane_id],
            text=True
        )
        agent = (json.loads(output).get("result") or {}).get("agent") or {}
        return agent.get("agent") or ""
    except Exception:
        return ""


def maybe_compact_coordinator(workflow_id, reason="stage_advance"):
    """阶段/fix-loop 边界给总指挥下发 /compact(有界等待,失败不阻断)。

    仅对支持 /compact 的 agent kind 生效;总指挥忙时跳过(下个边界再补)。
    这一调用把长工作流的上下文峰值压回可控区间,直接缩短后续回合耗时。
    """
    if not coordinator_compact_enabled():
        return False

    pane_id = coordinator_pane_for_workflow(workflow_id)
    if not pane_id:
        return False

    kind = coordinator_agent_kind(pane_id)
    if kind not in COORDINATOR_COMPACT_AGENTS:
        print(
            f"[COORDINATOR COMPACT SKIP] "
            f"workflow={workflow_id} pane={pane_id} "
            f"kind={kind or 'unknown'} reason={reason}"
        )
        return False

    if coordinator_status(workflow_id) not in ("idle", "done"):
        print(
            f"[COORDINATOR COMPACT SKIP] "
            f"workflow={workflow_id} pane={pane_id} "
            f"busy reason={reason}"
        )
        return False

    try:
        result = subprocess.run(
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


def build_coordinator_message(task, event_type):
    workflow_id = task.get("workflow_id", "unknown")
    task_id = task["task_id"]

    goal = task.get("goal", "未定义")

    criteria = "\n".join(
        f"- {item}"
        for item in task.get(
            "acceptance_criteria",
            []
        )
    )

    if not criteria:
        criteria = "- 未定义"

    if event_type == "inner_loop_exhausted":
        blocker_content = (
            _read_loop_doc(task, "BLOCKER.md")
            or "（BLOCKER.md 未找到）"
        )

        return f"""
HERDR_CONTROLLER_BLOCKER_EVENT

workflow_id: {workflow_id}
task_id: {task_id}
stage: {task['stage']}
pane_id: {task['pane_id']}
agent: {task['agent']}
agent_status: blocked (inner_loop_exhausted)

任务目标：
{goal}

⚠️ 工位内循环已耗尽全部重试次数，无法自愈，主动请求总指挥仲裁。

━━━━━━━━━━━━━━━━━━━━━
工位求助单 (BLOCKER.md)
━━━━━━━━━━━━━━━━━━━━━
{blocker_content}

━━━━━━━━━━━━━━━━━━━━━
总指挥仲裁三选一(决策必须落盘)：
━━━━━━━━━━━━━━━━━━━━━

A. 问题可解决(提供具体指导后让工位继续)：
   ~/HAFlow/bin/herdr-task set {task_id} rework
   然后用 herdr agent prompt 向工位下达具体修复指令。

B. 需要换策略(放弃本轮，换 Agent 或调整范围)：
   ~/HAFlow/bin/herdr-task set {task_id} failed
   再按需重新规划/重派。

C. 目标或验收标准有歧义(调整后重新派发)：
   ~/HAFlow/bin/herdr-task set {task_id} failed
   修正任务目标/验收标准后重新下发。

仲裁前不得把 Task 置为 completed。

{COORDINATOR_DISCIPLINE}
""".strip()

    if event_type == "blocked":
        return f"""
HERDR_CONTROLLER_BLOCKED_EVENT

workflow_id: {workflow_id}
task_id: {task_id}
stage: {task['stage']}
pane_id: {task['pane_id']}
agent: {task['agent']}
agent_status: blocked

任务目标：
{goal}

当前 Task 被 Agent 阻塞。

你现在只负责解除阻塞，不允许验收任务。

必须执行：

1. 使用 Herdr 读取 {task['pane_id']} 当前界面和最新输出。
2. 判断 blocked 的真实原因。
3. 如果属于低风险、当前任务范围内的正常操作，可以处理审批并让 Agent 继续。
4. 如果属于高风险操作，不得自动批准，向用户报告风险并保持 blocked。
5. blocked 阶段不得把 Task 设置为 completed。
6. blocked 阶段不得进行正式任务验收。
7. Agent 恢复执行后，Controller 会自动处理 working 状态。
8. 不要推进阶段。

blocked 只表示等待处理，不代表任务结束。

{COORDINATOR_DISCIPLINE}
""".strip()

    if event_type == "attention":
        return f"""
HERDR_CONTROLLER_ATTENTION_EVENT

workflow_id: {workflow_id}
task_id: {task_id}
stage: {task['stage']}
pane_id: {task['pane_id']}
agent: {task['agent']}
task_status: {task.get('status')}

任务目标：
{goal}

该 Task 长时间停留在需要人工/总指挥裁决的中间态（interrupted / paused），
既未完成也未失败，阻塞了当前节点的推进。

现在必须立即裁决，只处理这一个 Task：

1. 使用 Herdr 读取 {task['pane_id']} 当前界面与最新输出。
2. 判断现场是否仍有未完成的有效产出：
   - 产出已就绪 → 执行验收（verify-baseline），通过则 set completed（门禁阶段带 --verdict）。
   - 实现未完成 → set rework 并用 herdr agent prompt 继续下发指令。
   - 无法恢复 → set failed。
3. 严禁让任务继续停留在 interrupted / paused。
4. 不要创建新 Task，不要推进阶段。

{COORDINATOR_DISCIPLINE}
""".strip()

    if event_type == "done":
        return f"""
HERDR_CONTROLLER_DONE_EVENT

workflow_id: {workflow_id}
task_id: {task_id}
stage: {task['stage']}
pane_id: {task['pane_id']}
agent: {task['agent']}
agent_status: done

任务目标：
{goal}

验收标准：
{criteria}

Agent 本轮执行已经结束。

现在执行正式验收：

1. 使用 Herdr 读取 {task['pane_id']} 的最终输出。

2. 必须执行基线验证与量化指标核验：
   ~/HAFlow/bin/herdr-task verify-baseline {task_id}
   ~/HAFlow/bin/herdr-task verify-metrics {task_id} --if-present

3. `verify-baseline` 是判断当前 Task 文件变化的唯一事实来源：

   - `BASELINE_MATCH`
     表示 Agent 相对于 Task 创建时没有产生新的文件变化。

   - `TASK_CHANGED`
     后面列出的文件，才是当前 Task 真正产生的变化。

4. 验收决策指引（严禁混淆）：

   A. 验收通过（所有标准满足、测试绿灯）：
      ~/HAFlow/bin/herdr-task set {task_id} completed --verdict pass

   B. 发现代码缺陷需回炉（特别是 test / review 阶段查出问题）：
      **严禁对评审/测试任务执行 set rework！**
      必须以 blocked 结论闭环，Controller 会自动触发跨阶段回流并作废受影响链条：
      ~/HAFlow/bin/herdr-task set {task_id} completed --verdict blocked --note "<blocker 清单与修复指引>"

   C. 仅当当前任务自身未完成（如实现中途卡死、需在同一工位继续补全）：
      ~/HAFlow/bin/herdr-task set {task_id} rework
      然后使用 herdr agent prompt 继续下发指令。

   D. 如果任务发生不可恢复的崩溃：
      ~/HAFlow/bin/herdr-task set {task_id} failed

阶段推进前必须执行：

~/HAFlow/bin/herdr-task list --workflow-id {workflow_id}

只能检查当前 workflow_id 下的任务。
禁止使用其他 Workflow 或历史 Task 判断当前阶段门禁。

只有当前 Workflow 当前阶段所有必要 Task 都 completed，
才能进入下一阶段。

{COORDINATOR_DISCIPLINE}
""".strip()

    return None


def _read_loop_doc(task, filename, max_lines=None):
    """读取工位内环目录(<clone>/.herdr-loop/)下的报告文档,失败返回空串。"""
    clone_path = task.get("clone_path") or ""
    if not clone_path:
        return ""
    path = Path(clone_path) / ".herdr-loop" / filename
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
    except Exception:
        return ""
    if max_lines:
        lines = lines[:max_lines]
    return "\n".join(lines)


def blocked_event_type(task):
    """blocked 事件细分:内环耗尽走专属仲裁卡,其余走通用解除阻塞卡。

    内环协议(herdr-loop/evaluator)在重试耗尽时写 <clone>/.herdr-loop/
    BLOCKER.md 并输出 HERDR_TASK_BLOCKER 标记,Sentinel 置 blocked
    (sentinel_reason=inner_loop_exhausted)。此前 Controller 只发通用
    blocked 卡,工位自述的 BLOCKER.md 被丢弃——这是 Phase 0 设计的
    最后一跳(2026-09-13 设计,09-18 补齐)。
    """
    if task and task.get("sentinel_reason") == "inner_loop_exhausted":
        return "inner_loop_exhausted"
    return "blocked"


def enqueue_coordinator_event(task, event_type):
    key = f"{task['task_id']}:{event_type}"

    with lock:
        if key in queued_events:
            print(
                f"[QUEUE DUPLICATE SKIPPED] {key}"
            )
            return

        queued_events.add(key)

    coordinator_queue.put(
        {
            "task_id": task["task_id"],
            "event_type": event_type,
            "key": key
        }
    )

    print(
        f"[QUEUE] "
        f"task={task['task_id']} "
        f"event={event_type}"
    )


def handle_coordinator_delivery_stall(item, task, elapsed):
    """总指挥投递 SLA 到期:记录 attention、通知、让位(不再无限 BUSY)。

    registry watcher 会依据 episode 的 next_retry_at 做慢速重试;
    总指挥恢复后事件会自然补投,不需要人工重启。
    """
    key = item["key"]
    event_type = item["event_type"]
    workflow_id = task.get("workflow_id")
    pane_id = coordinator_pane_for_workflow(workflow_id) or "unknown"

    episode = attention_get(key) or {}
    attempts = int(episode.get("attempts") or 0) + 1
    retry_at = time.time() + liveness.attention_retry_interval()

    attention_note(
        key,
        task,
        event_type,
        reason="coordinator_stalled",
        attempts=attempts,
        next_retry_at=retry_at,
        detail=f"waited={int(elapsed)}s pane={pane_id}",
    )

    print(
        f"[COORDINATOR STALLED] "
        f"task={item['task_id']} "
        f"event={event_type} "
        f"pane={pane_id} "
        f"waited={int(elapsed)}s "
        f"attempts={attempts} "
        f"-> attention recorded, retry_in={int(liveness.attention_retry_interval())}s"
    )

    notify_attention(
        "Herdr Factory · 总指挥停滞",
        task,
        f"事件 {event_type} 等待总指挥超过 {int(elapsed)}s（pane={pane_id}）。"
        "已记录 attention 并将慢速重试，请检查总指挥状态与集成健康。",
        "coordinator_stalled",
    )


def wait_for_coordinator_decision(task_id, timeout=None):
    """等待总指挥判定落盘(completed/rework/...)。

    默认预算与真实回合尾部耗时对齐(env HERDR_COORDINATOR_DECISION_TIMEOUT);
    超时不立即重试:记录 attention 退避,避免再烧一整轮 10 分钟级回合。
    """
    if timeout is None:
        timeout = liveness.coordinator_decision_timeout()

    deadline = time.time() + timeout

    while time.time() < deadline:
        task = get_task(task_id)

        if not task:
            print(
                f"[DECISION ERROR] "
                f"task={task_id} missing"
            )
            return None

        status = task.get("status")

        if status != "agent_done":
            print(
                f"[DECISION] "
                f"task={task_id} "
                f"status={status}"
            )
            return status

        time.sleep(0.5)

    task = get_task(task_id)
    key = f"{task_id}:done"
    episode = attention_get(key) or {}
    attempts = int(episode.get("attempts") or 0) + 1

    if task:
        attention_note(
            key,
            task,
            "done",
            reason="decision_timeout",
            attempts=attempts,
            next_retry_at=time.time() + liveness.attention_retry_interval(),
            detail=f"waited={int(timeout)}s still=agent_done",
        )

    print(
        f"[DECISION TIMEOUT] "
        f"task={task_id} "
        f"still=agent_done "
        f"attempts={attempts} "
        f"-> attention recorded, "
        f"retry_in={int(liveness.attention_retry_interval())}s"
    )

    return "agent_done"


def retry_coordinator_decision(task_id):
    task = get_task(task_id)

    if not task:
        print(
            f"[RETRY ERROR] "
            f"task={task_id} missing"
        )
        return None

    if task.get("status") != "agent_done":
        return task.get("status")

    message = f"""
HERDR_CONTROLLER_RETRY_EVENT

workflow_id: {task.get('workflow_id', 'unknown')}
task_id: {task_id}
stage: {task.get('stage', 'unknown')}
pane_id: {task.get('pane_id', 'unknown')}
agent: {task.get('agent', 'unknown')}

这是一次自动重试。

该 Task 仍停留在 agent_done，
说明上一次验收通知没有完成状态落盘。

请立即只处理这个已有 Task，不要创建新 Task：

1. 读取 Task Registry。
2. 读取 Agent 最终输出。
3. 执行：
   ~/HAFlow/bin/herdr-task verify-baseline {task_id}
4. 根据任务目标和验收标准完成正式验收。
5. 必须将 Task 状态更新为以下之一：
   - completed（门禁阶段必须带 --verdict pass|blocked，blocked 另附 --note）
   - rework
   - failed
6. 不要只输出文字报告而不更新 Task Registry。

{COORDINATOR_DISCIPLINE}
""".strip()

    print(
        f"[COORDINATOR RETRY] "
        f"task={task_id}"
    )

    result = subprocess.run(
        [
            "herdr",
            "agent",
            "prompt",
            coordinator_pane_for_workflow(task.get("workflow_id")),
            message,
            "--wait",
            "--timeout",
            "120000"
        ],
        text=True,
        capture_output=True
    )

    if result.returncode != 0:
        print(
            f"[COORDINATOR RETRY ERROR] "
            f"task={task_id}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    return wait_for_coordinator_decision(
        task_id,
        timeout=30
    )

def maybe_complete_on_task_done(task_id, db_path=None):
    """Complete acknowledged handoffs whose target reached terminal done.

    Production hook for the authoritative task completion pipeline. Only
    ``acknowledged`` rows transition: a handoff never ACKed stays visible as
    stuck instead of being silently closed. Never raises.
    """
    if not collaboration_enabled():
        return []
    try:
        from herdr import state_db as _sdb
        done = []
        for row in _sdb.list_collaboration_events(
            task_id=task_id, status="acknowledged", db_path=db_path,
        ):
            if row["to_task_id"] != task_id:
                continue
            done.append(_sdb.mark_collaboration_completed(row["event_id"], db_path=db_path))
        return done
    except Exception as exc:
        print(f"[COLLABORATION COMPLETE SKIPPED] task={task_id}: {type(exc).__name__}")
        return []


def _parse_commit_result(output):
    """Extract HERDR_COMMIT_RESULT JSON from `herdr-task commit` output."""
    for line in (output or "").splitlines():
        if line.startswith("HERDR_COMMIT_RESULT="):
            try:
                payload = json.loads(line.split("=", 1)[1])
            except ValueError:
                return {}
            return payload if isinstance(payload, dict) else {}
    return {}


def _parse_integrate_result(output):
    """M-7: extract HERDR_INTEGRATE_RESULT JSON from integrate output."""
    for line in (output or "").splitlines():
        if line.startswith("HERDR_INTEGRATE_RESULT="):
            try:
                payload = json.loads(line.split("=", 1)[1])
            except ValueError:
                return {}
            return payload if isinstance(payload, dict) else {}
    return {}


def _record_finalize_event(task, event_type, payload):
    """Best-effort finalize observability event; never breaks finalization."""
    try:
        from herdr.trajectory import run_id_for_task

        store = _get_store()
        store.record_event(
            event_type,
            dict(payload or {}),
            workflow_id=(task or {}).get("workflow_id"),
            node_id=(task or {}).get("node") or (task or {}).get("stage"),
            task_id=(task or {}).get("task_id"),
            agent_id=(task or {}).get("agent"),
            source="controller",
            run_id=run_id_for_task(task or {}),
        )
    except (OSError, ValueError, RuntimeError, AttributeError,
             subprocess.SubprocessError) as exc:
        print(f"[FINALIZE EVENT ERROR] {event_type}: {exc}")


def _escalate_finalize(task, reason, detail=None):
    """Mark a git task escalated-to-human so close stops deferring on it."""
    task_id = (task or {}).get("task_id") or "unknown"
    payload = {"reason": reason, "status": (task or {}).get("status")}
    if detail:
        payload["detail"] = detail
    print(f"[FINALIZE ESCALATED] task={task_id} reason={reason}")
    _record_finalize_event(task, "finalize_escalated", payload)
    try:
        from herdr import kernel

        kernel.update_task_metadata(
            task_id,
            {
                "finalize_escalated": True,
                "finalize_escalate_reason": reason,
            },
            store=_get_store(),
        )
    except (OSError, ValueError, RuntimeError, AttributeError,
             subprocess.SubprocessError) as exc:
        print(f"[FINALIZE ESCALATE ERROR] task={task_id}: {exc}")


def _empty_auto_releasable(task):
    """H-3: anchored empties never auto-release; legacy time empties may.

    The old判据 returned True for any task with ``baseline_commit``, but
    ``commit_task`` EMPTY always persists ``commit_basis`` as
    ``baseline_commit``/``time``, so the ``empty_unreleasable`` branch was
    dead and every true vacuum task auto-advanced to ``cleanup_ready``.
    An anchor proves the *method* is credible, not that the *empty result*
    is credible: a completed git task that produced nothing needs a human.
    Only legacy anchor-less time-basis empties (same fail-closed timestamp
    guard as adoption) release; basis-absent records escalate.
    """
    task = task or {}
    if task.get("baseline_commit"):
        return False
    return task.get("commit_basis") == "time"


def clear_finalize_escalation(task_id):
    """H-1: revoke machine-set finalize_escalated so finalize re-drives."""
    task = get_task(task_id)
    if not task:
        return False
    if not task.get("finalize_escalated"):
        return True
    try:
        from herdr import kernel
        kernel.update_task_metadata(
            task_id,
            {"finalize_escalated": False,
             "finalize_escalate_reason": None},
            store=_get_store(),
        )
    except (OSError, ValueError, RuntimeError, AttributeError,
            subprocess.SubprocessError) as exc:
        print(f"[FINALIZE UNCLEAR ERROR] task={task_id}: {exc}")
        return False
    attention_clear(f"{task_id}:finalize")
    try:
        _finalize_retry_exhausted_logged.discard(task_id)
    except AttributeError:
        pass
    print(f"[FINALIZE UNESCALATED] task={task_id}")
    return True


def _check_finalize_retry(task, status, now):
    """Shared finalize-retry driver for completed/commit Retry paths.

    Returns True when a finalize attempt was made or exhaustion was recorded.

    M-2 (AC4-2/AC4-3): deterministic outcomes (rc=3 EMPTY, rc=4 REFUSED,
    rc=5 integrate-blocked, rc=6 conflict) never consume retry budget and
    rc=4 never emits a ``commit_retry`` log. ``finalize_completed_task``
    reports ``retryable``; only retryable attempts log
    ``[REGISTRY WATCHER] ... retry finalize`` and increment the attention
    episode. Already-escalated tasks are settled and skip re-driving
    entirely (revoke via ``clear_finalize_escalation``). M-6:
    ``subprocess.CalledProcessError`` from ``check=True``/``check_output``
    paths is escalated immediately instead of bypassing budget accounting.
    """
    task = task or {}
    task_id = task.get("task_id")
    if not task_id:
        return False
    if task.get("finalize_escalated"):
        return True
    if task.get("integration_mode") != "git" or workflow_closed(
        task.get("workflow_id")
    ):
        attention_clear(f"{task_id}:finalize")
        _finalize_retry_exhausted_logged.discard(task_id)
        return False
    key = f"{task_id}:finalize"
    # P1 recovery coherence: `herdr-task clear-escalation` (or any external
    # recovery) removes the shared attention episode from another process.
    # The in-memory exhausted latch must follow the episode: otherwise a
    # later re-exhaustion would stall silently with no log and no
    # re-escalation because the stale latch suppresses both.
    if attention_get(key) is None:
        _finalize_retry_exhausted_logged.discard(task_id)
    try:
        retry, reason, exhausted = should_retry_finalize(
            status, attention_get(key), now
        )
    except (OSError, RuntimeError, ValueError, KeyError,
            subprocess.SubprocessError) as exc:
        print(f"[FINALIZE RETRY ERROR] task={task_id}: {exc}")
        _escalate_finalize(get_task(task_id) or task, "retry_driver_error",
                           {"error": f"{type(exc).__name__}: {exc}"})
        return True
    if exhausted:
        if task_id not in _finalize_retry_exhausted_logged:
            _finalize_retry_exhausted_logged.add(task_id)
            print(
                f"[FINALIZE RETRY EXHAUSTED] task={task_id} "
                f"status={status} attempts>={FINALIZE_RETRY_MAX} "
                "-> manual/coordinator intervention required"
            )
            _escalate_finalize(
                get_task(task_id) or task,
                "retry_exhausted",
                {"status": status, "reason": reason},
            )
        return True
    if not retry:
        return False
    try:
        outcome = finalize_completed_task(task_id)
    except (OSError, RuntimeError, ValueError, KeyError,
            subprocess.SubprocessError) as exc:
        # M-6: integrate check=True / check_output raises
        # CalledProcessError (SubprocessError, not OSError). Without this
        # the exception escapes to registry_watcher, skips attention
        # budgeting, and retries every ~1s with no backoff.
        print(f"[FINALIZE SUBPROCESS ERROR] task={task_id}: "
              f"{type(exc).__name__}: {exc}")
        _escalate_finalize(get_task(task_id) or task,
                           "finalize_subprocess_error",
                           {"error": f"{type(exc).__name__}: {exc}"[:500]})
        return True
    retryable = True
    if isinstance(outcome, dict) and "retryable" in outcome:
        retryable = bool(outcome.get("retryable"))
    cur_t = get_task(task_id)
    if not retryable:
        # Deterministic failure (EMPTY/REFUSED): settled via escalation or
        # auto-release, no budget consumed, no retry log (AC4-2/AC4-3).
        if cur_t and cur_t.get("status") != status:
            attention_clear(key)
            _finalize_retry_exhausted_logged.discard(task_id)
        return True
    print(
        f"[REGISTRY WATCHER] "
        f"task={task_id} "
        f"status={status} -> retry finalize ({reason})"
    )
    if cur_t and cur_t.get("status") == status:
        episode = attention_get(key) or {}
        attention_note(
            key,
            task,
            "finalize",
            reason=reason,
            attempts=int(episode.get("attempts") or 0) + 1,
        )
        attention_throttle(key, now=now)
    else:
        attention_clear(key)
        _finalize_retry_exhausted_logged.discard(task_id)
    return True


def finalize_completed_task(task_id):
    """Drive one git-finalize step; returns ``{"retryable": bool, ...}``.

    M-2: deterministic outcomes (rc=3 EMPTY, rc=4 REFUSED, rc=5
    integrate-blocked/main-dirty, rc=6 conflict) report
    ``retryable=False`` so ``_check_finalize_retry`` never consumes
    budget or logs a retry for them; only transient/unknown failures
    (rc=75, unexpected rc, state-transition failures) report True.
    H-1: rc=5 (main repo tracked changes) is persistent, never self-heals,
    so it escalates immediately instead of retrying 5x into
    ``finalize_escalated`` + silent ``delivered``.
    """
    task = get_task(task_id)

    if not task:
        print(f"[FINALIZE SKIP] task={task_id} missing")
        return {"retryable": False, "kind": "skip"}

    if task.get("status") not in ("completed", "committed"):
        print(
            f"[FINALIZE SKIP] "
            f"task={task_id} "
            f"status={task.get('status')}"
        )
        return {"retryable": False, "kind": "skip"}

    # Collaboration accelerator: authoritative task completion closes the
    # handoffs targeting it, so handoff latency metrics stay trustworthy.
    for completed in maybe_complete_on_task_done(task_id):
        print(f"[COLLABORATION COMPLETED] {completed.get('event_id')}")

    mode = task.get(
        "integration_mode",
        "none"
    )

    print(
        f"[FINALIZE] "
        f"task={task_id} "
        f"integration_mode={mode}"
    )

    # --------------------------------
    # 需要 Git 集成
    # --------------------------------
    if mode == "git":

        clone_path = task.get("clone_path")
        if clone_path and ensure_no_git_processes is not None:
            try:
                ensure_no_git_processes(clone_path)
            except Exception as exc:
                print(
                    f"[FINALIZE WAIT] task={task_id} git process still active: {exc}"
                )
                return {"retryable": True, "kind": "wait"}

        # 1. 将 Task 自己产生的修改安全提交(若此前已 committed 则跳过)
        skip_integrate = False
        if task.get("status") == "completed":
            # 提交门禁拆分:herdr 任务克隆内只跑快速必需检查,
            # 全量测试由 workflow test 节点与 pre-push 门禁负责。
            commit_env = dict(os.environ)
            commit_env["HERDR_DEFER_HEAVY_TESTS"] = "1"

            result = subprocess.run(
                [
                    TASK_MANAGER,
                    "commit",
                    task_id,
                    "--message",
                    f"task: {task_id}"
                ],
                text=True,
                capture_output=True,
                env=commit_env
            )

            if result.stdout.strip():
                print(result.stdout.strip())

            commit_payload = _parse_commit_result(result.stdout)

            if result.returncode == 3:
                fresh = get_task(task_id) or task
                print(
                    f"[FINALIZE EMPTY] "
                    f"task={task_id} "
                    f"head={commit_payload.get('head')} "
                    f"basis={commit_payload.get('basis')}"
                )
                _record_finalize_event(
                    fresh, "finalize_empty", commit_payload
                )
                if not _empty_auto_releasable(fresh):
                    _escalate_finalize(
                        fresh,
                        "empty_unreleasable",
                        commit_payload,
                    )
                    return {"retryable": False, "kind": "empty", "rc": 3}
                if not set_task_status(task_id, "cleanup_ready"):
                    print(
                        f"[FINALIZE ERROR] "
                        f"task={task_id} "
                        f"empty release did not reach cleanup_ready"
                    )
                    return {"retryable": True, "kind": "error", "rc": 3}
                skip_integrate = True
                task = get_task(task_id)
            elif result.returncode == 4:
                print(
                    f"[FINALIZE REFUSED] "
                    f"task={task_id} "
                    f"reason={commit_payload.get('reason')}"
                )
                _escalate_finalize(
                    get_task(task_id) or task,
                    "commit_refused",
                    commit_payload,
                )
                return {"retryable": False, "kind": "refused", "rc": 4}
            elif result.returncode == 75:
                print(
                    f"[FINALIZE WAIT] "
                    f"task={task_id} "
                    f"reason=git_busy retry later"
                )
                return {"retryable": True, "kind": "wait", "rc": 75}
            elif result.returncode != 0:
                print(
                    f"[COMMIT ERROR] "
                    f"task={task_id} "
                    f"rc={result.returncode}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
                _record_finalize_event(
                    task,
                    "finalize_commit_error",
                    {
                        "rc": result.returncode,
                        "detail": (
                            result.stderr.strip()
                            or result.stdout.strip()
                        )[:500],
                    },
                )
                return {"retryable": True, "kind": "error", "rc": result.returncode}
            else:
                task = get_task(task_id)

                if not task or task.get("status") != "committed":
                    print(
                        f"[FINALIZE ERROR] "
                        f"task={task_id} "
                        f"did not reach committed"
                    )
                    return {"retryable": True, "kind": "error"}

        if skip_integrate:
            print(
                f"[FINALIZE EMPTY RELEASED] "
                f"task={task_id} "
                f"completed -> cleanup_ready"
            )
        else:
            # 2. Rebase + 导入主仓库 + Integration Branch
            result = subprocess.run(
                [
                    TASK_MANAGER,
                    "integrate",
                    task_id
                ],
                text=True,
                capture_output=True
            )

            if result.stdout.strip():
                print(result.stdout.strip())

            if result.returncode == 6:
                print(
                    f"[FINALIZE REFUSED] "
                    f"task={task_id} "
                    f"reason=integrate_rebase_conflict"
                )
                _escalate_finalize(
                    get_task(task_id) or task,
                    "integrate_rebase_conflict",
                    _parse_integrate_result(result.stdout),
                )
                return {"retryable": False, "kind": "refused", "rc": 6}

            if result.returncode == 4:
                # M-7: two exit-4 sources carry different payloads:
                # remote_diverged (explicit HERDR_INTEGRATE_RESULT) vs
                # not_based_cleanly (relation check). Do not conflate.
                integrate_payload = _parse_integrate_result(result.stdout)
                if integrate_payload.get("result") == "remote_diverged":
                    reason = "integrate_remote_diverged"
                else:
                    reason = "integrate_not_based_cleanly"
                print(
                    f"[FINALIZE REFUSED] "
                    f"task={task_id} "
                    f"reason={reason}"
                )
                _escalate_finalize(
                    get_task(task_id) or task,
                    reason,
                    integrate_payload,
                )
                return {"retryable": False, "kind": "refused", "rc": 4}

            if result.returncode == 5:
                # H-1: main-repo tracked changes are persistent (never
                # self-heal). Escalate deterministically; retrying 5x
                # then auto-closing as delivered would silently drop
                # the never-integrated deliverable.
                integrate_payload = _parse_integrate_result(result.stdout)
                print(
                    f"[FINALIZE REFUSED] "
                    f"task={task_id} "
                    f"reason=integrate_main_dirty"
                )
                _escalate_finalize(
                    get_task(task_id) or task,
                    "integrate_main_dirty",
                    integrate_payload,
                )
                return {"retryable": False, "kind": "refused", "rc": 5}

            if result.returncode != 0:
                print(
                    f"[INTEGRATE ERROR] "
                    f"task={task_id} "
                    f"rc={result.returncode}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
                _record_finalize_event(
                    task,
                    "finalize_integrate_error",
                    {
                        "rc": result.returncode,
                        "detail": (
                            result.stderr.strip()
                            or result.stdout.strip()
                        )[:500],
                    },
                )
                return {"retryable": True, "kind": "error", "rc": result.returncode}

            task = get_task(task_id)

            if not task or task.get("status") != "integrated":
                print(
                    f"[FINALIZE ERROR] "
                    f"task={task_id} "
                    f"did not reach integrated"
                )
                return {"retryable": True, "kind": "error"}

            # 3. 允许清理
            if not set_task_status(
                task_id,
                "cleanup_ready"
            ):
                return {"retryable": True, "kind": "error"}

    # --------------------------------
    # 不需要 Git 集成
    # --------------------------------
    elif mode == "none":
        if not set_task_status(
            task_id,
            "cleanup_ready"
        ):
            return {"retryable": True, "kind": "error"}

    else:
        print(
            f"[FINALIZE ERROR] "
            f"task={task_id} "
            f"unknown integration_mode={mode}"
        )
        return {"retryable": False, "kind": "error"}

    # --------------------------------
    # 自动 Cleanup
    # --------------------------------
    result = subprocess.run(
        [
            TASK_MANAGER,
            "cleanup",
            task_id
        ],
        text=True,
        capture_output=True
    )

    if result.stdout.strip():
        print(result.stdout.strip())

    if result.returncode != 0:
        print(
            f"[CLEANUP ERROR] "
            f"task={task_id}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
        return {"retryable": True, "kind": "error", "rc": result.returncode}

    print(
        f"[FINALIZED] task={task_id}"
    )

    # Task 最终 cleaned 后检查整个阶段是否已经完成。
    task = get_task(task_id)

    if task:
        enqueue_stage_advance(
            task
        )
    return {"retryable": False, "kind": "finalized"}


def coordinator_worker():
    """Main dispatcher: reads items from coordinator_queue and fans them out
    to per-workflow executor threads, guaranteeing at-most-one concurrent
    dispatch per workflow without blocking the queue for other workflows.
    """
    while True:
        item = coordinator_queue.get()
        try:
            # Resolve workflow_id for routing.
            workflow_id = item.get("workflow_id")
            if not workflow_id and "task_id" in item:
                t = get_task(item["task_id"])
                workflow_id = (t or {}).get("workflow_id", "unknown")

            wf_lock = _workflow_dispatch_lock(workflow_id or "unknown")
            _coordinator_executor.submit(
                _process_coordinator_item, item, wf_lock
            )
        finally:
            coordinator_queue.task_done()


def _process_coordinator_item(item, wf_lock):
    """Process one coordinator queue item inside the executor thread pool,
    serialized per-workflow via wf_lock.
    """
    with wf_lock:
        _handle_coordinator_item(item)


def build_fix_loop_message(item, project_name="unknown"):
    """fix_loop 事件消息;派发命令是建议骨架,裁量在总指挥。"""
    workflow_id = item["workflow_id"]
    gate_stage = item["gate_stage"]
    retry_node = item["retry_node"]
    loop_count = item["loop_count"]
    max_loops = item["max_loops"]
    invalidated = item.get("invalidated") or []
    suggested_branch = item.get("suggested_branch")

    blockers_text = "\n".join(
        f"- {b.get('task_id')}: {b.get('note') or '(未记录说明)'}"
        for b in item.get("blockers") or []
    ) or "- (未记录 blocker 说明,请读取 gate 阶段任务输出)"

    escalation = ""
    if loop_count >= max_loops:
        escalation = (
            f"\n注意:已达 fix-loop 上限({loop_count}/{max_loops})。"
            "先向用户请示(继续修 / 换方案 / 放弃),"
            "未经用户确认不得派发。\n"
        )

    if item.get("exhausted"):
        escalation = (
            f"\n⛔ 回流预算已耗尽(loops={loop_count}/{max_loops},"
            f"原因={item.get('escalation_reason', 'unknown')})。"
            "Controller 已停止自动作废/重派。"
            "只允许三选一并落盘:接受现状推进 / 缩小范围重派 / 关闭工作流。"
            "禁止再次派发同范围 fix task。\n"
        )

    if suggested_branch:
        onto_flag = f"--onto {suggested_branch} "
        branch_line = suggested_branch
    else:
        onto_flag = ""
        branch_line = "(未找到,请自行确认 retry_node 最近 committed 任务的分支)"

    docs_block = shared_docs_block(
        workflow_id,
        retry_node,
        related_nodes=(gate_stage,),
        project_ctx=project_for_workflow(workflow_id) or {},
    )

    return f"""
HERDR_CONTROLLER_FIX_LOOP_EVENT

workflow_id: {workflow_id}
project_name: {project_name}
gate_stage: {gate_stage} — 验收结论 blocked
retry_node: {retry_node}
suggested_branch: {branch_line}
loop_count: {loop_count}/{max_loops}
{escalation}
Controller 已自动作废受影响的 gate 与下游 Task(共 {len(invalidated)} 个,见 Task Registry);
fix 完成后 DAG 将自动按 test → review → wrapup 顺序重新推进,旧 verdict 一并作废。

Blocker 清单(blocked 结论与修复指引):
{blockers_text}

你现在只需派发修复 Task(禁止新建 workflow、禁止放弃本 workflow):

~/HAFlow/bin/herdr-task launch --workflow-id {workflow_id} --stage {retry_node} \\
  {onto_flag}--agent auto --task-type fix --integration-mode git \\
  --goal "修复 gate {gate_stage} 的阻断项" \\
  --acceptance "<逐条对应 Blocker 清单>" \\
  --prompt "<blocker 详情、修复范围与验证方式>"

fix 产出必须落分支("--integration-mode git"),否则测试仍测旧候选而恒 blocked。
如需再次修复,对旧 fix task 使用 --supersedes。
派发完成后结束当前回合,后续推进交给 Controller。

{docs_block}

{COORDINATOR_DISCIPLINE}
""".strip()


def _handle_fix_loop_item(item):
    """门禁 blocked 的回流通知:作废已由 handle_fix_loop 原子完成,
    这里只负责把 blocker 清单与修复派发指引送到总指挥。"""
    workflow_id = item["workflow_id"]
    coord_pane = coordinator_pane_for_workflow(workflow_id)

    if not coord_pane:
        print(
            f"[FIX LOOP SKIP] "
            f"no coordinator pane for workflow={workflow_id}"
        )
        return

    gate_stage = item["gate_stage"]
    retry_node = item["retry_node"]
    project_ctx = project_for_workflow(workflow_id) or {}

    message = build_fix_loop_message(
        item,
        project_ctx.get("project_name", "unknown"),
    )

    waited = 0

    while coordinator_status(workflow_id) not in ("idle", "done"):
        if waited >= 120:
            import json as _json

            from herdr import fix_loop as fix_loop_core

            summary = fix_loop_core.summarize_fix_loop_item(item)
            episode_key = f"{workflow_id}:fix_loop:{gate_stage}"
            episode = attention_get(episode_key) or {}
            try:
                attempts = int(episode.get("attempts") or 0) + 1
            except (TypeError, ValueError):
                attempts = 1
            attention_note(
                episode_key,
                {"task_id": f"fix_loop:{gate_stage}:{retry_node}",
                 "workflow_id": workflow_id},
                "fix_loop",
                reason="coordinator_busy",
                attempts=attempts,
                next_retry_at=(
                    time.time() + liveness.attention_retry_interval()
                ),
                detail=_json.dumps(summary, ensure_ascii=False),
            )
            print(
                f"[FIX LOOP WAIT TIMEOUT] "
                f"workflow={workflow_id} "
                f"-> persisted for redelivery "
                f"(attempts={attempts})"
            )
            return

        time.sleep(2)
        waited += 2

    result = subprocess.run(
        [
            "herdr",
            "agent",
            "prompt",
            coord_pane,
            message,
            "--wait",
            "--timeout",
            "600000"
        ],
        text=True,
        capture_output=True
    )

    if result.returncode == 0:
        attention_clear(f"{workflow_id}:fix_loop:{gate_stage}")
        print(
            f"[FIX LOOP NOTIFIED] "
            f"workflow={workflow_id} "
            f"gate={gate_stage} "
            f"retry={retry_node}"
        )

        maybe_compact_coordinator(
            workflow_id,
            reason=f"fix_loop:{gate_stage}->{retry_node}",
        )
    else:
        print(
            f"[FIX LOOP ERROR] "
            f"workflow={workflow_id}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def _handle_coordinator_item(item):
    """Actual item handling logic (stage_advance or normal task event)."""
    # ==============================================
    # Fix Loop (gate verdict blocked)
    # ==============================================
    if item.get("kind") == "fix_loop":
        _handle_fix_loop_item(item)
        return

    # ==============================================
    # Workflow Stage Advance
    # ==============================================
    if item.get("kind") == "stage_advance":
        node = item.get("node")
        target_node_id = item.get("node_id") or item.get("next_stage")
        if not node:
            config = workflow_config_for(item["workflow_id"])
            if config:
                node = find_node(config, target_node_id)
        if node:
            item = dict(item, node=node)
            if node.get("node_type", "agent") != "agent":
                print(
                    f"[STAGE ADVANCE BLOCKED] workflow={item['workflow_id']} "
                    f"node={target_node_id} node_type={node.get('node_type')}: "
                    "native executor unavailable; manual handling required"
                )
                return
        # 总指挥接单:新工作流首个节点(start -> first)默认交总指挥理解
        # 需求后再派发;HERDR_COORDINATOR_INTAKE=0 或非首节点保持直派。
        if (
            item.get("stage") in (None, "", "start")
            and coordinator_intake_enabled()
        ):
            item = dict(item, intake=True)
            print(
                f"[COORDINATOR INTAKE] "
                f"workflow={item['workflow_id']} "
                f"node={target_node_id} "
                "-> dispatch via coordinator"
            )
        # 常规推进会:优先规则化直接派发(不再等待总指挥 LLM 回合);
        # 配置不足/需求缺失/launch 失败时回落既有总指挥路径。
        elif try_direct_stage_advance(item):
            return

        workflow_id = item["workflow_id"]
        coord_pane = coordinator_pane_for_workflow(workflow_id)
        if not coord_pane:
            print(
                f"[STAGE ADVANCE SKIP] "
                f"no coordinator pane for workflow={workflow_id}"
            )
            return

        stage = item["stage"]
        next_stage = item["next_stage"]
        target_node_id = item.get("node_id") or next_stage

        project_ctx = project_for_workflow(
            workflow_id
        ) or {}

        project_name = project_ctx.get(
            "project_name",
            "legacy/unknown"
        )

        project_root = project_ctx.get(
            "project_root",
            ""
        )

        base_branch = project_ctx.get(
            "base_branch",
            ""
        )

        node = item.get("node")
        if not node:
            wf_cfg = workflow_config_for(workflow_id)
            if wf_cfg:
                node = find_node(wf_cfg, next_stage)

        policy = get_stage_policy(
            next_stage
        )

        if node:
            purpose = node.get("purpose") or policy.get("purpose", "未定义")
            integration_mode = node.get("default_integration_mode") or policy.get("default_integration_mode", "none")
            task_type = node.get("default_task_type") or policy.get("default_task_type", "feat")
            node_label = node.get("label", next_stage)
            node_type = node.get("node_type", "agent")
            agent_policy = node.get("agent_policy", {})
            req_outs = node.get("required_outputs") or policy.get("required_outputs", [])
            rules_list = node.get("rules") or policy.get("rules", [])
        else:
            purpose = policy.get("purpose", "未定义")
            integration_mode = policy.get("default_integration_mode", "none")
            task_type = policy.get("default_task_type", "test")
            node_label = item.get("stage_label", next_stage)
            node_type = "agent"
            agent_policy = {}
            req_outs = policy.get("required_outputs", [])
            rules_list = policy.get("rules", [])

        required_outputs = "\n".join(
            f"- {out}" for out in req_outs
        ) or "- 未定义"

        rules = "\n".join(
            f"- {r}" for r in rules_list
        ) or "- 未定义"

        agent_policy_text = ""
        if agent_policy:
            pref = ", ".join(agent_policy.get("preferred", [])) or "无"
            exc = ", ".join(agent_policy.get("exclude", [])) or "无"
            fix = agent_policy.get("fixed") or "无"
            agent_policy_text = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Node Agent 策略
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 优先 Agent: {pref}
- 排除 Agent: {exc}
- 固定 Agent: {fix}
""".strip()

        wait_started = time.time()

        preserved_ids = [
            task["task_id"]
            for task in load_tasks()
            if task.get("workflow_id") == workflow_id
            and next_stage in (task.get("node"), task.get("stage"))
            and task.get("status") != "superseded"
            and task.get("stage_verdict") == "pass"
        ]
        preserved_text = ""
        if preserved_ids:
            preserved_text = (
                "\n已保留的有效任务（verdict=pass，禁止重复创建）：\n"
                + "\n".join(f"- {tid}" for tid in preserved_ids)
                + "\n只需补派缺失/被作废的 Task。\n"
            )

        try:
            while True:
                # Re-validate on every wait iteration: the workflow may be
                # closed or deregistered while this event sat in the queue.
                if workflow_closed(workflow_id) or not project_for_workflow(workflow_id):
                    print(
                        f"[STAGE ADVANCE DROP] "
                        f"workflow={workflow_id} "
                        f"closed/deregistered while queued"
                    )
                    return

                startup_record = project_for_workflow(workflow_id) or {}
                if startup_record.get("startup_ready") is False:
                    print(
                        f"[STARTUP WAIT] workflow={workflow_id} "
                        "queued event held until request is ready"
                    )
                    time.sleep(1)
                    continue

                status = coordinator_status(workflow_id)

                if status in ("idle", "done"):
                    intake = bool(item.get("intake"))
                    event_header = (
                        "HERDR_WORKFLOW_INTAKE_EVENT"
                        if intake
                        else "HERDR_STAGE_ADVANCE_EVENT"
                    )
                    intake_note = (
                        "这是新工作流的接单(总指挥接单机制):\n"
                        "请先完整阅读下方用户需求,理解目标、范围与约束,\n"
                        "再严格按节点职责创建第一个节点的 Task。\n\n"
                        if intake
                        else ""
                    )
                    docs_block = shared_docs_block(
                        workflow_id,
                        next_stage,
                        related_nodes=(node or {}).get("depends_on") or [],
                        project_ctx=project_ctx,
                    )
                    message = f"""
{event_header}

{intake_note}workflow_id: {workflow_id}
project_name: {project_name}
project_root: {project_root}
base_branch: {base_branch}
completed_node: {stage}
next_node: {next_stage} ({node_label})
node_type: {node_type}

用户需求：
{project_ctx.get('requirement', '').strip() or '（未提供；请停止并等待需求正文）'}

当前工作流前置依赖已全部完成。
{preserved_text}
现在进入下一节点：

{next_stage} ({node_label})

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
节点职责
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{purpose}

默认配置：

integration_mode:
{integration_mode}

task_type:
{task_type}

必须产出：

{required_outputs}

执行规则：

{rules}

{docs_block}

{agent_policy_text}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
执行要求
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. 首先执行：

   ~/HAFlow/bin/herdr-task list --workflow-id {workflow_id}

   阅读当前 Workflow 已完成节点的真实成果。

2. 根据前面节点的实际成果，
   决定当前节点需要创建几个 Task。

3. 不要固定前端、后端、数据库等角色。

   Pane = Task。

   Agent 根据 Task 动态选择。

4. 每个 Task 必须明确：

   - task_id
   - goal
   - acceptance criteria
   - agent
   - task_type
   - integration_mode

5. 创建 Task 必须使用：

   ~/HAFlow/bin/herdr-task launch

   并指定：

   --workflow-id {workflow_id}
   --node {next_stage}
   --source {project_root}

6. 默认使用本节点 policy：

   task_type={task_type}
   integration_mode={integration_mode}

   只有当前 Task 的真实性质明确需要不同配置时，
   才允许调整。

7. 不允许手工创建：

   - Clone
   - Pane
   - Branch
   - Agent

8. 可以创建一个 Task，
   也可以创建多个并行 Task。

   数量由实际工作决定。

9. 当前节点所有必要 Task 派发完成后，
   结束当前回合。

10. 后续执行、验收、返工、节点推进，
继续交给 Controller。

不要等待用户提醒。

{COORDINATOR_DISCIPLINE}
""".strip()

                    result = subprocess.run(
                        [
                            "herdr",
                            "agent",
                            "prompt",
                            coord_pane,
                            message,
                            "--wait",
                            "--timeout",
                            "600000"
                        ],
                        text=True,
                        capture_output=True
                    )

                    if result.returncode == 0:
                        mark_stage_advance_notified(
                            workflow_id,
                            target_node_id
                        )

                        print(
                            f"[STAGE ADVANCED] "
                            f"workflow={workflow_id} "
                            f"{stage} -> {next_stage}"
                        )

                        maybe_compact_coordinator(
                            workflow_id,
                            reason=f"stage_advance:{next_stage}",
                        )
                    else:
                        clear_stage_advance(
                            workflow_id,
                            target_node_id
                        )

                        print(
                            f"[STAGE ADVANCE ERROR] "
                            f"workflow={workflow_id}: "
                            f"{result.stderr.strip() or result.stdout.strip()}"
                        )

                    break

                elapsed = time.time() - wait_started
                if elapsed >= liveness.stage_advance_sla():
                    # 有界等待:总指挥长期不可用(僵尸 pane / 状态僵死)时,
                    # 释放阶段闩并记录 attention,绝不占用 workflow 调度锁空转。
                    clear_stage_advance(
                        workflow_id,
                        target_node_id
                    )
                    key = f"{workflow_id}:stage_advance:{target_node_id}"
                    episode = attention_get(key) or {}
                    attempts = int(episode.get("attempts") or 0) + 1
                    attention_note(
                        key,
                        {"task_id": f"stage_advance:{target_node_id}", "workflow_id": workflow_id},
                        "stage_advance",
                        reason="coordinator_stalled",
                        attempts=attempts,
                        next_retry_at=time.time() + liveness.attention_retry_interval(),
                        detail=f"coordinator status={status} waited={int(elapsed)}s",
                    )
                    print(
                        f"[STAGE ADVANCE STALLED] "
                        f"workflow={workflow_id} "
                        f"{stage} -> {next_stage} "
                        f"coordinator={status} waited={int(elapsed)}s "
                        f"attempts={attempts} -> deferred"
                    )
                    notify_attention(
                        "Herdr Factory · 总指挥停滞",
                        {"task_id": f"stage_advance:{target_node_id}", "workflow_id": workflow_id},
                        f"阶段 {stage} -> {next_stage} 等待总指挥超过 {int(elapsed)}s，已延迟重试。请检查总指挥 pane 状态。",
                        "stage_advance_stalled",
                    )
                    return

                print(
                    f"[STAGE ADVANCE WAIT] "
                    f"coordinator={status} "
                    f"workflow={workflow_id}"
                )

                time.sleep(1)

        finally:
            pass  # task_done is called by coordinator_worker dispatcher

        return

    # ==============================================
    # Normal Task Event
    # ==============================================

    task_id = item["task_id"]
    event_type = item["event_type"]
    key = item["key"]

    if event_type == "attention":
        # attention 事件针对 interrupted/paused 等需要裁决的中间态,
        # 只要任务仍停留在待裁决状态就有效。
        expected_status = None
    elif event_type == "blocked":
        expected_status = "blocked"
    else:
        expected_status = "agent_done"

    try:
        last_busy_log = 0
        wait_started = time.time()

        # 规则化验收快路径:非门禁节点铁证齐备直接 completed,
        # 门禁节点有唯一结论标记则直接落 verdict;两者都不命中才回落总指挥。
        if event_type == "done" and (
            try_auto_accept(task_id) or try_auto_verdict(task_id)
        ):
            attention_clear(key)
            finalize_completed_task(task_id)
            return

        while True:
            task = get_task(task_id)

            if not task:
                print(
                    f"[QUEUE DROP] "
                    f"task={task_id} missing"
                )
                break

            current_task_status = task.get("status")

            if event_type == "attention" and current_task_status not in (
                "interrupted",
                "paused",
            ):
                print(
                    f"[QUEUE STALE] "
                    f"task={task_id} "
                    f"attention resolved actual={current_task_status}"
                )
                attention_clear(key)
                break

            # 事件在等待期间已经失效
            if expected_status is not None and current_task_status != expected_status:
                print(
                    f"[QUEUE STALE] "
                    f"task={task_id} "
                    f"expected={expected_status} "
                    f"actual={current_task_status}"
                )
                break

            status = coordinator_status(task.get("workflow_id"))

            if status in ("idle", "done"):
                message = build_coordinator_message(
                    task,
                    event_type
                )

                print(
                    f"[COORDINATOR READY] "
                    f"task={task_id} "
                    f"event={event_type}"
                )

                result = subprocess.run(
                    [
                        "herdr",
                        "agent",
                        "prompt",
                        coordinator_pane_for_workflow(task.get("workflow_id")),
                        message,
                        "--wait",
                        "--timeout",
                        "600000"
                    ],
                    text=True,
                    capture_output=True
                )

                if result.returncode == 0:
                    attention_clear(key)

                    print(
                        f"[COORDINATOR NOTIFIED] "
                        f"task={task_id} "
                        f"event={event_type}"
                    )

                    # done 事件经过总指挥正式验收后，
                    # 根据 integration_mode 自动集成并清理。
                    if event_type == "done":
                        decision = wait_for_coordinator_decision(
                            task_id
                        )

                        # 第一次没有形成决策时，只自动重试一次。
                        if decision == "agent_done":
                            decision = retry_coordinator_decision(
                                task_id
                            )

                        if decision == "completed":
                            finalize_completed_task(
                                task_id
                            )

                        elif decision == "rework":
                            print(
                                f"[FINALIZE DEFER] "
                                f"task={task_id} "
                                f"status=rework"
                            )

                        elif decision == "failed":
                            print(
                                f"[FINALIZE STOP] "
                                f"task={task_id} "
                                f"status=failed"
                            )

                        else:
                            print(
                                f"[FINALIZE WAIT] "
                                f"task={task_id} "
                                f"status={decision}"
                            )
                else:
                    # 投递失败(如 agent_prompt_stalled):记录退避节流,
                    # 由 registry watcher 在退避窗口后按需重试,杜绝 1s 级风暴。
                    episode = attention_get(key) or {}
                    attempts = int(episode.get("attempts") or 0) + 1
                    delay = liveness.backoff_delay(attempts)
                    attention_note(
                        key,
                        task,
                        event_type,
                        reason="delivery_failed",
                        attempts=attempts,
                        next_retry_at=time.time() + delay,
                        detail=(result.stderr.strip() or result.stdout.strip())[:400],
                    )

                    print(
                        "[COORDINATOR ERROR]",
                        result.stderr.strip()
                        or result.stdout.strip(),
                        f"-> retry_in={int(delay)}s attempts={attempts}"
                    )

                break

            now = time.time()

            elapsed = now - wait_started
            if elapsed >= liveness.coordinator_delivery_sla():
                handle_coordinator_delivery_stall(
                    item, task, elapsed
                )
                break

            if now - last_busy_log >= 15:
                last_busy_log = now
                print(
                    f"[COORDINATOR BUSY] "
                    f"status={status} "
                    f"task={task_id} "
                    f"waited={int(elapsed)}s"
                )

            time.sleep(2)

    finally:
        with lock:
            queued_events.discard(key)


# ============================================================
# 规则化验收 (auto-accept) — 非门禁节点免除总指挥 LLM 回合
# ============================================================

# 背景(lessons §61)：agent_done 后的总指挥验收回合实测占用 2.6h/8h。非门禁
# 节点(需求/计划/实现)的验收标准本就是"产物落盘 + 变更受控"，可由
# verify-baseline 铁证直接判定；门禁节点(test/review/wrapup)保留裁决语义。
def auto_accept_enabled():
    value = os.environ.get("HERDR_AUTO_ACCEPT", "1")
    return value.strip().lower() not in ("0", "false", "off", "no")


def node_is_gate(workflow_id, node_id):
    """节点是否带门禁配置(含 GATE_DEFAULTS 的 test/review/wrapup)。

    配置不可判定时保守返回 True(保留总指挥路径),绝不因配置异常
    误吞门禁裁决。
    """
    try:
        wf_cfg = workflow_config_for(workflow_id) or {}
        nodes_by_id = {n["id"]: n for n in wf_cfg.get("nodes", [])}
        return resolve_gate_config(nodes_by_id.get(node_id), node_id) is not None
    except Exception:
        return True


def task_changes_recorded(task):
    """verify-baseline 铁证：至少一个受控文件变更(TASK_CHANGED)。"""
    task_id = task.get("task_id")
    if not task_id:
        return False
    try:
        result = subprocess.run(
            [TASK_MANAGER, "verify-baseline", task_id],
            text=True,
            capture_output=True,
            timeout=60,
        )
    except Exception:
        return False

    output = (result.stdout or "") + "\n" + (result.stderr or "")
    for line in output.splitlines():
        if line.startswith("HERDR_BASELINE_RESULT="):
            try:
                payload = json.loads(line.split("=", 1)[1])
            except (ValueError, IndexError):
                continue
            return bool(payload.get("changes"))
    return "TASK_CHANGED" in output


def try_auto_accept(task_id):
    """非门禁节点规则化验收：变更铁证齐备即 completed，免总指挥回合。

    保守口径：只接管 agent_done 的非门禁节点；产物/变更证据不足、
    门禁节点、被显式关闭(HERDR_AUTO_ACCEPT=0)时返回 False，走原路径。
    """
    if not auto_accept_enabled():
        return False

    task = get_task(task_id)
    if not task or task.get("status") != "agent_done":
        return False

    workflow_id = task.get("workflow_id")
    node_id = task.get("node") or task.get("stage")
    if not workflow_id or not node_id:
        return False

    if node_is_gate(workflow_id, node_id):
        return False

    if not task_changes_recorded(task):
        return False

    if not set_task_status(task_id, "completed"):
        return False

    print(
        f"[AUTO ACCEPT] task={task_id} "
        f"node={node_id} baseline=TASK_CHANGED -> completed"
    )
    return True


# ============================================================
# 门禁规则化裁决 (auto-verdict) — 门禁节点免除总指挥 LLM 回合
# ============================================================

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


def auto_verdict_enabled():
    value = os.environ.get("HERDR_AUTO_VERDICT", "1")
    return value.strip().lower() not in ("0", "false", "off", "no")


def _normalize_gate_verdict(value):
    return _GATE_VERDICT_ALIASES.get(str(value or "").strip().lower())


def _gate_verdict_file_candidates(task):
    """结论文件候选路径：状态目录优先，clone 内旧契约路径兜底。"""
    candidates = []
    task_id = task.get("task_id")
    if task_id and direct_dispatch_planner is not None:
        try:
            candidates.append(direct_dispatch_planner.gate_verdict_path(task_id))
        except Exception:
            pass
    clone_path = task.get("clone_path")
    if clone_path:
        candidates.append(os.path.join(clone_path, GATE_VERDICT_FILE))
    return candidates


def _verdict_from_file(task):
    for path in _gate_verdict_file_candidates(task):
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        verdict = _normalize_gate_verdict(payload.get("verdict"))
        if verdict:
            return verdict, str(payload.get("note") or "").strip()
    return None, ""


def _is_instructional_or_ambiguous_verdict_line(line):
    """过滤指令模板、Prompt回显或歧义讨论行，防止误判为有效门禁结论。"""
    if not line:
        return True
    # 1. 单行出现多次标记(如 'HERDR_GATE_VERDICT: pass 或 HERDR_GATE_VERDICT: blocked')
    if line.count(GATE_VERDICT_MARKER) > 1:
        return True

    # 2. 含有二选一、条件词或模板占位符
    line_lower = line.lower()
    disjunctive_keywords = (
        "或", " or ", " / ", "二选一", "示例", "example", "格式", "template",
        "<pass", "[pass", "<blocked", "[blocked", "二者选一"
    )
    if any(k in line_lower for k in disjunctive_keywords):
        return True

    return False


def _verdict_from_screen(task):
    pane_id = task.get("pane_id")
    if not pane_id:
        return None, ""
    try:
        result = subprocess.run(
            ["herdr", "pane", "read", pane_id, "--source", "visible"],
            text=True,
            capture_output=True,
            timeout=10,
        )
    except Exception:
        return None, ""

    screen = (result.stdout or "") + "\n" + (result.stderr or "")
    verdicts = set()
    note = ""
    for line in screen.splitlines():
        if GATE_VERDICT_MARKER in line:
            if _is_instructional_or_ambiguous_verdict_line(line):
                continue
            raw = line.split(GATE_VERDICT_MARKER, 1)[1].strip()
            tokens = raw.split()
            if not tokens:
                continue
            first_token = tokens[0].strip().rstrip(".,;:")
            normalized = _normalize_gate_verdict(first_token)
            if normalized:
                # 检查第一词后续 token 中是否混有对立裁决词(如 'pass blocked' 等歧义讨论)
                if len(tokens) > 1:
                    trailing = {t.strip(".,;:()/[]{}").lower() for t in tokens[1:]}
                    conflicting = "blocked" if normalized == "pass" else "pass"
                    if conflicting in trailing:
                        continue
                verdicts.add(normalized)
        if GATE_NOTE_MARKER in line and not note:
            candidate_note = line.split(GATE_NOTE_MARKER, 1)[1].strip()
            # 过滤模板占位符说明如 'HERDR_GATE_NOTE: <原因>' 或 'HERDR_GATE_NOTE: 一句话结论'
            if candidate_note and not any(k in candidate_note for k in ("<原因>", "一句话结论", "阻塞原因清单", "note...")):
                note = candidate_note

    if len(verdicts) != 1:
        return None, ""
    return verdicts.pop(), note


def read_gate_verdict(task):
    """(verdict, note, source)：文件与终端两路信号一致才返回 verdict。"""
    file_verdict, file_note = _verdict_from_file(task)
    screen_verdict, screen_note = _verdict_from_screen(task)

    signals = {v for v in (file_verdict, screen_verdict) if v}
    if len(signals) != 1:
        return None, "", ""

    verdict = signals.pop()
    note = file_note or screen_note
    source = "file+screen" if file_verdict and screen_verdict else (
        "file" if file_verdict else "screen"
    )
    return verdict, note, source


def try_auto_verdict(task_id):
    """门禁节点规则化裁决：报告/终端给出唯一结论时直接落 verdict。

    pass -> 推进下一节点；blocked -> 既有 fix-loop 自动回流。
    信号缺失/冲突、非门禁节点、环境开关关闭时返回 False 回落总指挥。
    """
    if not auto_verdict_enabled():
        return False

    task = get_task(task_id)
    if not task or task.get("status") != "agent_done":
        return False

    workflow_id = task.get("workflow_id")
    node_id = task.get("node") or task.get("stage")
    if not workflow_id or not node_id:
        return False

    if not node_is_gate(workflow_id, node_id):
        return False

    verdict, note, source = read_gate_verdict(task)
    if verdict not in ("pass", "blocked"):
        return False
    if verdict == "blocked" and not note:
        note = f"gate verdict blocked (auto-verdict via {source or 'signal'})"

    result = subprocess.run(
        [
            TASK_MANAGER, "set", task_id, "completed",
            "--verdict", verdict, "--note", note,
        ],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        print(
            f"[AUTO VERDICT ERROR] task={task_id}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
        return False

    print(
        f"[AUTO VERDICT] task={task_id} node={node_id} "
        f"verdict={verdict} source={source}"
    )
    return True


def check_task_deliverables_ready(task):
    """Check whether deliverables required by the task or node are present and non-empty."""
    clone_path = task.get("clone_path")
    if not clone_path or not os.path.exists(clone_path):
        return False
    wf_id = task.get("workflow_id")
    node_id = task.get("node") or task.get("stage")
    required_outputs = []
    if wf_id and node_id:
        try:
            wf_cfg = workflow_config_for(wf_id)
            if wf_cfg:
                node = find_node(wf_cfg, node_id)
                if node:
                    required_outputs = node.get("required_outputs", [])
        except Exception:
            pass

    if required_outputs:
        for rel in required_outputs:
            p = os.path.join(clone_path, rel)
            if not os.path.exists(p) or os.path.getsize(p) == 0:
                return False
        return True

    # Fallback to checking git status for any changes
    try:
        res = subprocess.run(
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


def gate_verdict_ready(task):
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
    if not node_is_gate(workflow_id, node_id):
        return False
    verdict, _, _ = read_gate_verdict(task)
    return verdict in ("pass", "blocked")


def _supervisor_pane_report(task):
    """Agent done report source: bounded pane read, best-effort (never raises)."""
    pane_id = task.get("pane_id")
    if not pane_id:
        return None
    try:
        res = subprocess.run(
            ["herdr", "pane", "read", pane_id, "--source", "visible"],
            text=True,
            capture_output=True,
            timeout=3,
        )
        return (res.stdout or "") + "\n" + (res.stderr or "")
    except Exception:
        return None


def _supervisor_attention(task, decision, event_type):
    """Intervention ledger entry; task status is never touched here."""
    reasons = "; ".join((decision or {}).get("reasons") or ["policy intervention"])
    key = f"supervisor:{task.get('task_id')}"
    previous = attention_get(key) or {}
    note = attention_note(key, task, event_type, reasons)
    if previous.get("event_type") != event_type:
        _supervisor_notify(
            task,
            event_type.split("_")[-1],
            f"语义监督拦截: {reasons}",
        )
    return note


def _supervisor_notify(task, action, message):
    """Best-effort operator visibility for an enforced intervention."""
    if os.environ.get("HERDR_CONTROLLER_TEST"):
        return
    notify_attention(
        "Herdr Factory · 语义监督拦截",
        task,
        message,
        f"supervisor_{str(action).lower()}",
    )


def _supervisor_retry(task, decision, store=None):
    """RETRY -> the existing rework flow; return only observed state facts."""
    return _supervisor_retry_with_store(task, decision, store=store)


def _intervention_metadata(decision, action, attempt_count=None):
    intervention = (decision.get("intervention") or {}) if isinstance(decision, dict) else {}
    metadata = {
        "intervention_id": intervention.get("intervention_id"),
        "decision_id": intervention.get("decision_id") or decision.get("decision_id"),
        "action": action,
    }
    if attempt_count is not None:
        metadata["attempt_count"] = int(attempt_count)
    return {key: value for key, value in metadata.items() if value is not None}


def _supervisor_retry_with_store(task, decision, store=None):
    """Apply one new retry iteration using the existing legal task flow."""
    from herdr.intervention import attempt_count_for_task
    store = store or _get_store()
    fresh_before = store.get_task(task.get("task_id")) if store is not None else None
    fresh_before = fresh_before or task
    previous_status = fresh_before.get("status")
    current_attempt = attempt_count_for_task(fresh_before)
    intervention = (decision.get("intervention") or {}) if isinstance(decision, dict) else {}
    max_attempts = int(intervention.get("max_attempts") or 0)
    if max_attempts > 0 and current_attempt >= max_attempts:
        error = RuntimeError("RETRY budget exhausted")
        error.intervention_error = {
            "code": "retry_budget_exhausted",
            "attempt_count": current_attempt,
            "max_attempts": max_attempts,
        }
        raise error

    # A task already in rework is active work, not evidence that this new
    # Intervention ran. Re-enter the legal working path to start a new
    # iteration; never use rework -> rework as a fake retry.
    target_status = "working" if previous_status == "rework" else "rework"
    next_attempt = current_attempt + 1
    metadata = _intervention_metadata(decision, "RETRY", next_attempt)
    applied_status = target_status
    if store is not None and store.get_task(task.get("task_id")) is not None:
        from herdr import kernel
        kernel.transition_task(
            task_id=task.get("task_id"),
            to_status=target_status,
            reason="supervisor_retry",
            source="supervisor",
            metadata=metadata,
            store=store,
        )
    elif not set_task_status(task.get("task_id"), target_status):
        raise RuntimeError("RETRY handler could not enter retry flow")
    fresh = store.get_task(task.get("task_id")) if store is not None else None
    fresh = fresh or get_task(task.get("task_id")) or dict(fresh_before, status=applied_status)
    dispatch = _dispatch_supervisor_retry(fresh_before, decision, store)
    return {
        "action": "RETRY",
        "previous_status": previous_status,
        "new_status": fresh.get("status", target_status),
        "attempt_count": attempt_count_for_task(fresh),
        **dispatch,
    }


def _dispatch_supervisor_retry(task, decision, store):
    """Dispatch a real new retry iteration through the existing Agent path."""
    intervention = decision.get("intervention") or {}
    pane_id = task.get("pane_id")
    if not pane_id:
        raise RuntimeError("RETRY dispatch requires task pane_id")
    intervention_id = intervention.get("intervention_id")
    decision_id = intervention.get("decision_id") or decision.get("decision_id")
    from herdr.trajectory import run_id_for_task
    prior_intent = _latest_action_dispatch_intent(
        task, store, "RETRY", intervention_id=intervention_id,
    )
    if prior_intent is not None:
        payload = dict(prior_intent.get("payload") or {})
        payload["dispatch_recovered"] = True
        store.record_event(
            "retry_dispatched", payload,
            workflow_id=task.get("workflow_id"),
            node_id=task.get("node") or task.get("stage"),
            task_id=task.get("task_id"), agent_id=task.get("agent"),
            source="supervisor", run_id=run_id_for_task(task),
        )
        return {"retry_dispatched": True, "dispatch_recovered": True}
    payload = {
        "intervention_id": intervention_id,
        "decision_id": decision_id,
        "action": "RETRY",
        "pane_id": pane_id,
        "attempt_count": intervention.get("attempt"),
        "verification_pending": False,
    }
    working_context_ref = _working_context_ref_for_task(task, store=store)
    if working_context_ref:
        payload["working_context_id"] = working_context_ref
    store.record_event(
        "retry_dispatch_intent", payload,
        workflow_id=task.get("workflow_id"),
        node_id=task.get("node") or task.get("stage"),
        task_id=task.get("task_id"), agent_id=task.get("agent"),
        source="supervisor", run_id=run_id_for_task(task),
    )
    prompt = (
        "Supervisor requested a fresh retry iteration for this task.\n"
        "Continue the existing rework/fix loop now, run the configured work "
        "and verification steps, and only report completion after the new "
        "iteration has actually executed.\n"
        f"HERDR_RETRY_INTERVENTION_ID:{intervention_id}\n"
        f"HERDR_RETRY_DECISION_ID:{decision_id}\n"
        "HERDR_RETRY_ACTION:RETRY\n"
        + (f"WORKING_CONTEXT_REF:{working_context_ref}\n" if working_context_ref else "")
        + "Load the immutable context by reference before retrying.\n"
    )
    result = subprocess.run(
        ["herdr", "agent", "prompt", str(pane_id), prompt],
        text=True, capture_output=True, timeout=SUPERVISOR_VERIFY_DISPATCH_TIMEOUT,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "retry prompt failed").strip()
        raise RuntimeError(f"RETRY dispatch failed: {detail[:400]}")
    store.record_event(
        "retry_dispatched", payload,
        workflow_id=task.get("workflow_id"),
        node_id=task.get("node") or task.get("stage"),
        task_id=task.get("task_id"), agent_id=task.get("agent"),
        source="supervisor", run_id=run_id_for_task(task),
    )
    return {"retry_dispatched": True}


def _supervisor_verify(task, decision, store=None):
    """VERIFY -> re-enter the existing verification/rework route.

    Verification facts are emitted later by the existing tests_completed and
    verification_completed path; this handler never manufactures a verdict.
    """
    store = store or _get_store()
    fresh_before = store.get_task(task.get("task_id")) if store is not None else None
    fresh_before = fresh_before or task
    previous_status = fresh_before.get("status")
    applied_status = previous_status
    if previous_status != "rework":
        metadata = _intervention_metadata(decision, "VERIFY")
        if store is not None and store.get_task(task.get("task_id")) is not None:
            from herdr import kernel
            kernel.transition_task(
                task_id=task.get("task_id"),
                to_status="rework",
                reason="supervisor_verify",
                source="supervisor",
                metadata=metadata,
                store=store,
            )
        elif not set_task_status(task.get("task_id"), "rework"):
            raise RuntimeError("VERIFY handler could not enter verification rework")
        else:
            applied_status = "rework"
    dispatch = _dispatch_supervisor_verification(fresh_before, decision, store)
    fresh = store.get_task(task.get("task_id")) if store is not None else None
    fresh = fresh or get_task(task.get("task_id")) or dict(fresh_before, status=applied_status)
    return {
        "action": "VERIFY",
        "previous_status": previous_status,
        "new_status": fresh.get("status"),
        **dispatch,
        "verification_requested": True,
        "verification_pending": True,
    }


def _working_context_ref_for_task(task, store=None):
    try:
        from herdr.context_compiler import compile_working_context, infer_agent_role
        context = compile_working_context(
            workflow_id=task.get("workflow_id"),
            task_id=task.get("task_id"),
            agent_role=infer_agent_role(task),
            store=store,
        )
        return context.context_id
    except Exception as exc:
        print(f"[WORKING_CONTEXT SKIPPED] task={task.get('task_id')}: {type(exc).__name__}")
        return None


def _dispatch_supervisor_verification(task, decision, store):
    """Submit one real verification prompt through the existing Agent path."""
    intervention = decision.get("intervention") or {}
    pane_id = task.get("pane_id")
    if not pane_id:
        raise RuntimeError("VERIFY dispatch requires task pane_id")
    intervention_id = intervention.get("intervention_id")
    decision_id = intervention.get("decision_id") or decision.get("decision_id")
    from herdr.trajectory import run_id_for_task
    prior_intent = _latest_verification_dispatch_intent(
        task, store, intervention_id=intervention_id,
    )
    if prior_intent is not None:
        payload = dict(prior_intent.get("payload") or {})
        payload["dispatch_recovered"] = True
        store.record_event(
            "verification_dispatched",
            payload,
            workflow_id=task.get("workflow_id"),
            node_id=task.get("node") or task.get("stage"),
            task_id=task.get("task_id"),
            agent_id=task.get("agent"),
            source="supervisor",
            run_id=run_id_for_task(task),
        )
        return {"verification_dispatched": True, "dispatch_recovered": True}
    dispatch_started_at = time.time()
    evidence_baseline = _verification_snapshot(task.get("clone_path"))
    dispatch_payload = {
        "intervention_id": intervention_id,
        "decision_id": decision_id,
        "action": "VERIFY",
        "pane_id": pane_id,
        "dispatch_started_at": dispatch_started_at,
        "evidence_baseline": evidence_baseline,
        "verification_pending": True,
    }
    working_context_ref = _working_context_ref_for_task(task, store=store)
    if working_context_ref:
        dispatch_payload["working_context_id"] = working_context_ref
    store.record_event(
        "verification_dispatch_intent",
        dispatch_payload,
        workflow_id=task.get("workflow_id"),
        node_id=task.get("node") or task.get("stage"),
        task_id=task.get("task_id"),
        agent_id=task.get("agent"),
        source="supervisor",
        run_id=run_id_for_task(task),
    )
    prompt = (
        "Supervisor requested a fresh verification execution for this task.\n"
        "Run the existing project verification/test loop now (including "
        "~/HAFlow/bin/herdr-loop eval when configured), inspect the resulting "
        ".herdr-loop/EVAL_DONE.json, and only report completion after the new "
        "verification evidence is written.\n"
        f"HERDR_VERIFY_INTERVENTION_ID:{intervention_id}\n"
        f"HERDR_VERIFY_DECISION_ID:{decision_id}\n"
        "HERDR_VERIFY_ACTION:VERIFY\n"
        + (f"WORKING_CONTEXT_REF:{working_context_ref}\n" if working_context_ref else "")
        + "Load the immutable context by reference before verification.\n"
    )
    result = subprocess.run(
        ["herdr", "agent", "prompt", str(pane_id), prompt],
        text=True,
        capture_output=True,
        timeout=SUPERVISOR_VERIFY_DISPATCH_TIMEOUT,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "verification prompt failed").strip()
        raise RuntimeError(f"VERIFY dispatch failed: {detail[:400]}")
    store.record_event(
        "verification_dispatched",
        dispatch_payload,
        workflow_id=task.get("workflow_id"),
        node_id=task.get("node") or task.get("stage"),
        task_id=task.get("task_id"),
        agent_id=task.get("agent"),
        source="supervisor",
        run_id=run_id_for_task(task),
    )
    return {"verification_dispatched": True}


def _default_collab_sender(pane_id, prompt):
    result = subprocess.run(
        ["herdr", "agent", "prompt", str(pane_id), prompt],
        text=True, capture_output=True, timeout=120,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "collaboration prompt failed").strip()
        raise RuntimeError(f"collaboration dispatch failed: {detail[:400]}")
    return {"ok": True}


def _collab_task_pane(task):
    pane = task.get("pane_id")
    if pane:
        return pane
    runtime = task.get("runtime") or {}
    return runtime.get("pane_id")


def _collab_task_run(task):
    # Collaboration scope, NOT the per-task run_id: every herdr-task launch
    # mints its own run_id, so scope must be the shared workflow execution
    # identity. Fail closed when nothing identifies the execution.
    try:
        from herdr.collaboration import collab_scope_for_task
        return collab_scope_for_task(task)
    except Exception:
        return None


def collaboration_enabled():
    import os as _os
    return _os.environ.get("HERDR_COLLABORATION_ENABLED", "1") != "0"


def _collab_prior_intent(event_id, to_task_id, db_path):
    from herdr import state_db as _sdb
    try:
        rows = _sdb.list_events(
            event_type="collaboration_dispatch_intent",
            task_id=to_task_id, db_path=db_path,
        )
    except Exception:
        return None
    for row in rows or []:
        payload = row.get("payload") or {}
        if payload.get("collaboration_event_id") == event_id:
            return row
    return None


def _reconcile_collaboration_ack(event_id, target, db_path):
    """ACK reconciliation shared by the dispatch and recovery paths.

    A target already working never emits another working transition, so a
    handoff created after that point would stick at dispatched without this
    check. Returns the acknowledged row, or None when not applicable.
    """
    from herdr import state_db as _sdb

    try:
        if (target.get("status") or "") == "working":
            return _sdb.mark_collaboration_acknowledged(event_id, db_path=db_path)
    except Exception as exc:
        print(f"[COLLABORATION ACK RECONCILE SKIPPED] event={event_id}: {type(exc).__name__}")
    return None


def _working_context_ref_valid(event, target_task, db_path=None):
    """Validate V1 refs strictly; only an absent ref is legacy-compatible."""
    from herdr import state_db as _sdb
    from herdr.context_compiler import infer_agent_role
    from herdr.trajectory import run_id_for_task

    refs = list(event.get("context_refs") or [])
    if not refs:
        return True
    try:
        expected_role = infer_agent_role(target_task)
    except ValueError:
        return False
    for ref in refs:
        value = str(ref or "")
        if not value.startswith("wc_"):
            return False
        context = _sdb.get_working_context(value, db_path=db_path)
        if context is None:
            return False
        try:
            source_watermark = int(context.get("source_watermark") or 0)
        except (TypeError, ValueError):
            return False
        if len(value) <= 3 or not context.get("source_version") or source_watermark <= 0:
            return False
        if str(context.get("task_id") or "") != str(event.get("to_task_id") or ""):
            return False
        if str(context.get("workflow_id") or "") != str(event.get("workflow_id") or ""):
            return False
        if str(context.get("run_scope") or "") != str(event.get("run_id") or ""):
            return False
        expected_run_id = run_id_for_task(dict(target_task))
        if str(context.get("run_id") or "") != str(expected_run_id or ""):
            return False
        if str(context.get("agent_role") or "") != expected_role:
            return False
    return True


def dispatch_collaboration_event(event_id, tasks_by_id, prompt_sender=None, db_path=None):
    """Dispatch one CollaborationEvent through the existing Herdr prompt path.

    Pure assembly: identity/run/pane checks, durable intent, minimal prompt,
    real ``herdr agent prompt`` delivery (injectable for tests). Never falls
    back to another pane or agent: missing target fails the event.
    """
    from herdr import collaboration as _collab
    from herdr import state_db as _sdb

    sender = prompt_sender or _default_collab_sender
    event = _sdb.get_collaboration_event(event_id, db_path=db_path)
    if event is None:
        raise ValueError(f"collaboration event '{event_id}' not found")
    if event["status"] in ("dispatched", "acknowledged", "completed"):
        return {"dispatched": True, "recovered": True,
                "status": event["status"], "event_id": event_id}
    if event["status"] == "failed":
        return {"dispatched": False, "status": "failed", "event_id": event_id}

    tasks = tasks_by_id or {}
    target = tasks.get(event["to_task_id"])
    if target is None:
        return _sdb.mark_collaboration_failed(event_id, db_path=db_path)
    if _collab_task_run(target) != event["run_id"]:
        return _sdb.mark_collaboration_failed(event_id, db_path=db_path)
    pane_id = _collab_task_pane(target)
    if not pane_id:
        return _sdb.mark_collaboration_failed(event_id, db_path=db_path)
    source_task = tasks_by_id.get(event.get("from_task_id"))
    if (
        source_task is None
        or str(source_task.get("workflow_id") or "") != str(event.get("workflow_id") or "")
        or _collab.collab_scope_for_task(source_task) != str(event.get("run_id") or "")
    ):
        return _sdb.mark_collaboration_failed(event_id, db_path=db_path)
    if not _working_context_ref_valid(event, target, db_path=db_path):
        return _sdb.mark_collaboration_failed(event_id, db_path=db_path)
    prompt_event = dict(event)
    context_refs = event.get("context_refs") or []
    allowed_evidence_refs = set()
    for context_ref in context_refs:
        context_row = _sdb.get_working_context(context_ref, db_path=db_path)
        if context_row is None:
            continue
        for field_name in (
            "completed", "artifacts", "evidence", "findings", "decisions", "blockers",
            "open_questions", "verification", "handoffs",
        ):
            for item in context_row.get(field_name) or []:
                if field_name == "evidence" and item.get("source_ref"):
                    allowed_evidence_refs.add(str(item["source_ref"]))
                allowed_evidence_refs.update(
                    str(ref) for ref in item.get("evidence_refs") or [] if ref
                )
    if context_refs:
        from herdr.context_models import _canonical_evidence_ref
        filtered_evidence_refs = []
        for raw_ref in event.get("evidence_refs") or []:
            canonical_ref = _canonical_evidence_ref(raw_ref) or str(raw_ref)
            if canonical_ref in allowed_evidence_refs:
                filtered_evidence_refs.append(canonical_ref)
        prompt_event["evidence_refs"] = filtered_evidence_refs

    if _collab_prior_intent(event_id, event["to_task_id"], db_path) is not None:
        recovered = _sdb.mark_collaboration_dispatched(event_id, db_path=db_path)
        recovered["recovered"] = True
        recovered["dispatched"] = True
        acked = _reconcile_collaboration_ack(event_id, target, db_path)
        if acked is not None:
            acked["recovered"] = True
            acked["dispatched"] = True
            return acked
        return recovered

    _sdb.record_event(
        {"event_type": "collaboration_dispatch_intent",
         "workflow_id": event.get("workflow_id"),
         "task_id": event["to_task_id"],
         "agent_id": event.get("to_agent"),
         "run_id": event["run_id"],
         "payload": {"collaboration_event_id": event_id, "pane_id": pane_id},
         "source": "collaboration"},
        db_path=db_path,
    )
    prompt = _collab.build_handoff_prompt(
        prompt_event, next_action=f"Proceed as {event.get('to_agent') or ''}.",
    )
    try:
        sender(pane_id, prompt)
    except Exception:
        return _sdb.mark_collaboration_failed(event_id, db_path=db_path)
    marked = _sdb.mark_collaboration_dispatched(event_id, db_path=db_path)
    marked["dispatched"] = True
    # ACK fast-path: the target may already be working (it was launched
    # before this event existed, so the working-transition hook never fired
    # for it). Reconcile immediately instead of waiting for a transition
    # that may never come.
    acked = _reconcile_collaboration_ack(event_id, target, db_path)
    if acked is not None:
        acked["dispatched"] = True
        return acked
    return marked


def ack_collaboration_event_for_task(to_task_id, db_path=None):
    """ACK all dispatched handoffs targeting a task that just entered working."""
    from herdr import state_db as _sdb

    acked = []
    for row in _sdb.list_collaboration_events(
        task_id=to_task_id, status="dispatched", db_path=db_path,
    ):
        if row["to_task_id"] != to_task_id:
            continue
        acked.append(_sdb.mark_collaboration_acknowledged(row["event_id"], db_path=db_path))
    return acked


def maybe_ack_on_working(task_id, db_path=None):
    """Accelerator hook for the working transition: never breaks the caller."""
    if not collaboration_enabled():
        return []
    try:
        return ack_collaboration_event_for_task(task_id, db_path=db_path)
    except Exception as exc:
        print(f"[COLLABORATION ACK SKIPPED] task={task_id}: {type(exc).__name__}")
        return []


_NODE_DONE_STATUSES = frozenset({
    "completed", "committed", "integrated", "cleanup_ready", "cleaned",
})


def maybe_dispatch_node_handoffs(*, workflow_id, ready_id, dep_ids, launched,
                                 tasks_by_id=None, prompt_sender=None, db_path=None):
    """Accelerator hook for deterministic node advance.

    For each newly launched task on a known edge (implementation→review,
    review→test), creates one HANDOFF from the latest completed upstream task
    and dispatches it through Herdr. Unknown edges return ``skipped`` so the
    existing Coordinator path stays authoritative. Never raises: a handoff
    failure must not break the already-launched downstream task.
    """
    from herdr import collaboration as _collab
    from herdr import state_db as _sdb

    launched = launched or []
    if not collaboration_enabled():
        return [{"task_id": t, "skipped": True, "reason": "disabled"} for t in launched]
    try:
        if tasks_by_id is None:
            tasks_by_id = {t.get("task_id"): t for t in (load_tasks() or [])
                           if isinstance(t, dict) and t.get("task_id")}
        tasks = tasks_by_id or {}
    except Exception as exc:
        return [{"task_id": t, "status": "failed", "error": type(exc).__name__}
                for t in launched]

    results = []
    for to_id in launched:
        try:
            target = tasks.get(to_id)
            if target is None:
                results.append({"task_id": to_id, "status": "failed",
                                "reason": "target_task_missing"})
                continue
            dep_hit = None
            trigger = None
            for dep in (dep_ids or []):
                inferred = _collab.infer_handoff_trigger(dep, ready_id)
                if inferred is not None:
                    dep_hit = dep
                    trigger = inferred
                    break
            if trigger is None:
                results.append({"task_id": to_id, "skipped": True,
                                "reason": "no_deterministic_route"})
                continue
            route = _collab.route_deterministic_handoff(trigger=trigger)
            target_scope = _collab.collab_scope_for_task(target)
            target_has_explicit_scope = bool(
                target.get("workflow_run_id") or target.get("execution_id")
            )

            def legacy_pair_is_proven(candidate):
                if target_has_explicit_scope or candidate.get("workflow_run_id") or candidate.get("execution_id"):
                    return _collab.collab_scope_for_task(candidate) == target_scope
                if not target.get("run_id") or not candidate.get("run_id"):
                    return False
                return str(target.get("run_id")) == str(candidate.get("run_id"))

            upstream = [t for t in tasks.values()
                        if isinstance(t, dict)
                        and t.get("workflow_id") == workflow_id
                        and (t.get("node") == dep_hit or t.get("stage") == dep_hit)
                        and t.get("status") in _NODE_DONE_STATUSES
                        and (not target_scope or _collab.collab_scope_for_task(t) == target_scope)
                        and legacy_pair_is_proven(t)]
            if not upstream:
                results.append({"task_id": to_id, "skipped": True,
                                "reason": "no_completed_upstream"})
                continue
            upstream.sort(key=lambda t: float(t.get("updated_at") or 0))
            from_task = upstream[-1]
            run_id = _collab.collab_scope_for_task(from_task)
            branch = from_task.get("branch")
            event = _sdb.create_collaboration_event({
                "run_id": run_id,
                "workflow_id": workflow_id,
                "from_task_id": from_task.get("task_id"),
                "from_agent": from_task.get("agent") or "",
                "from_pane_id": _collab_task_pane(from_task),
                "to_task_id": to_id,
                "to_agent": target.get("agent") or (route or {}).get("to_agent") or "",
                "type": (route or {}).get("type") or "HANDOFF",
                "summary": str(from_task.get("goal") or f"{dep_hit} completed."),
                "artifact_refs": ([f"branch:{branch}"] if branch else []),
                "evidence_refs": [],
                "context_refs": [],
                "source_fact_id": f"{workflow_id}:{dep_hit}:completed",
            }, db_path=db_path)
            try:
                from herdr.context_compiler import compile_working_context, infer_agent_role
                target_context = compile_working_context(
                    workflow_id=workflow_id,
                    task_id=to_id,
                    agent_role=infer_agent_role(target),
                    planned_links=[{
                        "from_task_id": from_task.get("task_id"),
                        "to_task_id": to_id,
                    }],
                    db_path=db_path,
                )
                event = _sdb.attach_working_context_ref(
                    event["event_id"], target_context.context_id, db_path=db_path,
                )
            except Exception as exc:
                print(f"[WORKING_CONTEXT SKIPPED] task={to_id}: {type(exc).__name__}")
            dispatched = dispatch_collaboration_event(
                event["event_id"], tasks, prompt_sender, db_path=db_path)
            results.append({"task_id": to_id, **dispatched})
        except Exception as exc:
            results.append({"task_id": to_id, "status": "failed",
                            "error": type(exc).__name__})
    return results


def _verification_snapshot(clone_path):
    """Return the version of EVAL_DONE visible at a dispatch boundary."""
    if not clone_path:
        return None
    path = Path(clone_path) / ".herdr-loop" / "EVAL_DONE.json"
    try:
        raw = path.read_bytes()
        snapshot = json.loads(raw.decode("utf-8"))
        return {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "mtime_ns": path.stat().st_mtime_ns,
            "iteration": snapshot.get("iteration"),
            "completed_at": snapshot.get("completed_at"),
        }
    except Exception:
        return None


def _verification_evidence_is_new(dispatch, test_evidence):
    """Reject the exact EVAL_DONE snapshot that predates VERIFY dispatch."""
    payload = dispatch.get("payload") or {}
    baseline = payload.get("evidence_baseline")
    started_at = float(payload.get("dispatch_started_at") or dispatch.get("timestamp") or 0)
    current_sha = test_evidence.get("snapshot_sha256")
    if not current_sha:
        return False
    if baseline and current_sha == baseline.get("sha256"):
        return False
    completed_at = test_evidence.get("snapshot_completed_at")
    if started_at:
        if completed_at is None:
            return False
        try:
            if float(completed_at) <= started_at:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _intervention_execution_evidence(task, intervention, store):
    """Find durable evidence for this exact Intervention, never by status alone."""
    intervention_id = intervention.get("intervention_id")
    action = intervention.get("action")
    task_id = intervention.get("task_id")
    run_id = intervention.get("run_id")
    fresh = store.get_task(task_id) if store is not None else None
    fresh = fresh or task
    if store is None:
        return None
    if action == "RETRY":
        for event in store.list_events(
            task_id=task_id, event_type="retry_dispatched", limit=200, desc=True,
        ):
            payload = event.get("payload") or {}
            if (
                event.get("run_id") == run_id
                and payload.get("intervention_id") == intervention_id
                and payload.get("action") == action
            ):
                return {
                    "action": action,
                    "previous_status": fresh.get("status"),
                    "new_status": fresh.get("status"),
                    "attempt_count": payload.get("attempt_count"),
                    "retry_dispatched": True,
                    "already_applied": True,
                    "execution_evidence": True,
                }
        return None
    for event in store.list_events(task_id=task_id, event_type="verification_dispatched", limit=200, desc=True):
        payload = event.get("payload") or {}
        if (
            event.get("run_id") == run_id
            and payload.get("intervention_id") == intervention_id
            and payload.get("action") == action
        ):
            return {
                "action": action,
                "previous_status": fresh.get("status"),
                "new_status": fresh.get("status"),
                "verification_dispatched": True,
                "verification_requested": True,
                "verification_pending": True,
                "already_applied": True,
                "execution_evidence": True,
            }
    return None


def _latest_verification_dispatch(task, store=None):
    store = store or _get_store()
    from herdr.trajectory import run_id_for_task
    rows = store.list_events(
        task_id=task.get("task_id"), event_type="verification_dispatched",
        source="supervisor", limit=100, desc=True,
    )
    expected_run = run_id_for_task(task)
    for event in rows:
        if event.get("run_id") == expected_run:
            return event
    return None


def _latest_action_dispatch_intent(task, store, action, intervention_id=None):
    store = store or _get_store()
    from herdr.trajectory import run_id_for_task
    event_type = {
        "VERIFY": "verification_dispatch_intent",
        "RETRY": "retry_dispatch_intent",
    }.get(action, f"{action.lower()}_dispatch_intent")
    rows = store.list_events(
        task_id=task.get("task_id"),
        event_type=event_type,
        source="supervisor", limit=100, desc=True,
    )
    expected_run = run_id_for_task(task)
    for event in rows:
        payload = event.get("payload") or {}
        if (
            event.get("run_id") == expected_run
            and payload.get("action") == action
            and (intervention_id is None or payload.get("intervention_id") == intervention_id)
        ):
            return event
    return None


def _latest_verification_dispatch_intent(task, store=None, intervention_id=None):
    return _latest_action_dispatch_intent(task, store, "VERIFY", intervention_id)


def _latest_retry_dispatch(task, store=None):
    store = store or _get_store()
    from herdr.trajectory import run_id_for_task
    expected_run = run_id_for_task(task)
    for event in store.list_events(
        task_id=task.get("task_id"), event_type="retry_dispatched",
        source="supervisor", limit=100, desc=True,
    ):
        if event.get("run_id") == expected_run:
            return event
    return None


def _retry_execution_complete_for_rework(task, store=None):
    """Old deliverables cannot heal a task while a new RETRY lacks dispatch."""
    store = store or _get_store()
    try:
        from herdr.trajectory import run_id_for_task
        rows = store.list_interventions(
            run_id=run_id_for_task(task), task_id=task.get("task_id"),
        )
        active = [
            row for row in rows
            if row.get("action") == "RETRY" and row.get("status") != "superseded"
        ]
        latest_agent_done = max(
            (
                float(entry.get("timestamp") or 0)
                for entry in (task.get("status_history") or [])
                if isinstance(entry, dict) and entry.get("to") == "agent_done"
            ),
            default=0.0,
        )
        active = [
            row for row in active
            if float(row.get("requested_at") or 0) >= latest_agent_done
        ]
        if not active:
            return True
        latest = max(active, key=lambda row: float(row.get("requested_at") or 0))
        if latest.get("status") in ("requested", "running", "failed"):
            dispatch = _latest_retry_dispatch(task, store)
            return bool(
                dispatch
                and (dispatch.get("payload") or {}).get("intervention_id")
                == latest.get("intervention_id")
            )
        return True
    except Exception:
        return False


def _verification_execution_complete_for_rework(task, store=None):
    """Old deliverables cannot heal a Task while a new VERIFY lacks evidence."""
    store = store or _get_store()
    try:
        from herdr.trajectory import run_id_for_task
        verify_rows = store.list_interventions(
            run_id=run_id_for_task(task),
            task_id=task.get("task_id"),
        )
        active_verify = [
            row for row in verify_rows
            if row.get("action") == "VERIFY"
            and row.get("status") != "superseded"
        ]
        latest_agent_done = max(
            (
                float(entry.get("timestamp") or 0)
                for entry in (task.get("status_history") or [])
                if isinstance(entry, dict) and entry.get("to") == "agent_done"
            ),
            default=0.0,
        )
        active_verify = [
            row for row in active_verify
            if float(row.get("requested_at") or 0) >= latest_agent_done
        ]
        if not active_verify:
            return True
        latest_verify = max(
            active_verify,
            key=lambda row: float(row.get("requested_at") or 0),
        ) if active_verify else None
        dispatch = _latest_verification_dispatch(task, store)
        dispatch_intervention_id = (
            (dispatch.get("payload") or {}).get("intervention_id")
            if dispatch is not None else None
        )
        if latest_verify and latest_verify.get("status") in ("requested", "running", "failed"):
            if dispatch_intervention_id != latest_verify.get("intervention_id"):
                return False
        if dispatch is None:
            latest = max(
                active_verify,
                key=lambda row: float(row.get("requested_at") or 0),
            )
            return latest.get("status") not in ("requested", "running", "failed")
        intervention_id = (dispatch.get("payload") or {}).get("intervention_id")
        for event in store.list_events(
            task_id=task.get("task_id"), event_type="verification_completed",
            source="trajectory", limit=100, desc=True,
        ):
            if event.get("run_id") != run_id_for_task(task):
                continue
            payload = event.get("payload") or {}
            verification = payload.get("verification") or {}
            if (
                verification.get("intervention_id") == intervention_id
                and float(event.get("timestamp") or 0) >= float(dispatch.get("timestamp") or 0)
            ):
                return True
        return False
    except Exception:
        return False


_INTERVENTION_EXECUTION_OWNER = f"controller:{os.getpid()}:{uuid.uuid4()}"


def _execute_supervisor_intervention(task, decision, action_handler, store=None):
    """Claim and execute one durable action, retaining legacy handler support."""
    intervention = decision.get("intervention") if isinstance(decision, dict) else None
    if not intervention:
        return action_handler(task, decision)
    store = store or _get_store()
    intervention_id = intervention.get("intervention_id")
    current = store.get_intervention(intervention_id)
    if current is None:
        raise RuntimeError("canonical intervention disappeared")
    if current.get("status") in ("completed", "failed", "superseded"):
        if current.get("status") == "failed":
            raise RuntimeError("intervention already failed")
        return current.get("result") or {"already_applied": True}
    claimed = store.claim_intervention(
        intervention_id,
        execution_owner=_INTERVENTION_EXECUTION_OWNER,
        recover_running=bool(decision.get("_recovery")),
    )
    if claimed is None:
        return {"already_claimed": True}
    owner = claimed.get("execution_owner")
    try:
        evidence = _intervention_execution_evidence(task, claimed, store)
        if evidence is not None:
            result = evidence
        elif action_handler in (_supervisor_retry, _supervisor_verify):
            result = action_handler(task, decision, store=store) or {}
        else:
            result = action_handler(task, decision) or {}
        store.complete_intervention(intervention_id, result, execution_owner=owner)
        return result
    except Exception as exc:
        from herdr.supervisor.state import redact_text
        error = getattr(exc, "intervention_error", None) or {
            "type": type(exc).__name__, "message": redact_text(str(exc)[:500]),
        }
        try:
            store.fail_intervention(intervention_id, error, execution_owner=owner)
        except ValueError:
            # A reclaimed owner or a concurrent terminal transition already
            # owns the durable outcome; do not mask the original action error.
            pass
        raise


def recover_pending_interventions(store=None, run_id=None, task_id=None):
    """Recover requested/running actions before a done redelivery can pass."""
    store = store or _get_store()
    recovered = []
    pending = store.list_interventions(
        run_id=run_id, task_id=task_id, statuses=["requested", "running"],
    )
    for item in pending:
        task = store.get_task(item.get("task_id"))
        if not task:
            store.fail_intervention(item["intervention_id"], {"code": "task_missing"})
            continue
        from herdr.trajectory import run_id_for_task
        if str(run_id_for_task(task)) != str(item.get("run_id")):
            store.fail_intervention(item["intervention_id"], {"code": "run_mismatch"})
            continue
        decision = dict(item)
        decision["intervention"] = item
        decision["_recovery"] = True
        if item.get("action") == "RETRY":
            result = _execute_supervisor_intervention(task, decision, _supervisor_retry, store=store)
        elif item.get("action") == "VERIFY":
            result = _execute_supervisor_intervention(task, decision, _supervisor_verify, store=store)
        else:
            store.fail_intervention(item["intervention_id"], {"code": "unsupported_action"})
            continue
        recovered.append({"intervention": item, "result": result})
    return recovered


def supervisor_continue_flow(result):
    """Whether the caller may run its default continuation (the done event).

    Fail-safe: no supervision result (disabled / skipped / crash) continues
    the original flow unchanged. An enforced intervention returns
    ``continue_flow=False`` so HAFlow orchestration owns the next step.
    """
    if not result:
        return True
    return bool(result.get("continue_flow", True))


def _supervisor_action_recovery_enabled():
    """Read the current kill switch before replaying durable actions."""
    try:
        if supervisor_harness is None:
            return False
        cfg = supervisor_harness.load_config()
        from herdr.supervisor.config import supervisor_enabled
        return bool(cfg.get("enforce") and supervisor_enabled(cfg))
    except Exception:
        return False


def emit_done_if_allowed(task, report_text=None):
    """The single gateway for agent_done -> coordinator done.

    Every Controller path that would announce a task as done goes through
    the supervisor checkpoint first; an enforced intervention blocks the
    default flow (the action handler owns the next step instead). When the
    checkpoint is rate-gated/skipped, a still-pending enforced intervention
    from the events ledger keeps blocking redelivery until a newer decision
    or a new agent_done transition supersedes it.

    The terminal Trajectory Observer checkpoint also lives here: listener,
    recovery and registry-redelivery paths all funnel through this gateway,
    and the observation must be queued before the task can advance.
    """
    observer_complete = threading.Event()
    supervisor_complete = threading.Event()
    store = _get_store()
    try:
        from herdr.trajectory import run_id_for_task
        if hasattr(store, "list_interventions") and _supervisor_action_recovery_enabled():
            recovered = recover_pending_interventions(
                store=store, run_id=run_id_for_task(task), task_id=task.get("task_id"),
            )
            if recovered:
                return False
    except Exception as exc:
        print(f"[INTERVENTION RECOVERY UNAVAILABLE] task={task.get('task_id')}: {type(exc).__name__}")
        return False
    _observer_terminal_checkpoint(task, completion_event=observer_complete)
    _schedule_context_compact(
        task, wait_for=observer_complete, supervisor_done=supervisor_complete,
    )
    try:
        checkpoint = supervisor_checkpoint(task, "agent_done", report_text=report_text)
        if checkpoint is None:
            pending = None
            if supervisor_harness is not None:
                try:
                    pending = supervisor_harness.pending_intervention(task, store)
                except Exception as exc:
                    if hasattr(store, "list_interventions"):
                        print(
                            f"[INTERVENTION RECOVERY UNAVAILABLE] "
                            f"task={task.get('task_id')}: {type(exc).__name__}"
                        )
                        return False
                    pending = None
            if pending:
                _supervisor_log_pending(task, pending)
                return False
            enqueue_coordinator_event(task, "done")
            return True
        if not supervisor_continue_flow(checkpoint):
            return False
        enqueue_coordinator_event(task, "done")
        return True
    finally:
        supervisor_complete.set()


def _schedule_context_compact(task, wait_for=None, supervisor_done=None):
    """Schedule working-memory creation without joining or affecting done flow."""
    if not task:
        return False
    try:
        from herdr.context_compact import compact_run_best_effort
        from herdr.trajectory import run_id_for_task
        store = _get_store()
        target_task_id = str(task.get("task_id") or "")
        target_run_id = str(run_id_for_task(task))
        if not target_task_id or not target_run_id:
            return False

        def worker():
            try:
                if wait_for is not None:
                    wait_for.wait()
                if supervisor_done is not None:
                    supervisor_done.wait()
                from herdr import state_db
                fresh_task = state_db.get_task(
                    target_task_id, db_path=getattr(store, "db_path", None),
                )
                if not fresh_task or str(run_id_for_task(fresh_task)) != target_run_id:
                    print(f"[CONTEXT COMPACT SKIPPED] task={target_task_id}: task/run identity changed")
                    return
                provider = None
                try:
                    from herdr.observer.harness import get_provider
                    provider = get_provider()
                except Exception as provider_exc:
                    print(f"[CONTEXT COMPACT PROVIDER FALLBACK] task={task.get('task_id')}: {type(provider_exc).__name__}: {provider_exc}")
                compact_run_best_effort(
                    target_run_id, task=fresh_task, store=store, provider=provider,
                )
            except Exception as exc:  # defensive boundary isolation
                print(f"[CONTEXT COMPACT WORKER SKIPPED] task={task.get('task_id')}: {type(exc).__name__}: {exc}")

        thread = threading.Thread(
            target=worker,
            name=f"context-compact-{task.get('task_id', 'run')}",
            daemon=True,
        )
        thread.start()
        return True
    except Exception as exc:
        print(f"[CONTEXT COMPACT SKIPPED] task={task.get('task_id')}: {type(exc).__name__}: {exc}")
        return False


def _supervisor_log_pending(task, action):
    """Record/refresh the pending-intervention ledger entry (log/notify once)."""
    key = f"supervisor:{task.get('task_id')}"
    reason = f"action {action} pending; done flow blocked until resolved"
    episode = attention_get(key) or {}
    if episode.get("reason") == reason:
        return
    print(f"[SUPERVISOR PENDING] task={task.get('task_id')} {reason}")
    attention_note(key, task, "supervisor_pending", reason)
    _supervisor_notify(task, action, reason)


def redeliver_done_event(task, now=None):
    """Registry-watcher done redelivery: supervisor-gated and throttled.

    This is a primary production path (sentinel-driven completions only reach
    the coordinator through here), so it must use the same gateway as
    handle_event; an enforced intervention keeps the done event withheld.
    """
    task_id = task.get("task_id")
    key = f"{task_id}:done"
    now = time.time() if now is None else now
    with lock:
        if key in queued_events:
            return False
    if attention_blocks_retry(key, now):
        return False
    print(
        f"[REGISTRY WATCHER] "
        f"task={task_id} "
        f"status=agent_done -> supervisor-gated done event"
    )
    if not emit_done_if_allowed(task):
        return False
    if attention_get(key):
        attention_throttle(key, now=now)
    return True


def _observer_terminal_checkpoint(task, now=None, completion_event=None):
    """Terminal Trajectory Observer checkpoint for agent_done.

    Fail-safe by contract: any failure only logs and returns False, so the
    existing done flow is never blocked. The observation itself runs on the
    observer's daemon worker (async, non-blocking).
    """
    if observer_harness is None or not task:
        if completion_event is not None:
            completion_event.set()
        return False
    try:
        kwargs = {"store": _get_store(), "now": now}
        if completion_event is not None:
            kwargs["completion_event"] = completion_event
        try:
            submitted = observer_harness.submit_terminal_observation(task, **kwargs)
        except TypeError as exc:
            # Keep older test adapters/in-process integrations fail-safe while
            # the built-in harness uses the completion barrier.
            if completion_event is None or "completion_event" not in str(exc):
                raise
            kwargs.pop("completion_event", None)
            submitted = observer_harness.submit_terminal_observation(task, **kwargs)
            completion_event.set()
        if not submitted and completion_event is not None:
            completion_event.set()
        return submitted
    except Exception as exc:
        if completion_event is not None:
            completion_event.set()
        print(f"[OBSERVER TERMINAL CHECK ERROR] task={task.get('task_id')}: {exc}")
        return False


def supervisor_checkpoint(task, trigger, report_text=None, test_evidence=None, evidence_id=None, now=None):
    """Semantic Supervisor 观察点:Jev/Provider 只产生信号与 Policy 结论,
    状态推进全部走既有流程;整体 fail-safe,绝不影响任务主链路。

    返回 harness 结果(None=未监督或异常)。调用方必须用
    ``supervisor_continue_flow(result)`` 决定是否继续 done:
    被 enforce 拦截的 intervention 绝不再走默认完成流程。
    """
    if supervisor_harness is None or not task:
        return None
    try:
        fresh = get_task(task.get("task_id")) or task
        actions = {
            # Policy actions map onto existing flows only. Durable VERIFY/RETRY
            # claims are owned by the Controller wrapper, not the Supervisor.
            "RETRY": lambda t, d: _execute_supervisor_intervention(t, d, _supervisor_retry),
            "ESCALATE": lambda t, d: _supervisor_attention(t, d, "supervisor_escalate"),
            "VERIFY": lambda t, d: _execute_supervisor_intervention(t, d, _supervisor_verify),
            "PAUSE": lambda t, d: _supervisor_attention(t, d, "supervisor_pause"),
            "REROUTE": lambda t, d: _supervisor_attention(t, d, "supervisor_reroute"),
        }
        if report_text is not None:
            report_reader = lambda: report_text
        else:
            report_reader = lambda: _supervisor_pane_report(fresh)
        result = supervisor_harness.run_checkpoint(
            task=fresh,
            trigger=trigger,
            store=_get_store(),
            actions=actions,
            report_reader=report_reader,
            test_evidence=test_evidence,
            evidence_id=evidence_id,
            now=now,
            log=print,
        )
        if result and result.get("intercepted") and not result.get("handled"):
            action = (result.get("decision") or {}).get("action")
            attention_note(
                f"supervisor:{fresh.get('task_id')}",
                fresh,
                "supervisor_unhandled",
                f"intercepted action {action} without a handler; done flow blocked",
            )
        return result
    except Exception as e:
        print(f"[SUPERVISOR SKIPPED] task={task.get('task_id')}: {type(e).__name__}: {e}")
        return None


def check_task_tests_completed(task, store=None, now=None):
    """Continuous Evaluation Checkpoint: tests_completed.

    Deterministic trigger when a real test run produces new METRICS.json evidence.
    Does NOT complete tasks, does NOT disrupt working agents on intermediate failures,
    uses persisted evidence_id dedup (restart-safe) and RateGate throttling.
    """
    if supervisor_harness is None or not task:
        return None

    # 1. Kill switches — checked FIRST, before any file I/O (Fix 5).
    #    supervisor_enabled() verifies: enabled flag + provider flag + API key.
    cfg = supervisor_harness.load_config()
    from herdr.supervisor.config import supervisor_enabled
    if not supervisor_enabled(cfg):
        return None

    clone_path = task.get("clone_path")
    if not clone_path or not os.path.isdir(clone_path):
        return None

    # 2. Extract test evidence (gated by EVAL_DONE.json atomic sentinel)
    from herdr.supervisor import evidence as supervisor_evidence
    test_evidence = supervisor_evidence.extract_test_evidence(clone_path)
    if not test_evidence:
        return None

    # 3. Build deterministic evidence fingerprint
    evidence_id = supervisor_evidence.build_test_evidence_id(test_evidence)

    # 4. Dedup against persisted evaluation events (restart-safe).
    #    Fix 4: query specifically for supervisor_evaluation events from the
    #    semantic_supervisor source, so the dedup window is never squeezed out
    #    by unrelated events flooding the ledger.
    st = store if store is not None else _get_store()
    task_id = task.get("task_id")
    try:
        events = st.list_events(
            task_id=task_id,
            event_type="supervisor_evaluation",
            source="semantic_supervisor",
            desc=True,
        ) or []
    except Exception:
        events = []

    from herdr.supervisor.evaluation import latest_tests_completed_evidence_id
    latest_ev_id = latest_tests_completed_evidence_id(events)
    if latest_ev_id == evidence_id:
        return None

    # 5. RateGate check: can supervisor evaluate now?
    # If RateGate skips, we DEFER (do not evaluate now, but keep evidence un-evaluated
    # so it can be evaluated when RateGate interval clears).
    sup = supervisor_harness.get_supervisor(cfg)
    if sup is not None:
        skip_reason = sup.should_evaluate(task_id, "tests_completed", now=now)
        if skip_reason is not None:
            return None

    # 6. Materialize the compact verification receipt as immutable evidence.
    verification = {
        "type": "tests_completed",
        "passed": bool(test_evidence.get("converged", False)),
        "evidence_id": evidence_id,
        "passed_tests": test_evidence.get("passed_tests"),
        "total_tests": test_evidence.get("total_tests"),
        "failing_count": test_evidence.get("failing_count", 0),
        "lint_errors": test_evidence.get("lint_errors", 0),
        "type_errors": test_evidence.get("type_errors", 0),
        "composite_score": test_evidence.get("composite_score", 0.0),
    }
    dispatch = _latest_verification_dispatch(task, st)
    if dispatch is not None:
        if not _verification_evidence_is_new(dispatch, test_evidence):
            print(
                f"[VERIFY EVIDENCE DEFERRED] task={task_id}: "
                "EVAL_DONE predates current VERIFY dispatch"
            )
            return None
        dispatch_payload = dispatch.get("payload") or {}
        verification["intervention_id"] = dispatch_payload.get("intervention_id")
        verification["decision_id"] = dispatch_payload.get("decision_id")
    try:
        observation, _created = create_verification_observation_with_status(
            verification,
            run_id=task.get("run_id") or f"run_{task_id}",
            task_id=task_id,
            workflow_id=task.get("workflow_id"),
            store=ObservationStore(getattr(st, "db_path", None)),
        )
        verification["observation_id"] = observation.observation_id
        record_observation_created(
            task,
            observation,
            ledger=TrajectoryLedger(getattr(st, "db_path", None)),
        )
    except Exception as exc:
        print(f"[VERIFICATION OBSERVATION SKIPPED] task={task_id}: {type(exc).__name__}")

    # 7. Record deterministic tests_completed event
    try:
        st.record_event(
            "tests_completed",
            {
                "task_id": task_id,
                "workflow_id": task.get("workflow_id"),
                "iteration": test_evidence.get("iteration"),
                "evidence_id": evidence_id,
                "passed_tests": test_evidence.get("passed_tests"),
                "total_tests": test_evidence.get("total_tests"),
                "failing_count": test_evidence.get("failing_count", 0),
                "lint_errors": test_evidence.get("lint_errors", 0),
                "type_errors": test_evidence.get("type_errors", 0),
                "composite_score": test_evidence.get("composite_score", 0.0),
                "intervention_id": verification.get("intervention_id"),
                "decision_id": verification.get("decision_id"),
            },
            task_id=task_id,
            workflow_id=task.get("workflow_id"),
            node_id=task.get("node") or task.get("stage"),
            agent_id=task.get("agent"),
            source="herdr-controller",
        )
        receipt = record_trajectory_event(
            task,
            "verification_completed",
            ledger=TrajectoryLedger(getattr(st, "db_path", None)),
            verification=verification,
        )
        if not receipt:
            raise RuntimeError("verification_completed receipt was not persisted")
    except Exception as exc:
        print(f"[TESTS_COMPLETED EVENT ERROR] task={task_id}: {exc}")
        # Do not consume/deduplicate this evidence until the durable receipt
        # exists. The next watcher pass must be able to retry the receipt.
        return None

    # 7. Run supervisor checkpoint
    return supervisor_checkpoint(
        task,
        "tests_completed",
        test_evidence=test_evidence,
        evidence_id=evidence_id,
        now=now,
    )


def handle_event(task_id, agent_status):
    task = get_task(task_id)

    if not task:
        return

    current_status = task.get("status")

    print(
        f"[TASK] "
        f"id={task_id} "
        f"workflow={task.get('workflow_id')} "
        f"stage={task.get('stage', 'unknown')} "
        f"pane={task.get('pane_id', 'unknown')} "
        f"agent={task.get('agent', 'unknown')} "
        f"status={agent_status} "
        f"task_status={current_status}"
    )

    # 终态绝不能被 Agent 普通事件覆盖
    if current_status in (
        "completed",
        "failed"
    ):
        return

    if agent_status == "working":
        if current_status in (
            "dispatched",
            "blocked",
            "rework"
        ):
            set_task_status(
                task_id,
                "working"
            )
            # Collaboration accelerator: a working target ACKs its handoff.
            maybe_ack_on_working(task_id)

    elif agent_status == "idle":
        pane_id = task.get("pane_id")
        has_done_marker = False
        screen = None
        if pane_id:
            try:
                res = subprocess.run(
                    ["herdr", "pane", "read", pane_id, "--source", "visible"],
                    text=True,
                    capture_output=True,
                    timeout=3
                )
                screen = (res.stdout or "") + (res.stderr or "")
                if f"HERDR_TASK_DONE:{task_id}" in screen:
                    has_done_marker = True
            except Exception:
                pass

        if current_status == "working":
            # 契约驱动前置检验：若任务有明确 required_outputs 但尚未生成，且无显式 DONE 标记，
            # 说明 Agent 正在长推理或多阶段阅读中，暂缓判定为完成，防止提前触发 rework
            wf_id = task.get("workflow_id")
            node_id = task.get("node") or task.get("stage")
            req_outputs = []
            if wf_id and node_id:
                try:
                    wf_cfg = workflow_config_for(wf_id)
                    if wf_cfg:
                        node = find_node(wf_cfg, node_id)
                        if node:
                            req_outputs = node.get("required_outputs", [])
                except Exception:
                    pass

            if req_outputs and not has_done_marker:
                verdict_ready = gate_verdict_ready(task)
                if not check_task_deliverables_ready(task) and not verdict_ready:
                    print(
                        f"[COMPLETION DEFERRED] "
                        f"task={task_id} "
                        f"required outputs {req_outputs} not ready; "
                        f"treating idle as transient think time"
                    )
                    return

            if not has_done_marker and not os.environ.get("HERDR_CONTROLLER_TEST"):
                # 屏幕上无明确完成标记，进行短暂防抖二次确认，防止推理/长命令间歇抖动
                time.sleep(3)
                runtime_check = get_agent_runtime_status(pane_id) if pane_id else None
                if runtime_check == "working":
                    print(
                        f"[AGENT JITTER FILTERED] "
                        f"task={task_id} "
                        f"recovered to working from idle"
                    )
                    return

            if set_task_status(
                task_id,
                "agent_done"
            ):
                task = get_task(task_id)
                emit_done_if_allowed(task, report_text=screen)

        elif current_status == "rework":
            # 自愈修复：若任务处于 rework 状态，当 Agent 输出 DONE 标记或产物已落盘就绪时，
            # 自动推进至 agent_done，彻底避免孤儿停滞！
            if (has_done_marker or check_task_deliverables_ready(task)) and \
                    _verification_execution_complete_for_rework(task) and \
                    _retry_execution_complete_for_rework(task):
                print(
                    f"[REWORK HEALED] "
                    f"task={task_id} "
                    f"deliverables verified, advancing rework -> agent_done"
                )
                if set_task_status(task_id, "agent_done"):
                    task = get_task(task_id)
                    emit_done_if_allowed(task, report_text=screen)

    elif agent_status == "blocked":
        if current_status in (
            "working",
            "dispatched",
            "rework"
        ):
            if set_task_status(
                task_id,
                "blocked"
            ):
                task = get_task(task_id)

                enqueue_coordinator_event(
                    task,
                    blocked_event_type(task)
                )

    elif agent_status == "done":
        if current_status in ("working", "rework"):
            if current_status == "rework" and (
                not check_task_deliverables_ready(task)
                or not _verification_execution_complete_for_rework(task)
                or not _retry_execution_complete_for_rework(task)
            ):
                return
            if set_task_status(
                task_id,
                "agent_done"
            ):
                task = get_task(task_id)
                emit_done_if_allowed(task)


# ============================================================
# Crash recovery / startup reconciliation
# ============================================================

def get_agent_runtime_status(pane_id):
    try:
        output = subprocess.check_output(
            [
                "herdr",
                "agent",
                "get",
                pane_id
            ],
            text=True
        )

        data = json.loads(output)

        return (
            data["result"]["agent"]
            .get("agent_status", "unknown")
        )

    except Exception as e:
        print(
            f"[RECOVERY STATUS ERROR] "
            f"pane={pane_id}: {e}"
        )
        return None


def reconcile_task_state(task_id):
    task = get_task(task_id)

    if not task:
        return

    current = task.get("status")

    if current in (
        "completed",
        "committed",
        "integrated",
        "cleanup_ready",
        "cleaned",
        "failed"
    ):
        return

    # Registry 已经知道 Agent 执行结束，
    # 但 Controller 可能在通知总指挥前重启。
    if current == "agent_done":
        print(
            f"[RECOVERY] "
            f"task={task_id} "
            f"registry=agent_done "
            f"→ restore done event"
        )

        emit_done_if_allowed(task)
        return

    pane_id = task.get("pane_id")

    if not pane_id:
        return

    runtime = get_agent_runtime_status(
        pane_id
    )

    if runtime is None:
        return

    print(
        f"[RECOVERY] "
        f"task={task_id} "
        f"registry={current} "
        f"agent={runtime}"
    )

    # --------------------------------
    # Agent 当前正在运行
    # --------------------------------
    if runtime == "working":
        if current in (
            "dispatched",
            "blocked",
            "rework"
        ):
            set_task_status(
                task_id,
                "working"
            )
        return

    # --------------------------------
    # Agent 当前 blocked
    # --------------------------------
    if runtime == "blocked":
        if current in (
            "dispatched",
            "working",
            "rework"
        ):
            if not set_task_status(
                task_id,
                "blocked"
            ):
                return

        task = get_task(task_id)

        if task and task.get("status") == "blocked":
            enqueue_coordinator_event(
                task,
                blocked_event_type(task)
            )

        return

    # --------------------------------
    # Agent 已经 done，但 Controller
    # 错过了 working/done 事件
    # --------------------------------
    if runtime == "done":
        current = get_task(task_id).get(
            "status"
        )

        if current == "dispatched":
            if not set_task_status(
                task_id,
                "working"
            ):
                return

            current = "working"

        elif current == "blocked":
            if not set_task_status(
                task_id,
                "working"
            ):
                return

            current = "working"

        elif current == "rework":
            # 只有当产物已经真实就绪时，才允许从 rework 恢复为 agent_done；
            # 否则必须等待新派发启动后实际进入 working。
            if (
                check_task_deliverables_ready(task)
                and _verification_execution_complete_for_rework(task)
                and _retry_execution_complete_for_rework(task)
            ):
                print(
                    f"[RECOVERY REWORK HEALED] "
                    f"task={task_id} deliverables detected -> restore agent_done"
                )
                if set_task_status(task_id, "agent_done"):
                    task = get_task(task_id)
                    if task and task.get("status") == "agent_done":
                        emit_done_if_allowed(task)
            return

        if current == "working":
            if not set_task_status(
                task_id,
                "agent_done"
            ):
                return

        task = get_task(task_id)

        if task and task.get("status") == "agent_done":
            emit_done_if_allowed(task)

        return

    # Agent 曾经进入 working/rework，随后 Controller 重启时发现已经 idle
    if runtime == "idle":
        if current == "working":
            if not set_task_status(
                task_id,
                "agent_done"
            ):
                return

            task = get_task(task_id)

            if task and task.get("status") == "agent_done":
                emit_done_if_allowed(task)
        elif current == "rework":
            if (
                check_task_deliverables_ready(task)
                and _verification_execution_complete_for_rework(task)
                and _retry_execution_complete_for_rework(task)
            ):
                print(
                    f"[RECOVERY REWORK HEALED] "
                    f"task={task_id} idle + deliverables detected -> restore agent_done"
                )
                if set_task_status(task_id, "agent_done"):
                    task = get_task(task_id)
                    if task and task.get("status") == "agent_done":
                        emit_done_if_allowed(task)

        return

    # dispatched -> idle 不自动推断完成；
    # unknown 也不自动推断。
    print(
        f"[RECOVERY NOOP] "
        f"task={task_id} "
        f"agent={runtime}"
    )



# ============================================================
# Per-task Herdr subscriptions
# ============================================================

def listen_task(task_id):
    task = get_task(task_id)

    if not task:
        return

    pane_id = task["pane_id"]

    sock = socket.socket(
        socket.AF_UNIX,
        socket.SOCK_STREAM
    )

    try:
        sock.connect(SOCKET_PATH)

        with lock:
            task_sockets[task_id] = sock

        request = {
            "id": f"task-{task_id}",
            "method": "events.subscribe",
            "params": {
                "subscriptions": [
                    {
                        "type":
                        "pane.agent_status_changed",
                        "pane_id": pane_id
                    }
                ]
            }
        }

        sock.sendall(
            (json.dumps(request) + "\n").encode()
        )

        file = sock.makefile("r")

        first = file.readline()

        if first:
            # 订阅响应可能是错误(如目标 pane 已不存在),必须显式失败,
            # 交由 registry_watcher 的退避逻辑处理,禁止伪装成已订阅。
            try:
                payload = json.loads(first)
            except Exception:
                payload = None

            if isinstance(payload, dict) and payload.get("error"):
                raise RuntimeError(
                    str(payload["error"].get("message") or payload["error"])
                )

            print(
                f"[SUBSCRIBED] "
                f"task={task_id} "
                f"pane={pane_id}"
            )

            # Controller 重启后立即核对
            # Registry 与 Agent 当前真实状态。
            reconcile_task_state(
                task_id
            )

        for line in file:
            if not line:
                break

            event = json.loads(line)

            if (
                event.get("event")
                != "pane.agent_status_changed"
            ):
                continue

            status = (
                event["data"]
                .get(
                    "agent_status",
                    "unknown"
                )
            )

            handle_event(
                task_id,
                status
            )

    except Exception as e:
        print(
            f"[LISTENER ERROR] "
            f"task={task_id}: {e}"
        )

    finally:
        with lock:
            task_sockets.pop(
                task_id,
                None
            )
            listeners.pop(
                task_id,
                None
            )

        try:
            sock.close()
        except Exception:
            pass


def start_task_listener(task_id):
    thread = threading.Thread(
        target=listen_task,
        args=(task_id,),
        daemon=True
    )

    with lock:
        listeners[task_id] = thread

    thread.start()


def stop_task_listener(task_id):
    with lock:
        sock = task_sockets.pop(
            task_id,
            None
        )

    if sock:
        try:
            sock.shutdown(
                socket.SHUT_RDWR
            )
        except Exception:
            pass

        try:
            sock.close()
        except Exception:
            pass

        print(
            f"[UNSUBSCRIBED] "
            f"task={task_id}"
        )


# ============================================================
# Awake guard (长跑 workflow 防休眠)
# ============================================================

_awake_guard_proc = None


def sync_awake_guard(active_workflows):
    """有活跃 workflow 时持有 caffeinate,避免机器休眠冻结整条流水线。

    controller 进程退出或 workflow 清零时自动释放;HERDR_AWAKE_GUARD=0 关闭。
    """
    global _awake_guard_proc

    disabled = os.environ.get("HERDR_AWAKE_GUARD", "1").strip().lower() in (
        "0",
        "false",
        "off",
        "no",
    )

    want = (
        not disabled
        and bool(active_workflows)
        and sys.platform == "darwin"
        and shutil.which("caffeinate") is not None
    )

    if want:
        if _awake_guard_proc is None or _awake_guard_proc.poll() is not None:
            try:
                _awake_guard_proc = subprocess.Popen(
                    ["caffeinate", "-i", "-s", "-w", str(os.getpid())],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                print(
                    f"[AWAKE GUARD] caffeinate started "
                    f"workflows={len(active_workflows)}"
                )
            except Exception as exc:
                print(f"[AWAKE GUARD ERROR] {exc}")
                _awake_guard_proc = None
        return

    if _awake_guard_proc is not None:
        try:
            _awake_guard_proc.terminate()
        except Exception:
            pass
        _awake_guard_proc = None
        print("[AWAKE GUARD] caffeinate released")


# ============================================================
# Registry watcher
# ============================================================

def registry_watcher():
    active_statuses = {
        "dispatched",
        "working",
        "blocked",
        "agent_done",
        "rework",
    }

    last_advance_check = 0

    while True:
        try:
            now = time.time()
            if now - last_advance_check >= 2:
                last_advance_check = now
                check_all_workflows_stage_advance()
                sync_awake_guard(active_registered_workflows())

            tasks = load_tasks()
            task_ids_now = set()

            for task in tasks:
                task_id = task["task_id"]
                status = task.get("status")
                task_ids_now.add(task_id)

                if status in (
                    "completed",
                    "failed",
                    "superseded"
                ) or workflow_closed(task.get("workflow_id")):
                    with lock:
                        running = (
                            task_id
                            in task_sockets
                        )

                    if running:
                        stop_task_listener(
                            task_id
                        )

                    _listener_backoff.pop(task_id, None)
                    _listener_giveup_logged.discard(task_id)
                    attention_clear(f"{task_id}:done")
                    attention_clear(f"{task_id}:blocked")
                    attention_clear(f"{task_id}:attention")

                    # 基础设施失败(投递熔断/进程崩溃)自动作废补派,
                    # 不让整个节点空等人工 relaunch。
                    if status == "failed" and not workflow_closed(
                        task.get("workflow_id")
                    ):
                        try:
                            recover_infra_failed_tasks(
                                task.get("workflow_id"), tasks
                            )
                        except Exception as exc:
                            print(f"[AUTO RECOVER ERROR] {exc}")

                    # completed + git 的终化重试必须在这里驱动:
                    # 该分支随即 continue,走不到后方的重试块。
                    if status == "completed":
                        try:
                            _check_finalize_retry(task, status, now)
                        except (OSError, RuntimeError, ValueError, KeyError,
                                subprocess.SubprocessError) as exc:
                            print(f"[FINALIZE RETRY ERROR] {exc}")

                    continue

                # ---- listener 订阅:指数退避 + 封顶(僵尸 pane 护栏) ----
                if status in active_statuses:
                    with lock:
                        already = (
                            task_id
                            in listeners
                        )

                    if not already:
                        signature = (status, task.get("updated_at"))
                        slot = _listener_backoff.get(task_id)
                        if not slot or slot.get("signature") != signature:
                            # 任务真实状态变化即重置退避,避免误封活工位。
                            slot = {
                                "attempts": 0,
                                "next_allowed_at": 0.0,
                                "signature": signature,
                            }
                            _listener_backoff[task_id] = slot

                        if now >= slot.get("next_allowed_at", 0.0):
                            attempts = slot.get("attempts", 0) + 1
                            slot["attempts"] = attempts
                            if attempts >= liveness.subscribe_max_attempts():
                                slot["next_allowed_at"] = (
                                    now + liveness.subscribe_backoff_cap()
                                )
                                if task_id not in _listener_giveup_logged:
                                    _listener_giveup_logged.add(task_id)
                                    print(
                                        f"[LISTENER GIVEUP] "
                                        f"task={task_id} "
                                        f"pane={task.get('pane_id')} "
                                        f"attempts={attempts} -> "
                                        f"retry every {int(liveness.subscribe_backoff_cap())}s"
                                    )
                            else:
                                slot["next_allowed_at"] = (
                                    now + liveness.backoff_delay(attempts)
                                )
                            start_task_listener(
                                task_id
                            )

                # ---- tests_completed 连续评估检查 (仅对活跃工作中的任务) ----
                # dispatched 排除：Agent 尚未开始工作，不会有真实测试结果；
                # rework 保留：Agent 仍在 inner loop 迭代，和 working 等价对待。
                if status in ("working", "rework"):
                    try:
                        check_task_tests_completed(task, store=_get_store(), now=now)
                    except Exception as exc:
                        print(f"[TESTS_COMPLETED CHECK ERROR] task={task_id}: {exc}")

                # ---- Trajectory Observer: 旁路观察,非阻塞投递 ----
                # 观察线程与主轮询物理隔离;超时/模型失败/内部异常均不影响
                # Task/Workflow/Runtime 与事件推进。
                if (
                    status in ("working", "rework", "blocked")
                    and observer_harness is not None
                ):
                    try:
                        observer_harness.submit_observation(
                            task, store=_get_store(), now=now
                        )
                    except Exception as exc:
                        print(f"[OBSERVER CHECK ERROR] task={task_id}: {exc}")

                # ---- done 事件投递:受 attention episode 节流 + 监督网关把关 ----
                # terminal Observer checkpoint 已挂在统一 Done Gateway
                # (emit_done_if_allowed) 内,覆盖 listener/recovery/redelivery 全部路径。
                if status == "agent_done":
                    redeliver_done_event(task, now=now)

                # ---- blocked 事件:此前投递失败会被静默吞掉,这里补投递护栏 ----
                if status == "blocked":
                    key = f"{task_id}:blocked"
                    updated = float(task.get("updated_at") or 0)
                    with lock:
                        already_queued = key in queued_events
                    if (
                        updated
                        and now - updated >= liveness.attention_grace()
                        and not already_queued
                        and not attention_blocks_retry(key, now)
                    ):
                        print(
                            f"[REGISTRY WATCHER] "
                            f"task={task_id} "
                            f"status=blocked -> notify coordinator"
                        )
                        enqueue_coordinator_event(
                            task, blocked_event_type(task)
                        )
                        if not attention_get(key):
                            attention_note(
                                key,
                                task,
                                "blocked",
                                reason="blocked_unhandled",
                                attempts=1,
                            )
                        attention_throttle(key, now=now)
                else:
                    attention_clear(f"{task_id}:blocked")

                # ---- interrupted / paused 死区:超时未裁决即升级给总指挥 ----
                if status in ("interrupted", "paused"):
                    key = f"{task_id}:attention"
                    updated = float(task.get("updated_at") or 0)
                    with lock:
                        already_queued = key in queued_events
                    if (
                        updated
                        and now - updated >= liveness.attention_grace()
                        and not already_queued
                        and not attention_blocks_retry(key, now)
                    ):
                        print(
                            f"[REGISTRY WATCHER] "
                            f"task={task_id} "
                            f"status={status} -> attention event to coordinator"
                        )
                        enqueue_coordinator_event(task, "attention")
                        if not attention_get(key):
                            attention_note(
                                key,
                                task,
                                "attention",
                                reason="status_requires_attention",
                                attempts=1,
                            )
                        attention_throttle(key, now=now)
                else:
                    attention_clear(f"{task_id}:attention")

                # ---- committed 滞留 / completed 但 commit 门禁瞬时失败(flaky):
                #      integrate 失败或 commit gate 抖动时按退避自动重试,
                #      达到上限后停止并升级人工(避免无限重试风暴)。
                #      completed 分支在上方的 terminal 分支内已驱动(随即
                #      continue,走不到这里);这里覆盖 committed 等终化中状态。
                _check_finalize_retry(task, status, now)

                if status == "rework" and not workflow_closed(task.get("workflow_id")):
                    pane_id = task.get("pane_id")
                    if pane_id:
                        runtime = get_agent_runtime_status(pane_id)
                        if (
                            runtime in ("idle", "done")
                            and check_task_deliverables_ready(task)
                            and _verification_execution_complete_for_rework(task)
                            and _retry_execution_complete_for_rework(task)
                        ):
                            print(
                                f"[REWORK WATCHDOG HEAL] "
                                f"task={task_id} deliverables detected and agent is {runtime} -> advance to agent_done"
                            )
                            if set_task_status(task_id, "agent_done"):
                                task = get_task(task_id)
                                emit_done_if_allowed(task)

            for stale_id in list(_listener_backoff.keys()):
                if stale_id not in task_ids_now:
                    _listener_backoff.pop(stale_id, None)
                    _listener_giveup_logged.discard(stale_id)

        except Exception as e:
            print(
                f"[REGISTRY ERROR] {e}"
            )

        time.sleep(1)


# ============================================================
# Main
# ============================================================

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


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print(
            "\n[CONTROLLER V12] stopped"
        )
