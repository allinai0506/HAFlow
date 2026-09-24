"""Control-plane liveness policy: bounded waits, retry backoff, registry hygiene.

HAFlow 控制面铁律（本模块是唯一策略来源）:

1. 对任何 Actor（总指挥 / 工位 / 订阅 / 阶段推进）的等待都必须有 SLA 上限，
   严禁无条件 while True 阻塞控制线程；
2. 每次重试必须指数退避并封顶，严禁 2s 级无限重试风暴；
3. 夹具 / 临时目录里的僵尸 Workflow 绝不允许进入调度 sweep；
4. 停滞与投递失败必须落盘为 attention episode（可审计、可升级、可恢复）。

本模块只承载纯逻辑与 JSON 事件簿，不依赖 controller / sentinel 运行时。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

# ============================================================
# Defaults (env-overridable for tests / operations)
# ============================================================

DEFAULT_COORDINATOR_DELIVERY_SLA = 900.0
DEFAULT_STAGE_ADVANCE_SLA = 600.0
DEFAULT_ATTENTION_RETRY_INTERVAL = 600.0
DEFAULT_ATTENTION_GRACE = 120.0
DEFAULT_COORDINATOR_DECISION_TIMEOUT = 180.0
DEFAULT_TASK_STALL_AFTER = 1800.0
# dispatched 投递熔断:任务派发后超过该秒数仍未进入 working,视为投递死亡。
# 正常派发 working 只需数秒;600s 内无 ack 的 pane 几乎不可能自愈
# (wf-nexusarchive-0917-01 曾有 challenger 卡 dispatched 2h50m 的先例)。
DEFAULT_DISPATCH_DELIVERY_SLA = 600.0
DEFAULT_SUBSCRIBE_BACKOFF_BASE = 2.0
DEFAULT_SUBSCRIBE_BACKOFF_CAP = 300.0
DEFAULT_SUBSCRIBE_MAX_ATTEMPTS = 8

# 自动补派判定用终态集合:这些状态的任务不会再有推进,节点可视为"已空"。
RECOVERY_TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "committed",
        "integrated",
        "cleanup_ready",
        "cleaned",
        "failed",
        "superseded",
    }
)

REPLACEMENT_SUFFIX_RE = re.compile(r"-r(\d+)$")

# 需要停滞监控的任务状态：所有"未到达终态但也没有推进"的状态。
STALL_WATCH_STATUSES = frozenset(
    {
        "pending",
        "dispatched",
        "working",
        "blocked",
        "agent_done",
        "rework",
        "paused",
        "interrupted",
    }
)

# 夹具/临时工作流指纹：pytest 夹具目录与显式 /tmp 路径。
# 注意:不把整个 /var/folders 视为外来——macOS 的正常临时目录也在其中,
# 真正需要拦截的是测试夹具(pytest-*)与 /tmp 残留,以及已删除的定义文件。
TEMP_PATH_MARKERS = (
    "/tmp/",
)
FIXTURE_NAME_MARKERS = ("pytest-",)

# Task agent kind -> herdr integration 名称（用于集成健康检查）。
AGENT_INTEGRATION_ALIASES = {
    "agy": "antigravity-cli",
    "antigravity": "antigravity-cli",
    "antigravity-cli": "antigravity-cli",
    "qwen-code": "qwen",
    "open-code": "opencode",
}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def coordinator_delivery_sla() -> float:
    return _env_float(
        "HERDR_COORDINATOR_DELIVERY_SLA", DEFAULT_COORDINATOR_DELIVERY_SLA
    )


def stage_advance_sla() -> float:
    return _env_float("HERDR_STAGE_ADVANCE_SLA", DEFAULT_STAGE_ADVANCE_SLA)


def attention_retry_interval() -> float:
    return _env_float("HERDR_ATTENTION_RETRY_INTERVAL", DEFAULT_ATTENTION_RETRY_INTERVAL)


def attention_grace() -> float:
    return _env_float("HERDR_ATTENTION_GRACE", DEFAULT_ATTENTION_GRACE)


def coordinator_decision_timeout() -> float:
    """总指挥 prompt 返回后,等待判定落盘的真实回合预算。

    30s 级别窗口会把"回合尾部仍在写状态"误判为无决策,触发一次多余的
    10 分钟级重试回合;默认对齐一次总指挥回合的常见尾部耗时。
    """
    return _env_float(
        "HERDR_COORDINATOR_DECISION_TIMEOUT",
        DEFAULT_COORDINATOR_DECISION_TIMEOUT,
    )


def task_stall_after() -> float:
    return _env_float("HERDR_TASK_STALL_AFTER", DEFAULT_TASK_STALL_AFTER)


def dispatch_delivery_sla() -> float:
    return _env_float("HERDR_DISPATCH_DELIVERY_SLA", DEFAULT_DISPATCH_DELIVERY_SLA)


def subscribe_backoff_base() -> float:
    return _env_float("HERDR_SUBSCRIBE_BACKOFF_BASE", DEFAULT_SUBSCRIBE_BACKOFF_BASE)


def subscribe_backoff_cap() -> float:
    return _env_float("HERDR_SUBSCRIBE_BACKOFF_CAP", DEFAULT_SUBSCRIBE_BACKOFF_CAP)


def subscribe_max_attempts() -> int:
    return int(
        _env_float("HERDR_SUBSCRIBE_MAX_ATTEMPTS", float(DEFAULT_SUBSCRIBE_MAX_ATTEMPTS))
    )


# ============================================================
# Pure helpers
# ============================================================


def backoff_delay(attempt: int, base: Optional[float] = None, cap: Optional[float] = None) -> float:
    """Exponential backoff: attempt 1 -> base, doubling, capped at cap."""
    base = subscribe_backoff_base() if base is None else base
    cap = subscribe_backoff_cap() if cap is None else cap
    step = max(1, int(attempt))
    delay = base * (2 ** (step - 1))
    return float(min(cap, delay))


def is_foreign_workflow_file(path: Optional[str]) -> bool:
    """True for fixture/temp workflow definitions that must stay out of the runtime.

    A workflow is foreign when its definition file is missing, points into a
    system temp directory, or lives under a pytest fixture directory. Real
    factory workflows always persist their definition under the runtime root.
    """
    if not path:
        return True

    normalized = str(path)
    if any(marker in normalized for marker in FIXTURE_NAME_MARKERS):
        return True
    if any(marker in normalized for marker in TEMP_PATH_MARKERS):
        return True

    return not os.path.exists(normalized)


def workflow_is_foreign(workflow: Optional[Dict[str, Any]]) -> bool:
    """夹具/临时/无项目空壳 workflow 判定（仅基于正向证据，避免误杀遗留记录）。

    - 定义了 workflow_file 的记录：路径指向 pytest 夹具、/tmp 或已删除 → 外来；
    - 未定义 workflow_file 的记录：只有在完全没有项目上下文（空壳）时才算外来；
    - 空 dict（无记录）不在此判定，交由既有 fail-closed 逻辑处理。
    """
    if not workflow:
        return False

    path = workflow.get("workflow_file")
    if path:
        return is_foreign_workflow_file(path)

    return not workflow.get("project_id")


def integration_name(agent_kind: Optional[str]) -> Optional[str]:
    if not agent_kind:
        return None
    kind = str(agent_kind).strip().lower()
    return AGENT_INTEGRATION_ALIASES.get(kind, kind)


def parse_integration_status(text: str) -> Dict[str, str]:
    """Parse `herdr integration status` output into {kind: status-line}."""
    statuses: Dict[str, str] = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        kind, _, rest = line.partition(":")
        kind = kind.strip().lower()
        rest = rest.strip()
        if not kind or not rest:
            continue
        statuses[kind] = rest
    return statuses


def integration_gaps(status_text: str, agent_kinds: Iterable[Optional[str]]) -> List[Dict[str, str]]:
    """Return integration gaps for the agent kinds actually in use.

    Only lifecycle-capable integrations matter for state authority; every
    installed-but-missing integration still degrades session restore, so any
    "not installed" status for a kind in use is reported once.
    """
    statuses = parse_integration_status(status_text)
    gaps: List[Dict[str, str]] = []
    seen = set()

    for kind in agent_kinds:
        integration = integration_name(kind)
        if not integration or integration in seen:
            continue
        seen.add(integration)
        status = statuses.get(integration)
        if status and "not installed" in status.lower():
            gaps.append({"agent": str(kind), "integration": integration, "status": status})

    return gaps


def evaluate_task_stalls(
    tasks: Iterable[Dict[str, Any]],
    episodes: Dict[str, Dict[str, Any]],
    now: float,
    stall_after: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Detect tasks that stopped making progress; dedupe by task episode.

    Episode identity is (task_id, updated_at): a real state transition resets
    the episode and a new stall on the same task raises a fresh alert.
    Returns (new_alerts, episodes_out) without performing I/O.
    """
    threshold = task_stall_after() if stall_after is None else stall_after
    episodes_out: Dict[str, Dict[str, Any]] = dict(episodes or {})
    alerts: List[Dict[str, Any]] = []
    seen = set()

    for task in tasks or []:
        task_id = task.get("task_id")
        if not task_id:
            continue
        seen.add(task_id)

        status = task.get("status")
        if status not in STALL_WATCH_STATUSES:
            episodes_out.pop(task_id, None)
            continue

        updated = float(task.get("updated_at") or task.get("last_activity_at") or 0)
        if not updated:
            continue

        idle_seconds = now - updated
        if idle_seconds < threshold:
            # 有新鲜推进即撤销停滞 episode,避免旧告警污染下一轮。
            episodes_out.pop(task_id, None)
            continue

        existing = episodes_out.get(task_id)
        if existing and float(existing.get("updated_at") or 0) == updated:
            continue

        alert = {
            "task_id": task_id,
            "workflow_id": task.get("workflow_id"),
            "status": status,
            "updated_at": updated,
            "idle_seconds": int(idle_seconds),
            "alerted_at": now,
        }
        episodes_out[task_id] = alert
        alerts.append(dict(alert))

    for task_id in list(episodes_out.keys()):
        if task_id not in seen:
            episodes_out.pop(task_id, None)

    return alerts, episodes_out


def evaluate_dispatch_fuse(
    tasks: Iterable[Dict[str, Any]],
    episodes: Dict[str, Dict[str, Any]],
    now: float,
    sla: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Detect dispatched tasks whose delivery never landed (no working ack).

    Only `dispatched` tasks are watched: any other status (including pending,
    which legitimately queues before dispatch) resets its episode. Episode
    identity is (task_id, updated_at), so a controller redispatch starts a
    fresh episode -- but the `requeues` counter is inherited, letting the
    caller cap repeated failovers of the same never-started task.
    Returns (new_breaches, episodes_out) without performing I/O.
    """
    threshold = dispatch_delivery_sla() if sla is None else sla
    episodes_out: Dict[str, Dict[str, Any]] = dict(episodes or {})
    breaches: List[Dict[str, Any]] = []
    seen = set()

    for task in tasks or []:
        task_id = task.get("task_id")
        if not task_id:
            continue
        seen.add(task_id)

        if task.get("status") != "dispatched":
            episodes_out.pop(task_id, None)
            continue

        updated = float(task.get("updated_at") or task.get("last_activity_at") or 0)
        if not updated:
            continue

        waited = now - updated
        if waited < threshold:
            episodes_out.pop(task_id, None)
            continue

        previous = episodes_out.get(task_id) or {}
        if previous and float(previous.get("updated_at") or 0) == updated:
            continue

        breach = {
            "task_id": task_id,
            "workflow_id": task.get("workflow_id"),
            "node": task.get("node") or task.get("stage"),
            "status": "dispatched",
            "agent": task.get("agent"),
            "pane_id": task.get("pane_id"),
            "updated_at": updated,
            "waited_seconds": int(waited),
            "idle_seconds": int(waited),
            "sla_seconds": int(threshold),
            "requeues": int(previous.get("requeues", 0)),
            "alerted_at": now,
        }
        episodes_out[task_id] = breach
        breaches.append(dict(breach))

    for task_id in list(episodes_out.keys()):
        if task_id not in seen:
            episodes_out.pop(task_id, None)

    return breaches, episodes_out


def task_lineage_root(task_id) -> str:
    """Replacement lineage root: x, x-r2, x-r3 all share root x."""
    text = str(task_id or "")
    match = REPLACEMENT_SUFFIX_RE.search(text)
    return text[: match.start()] if match else text


def _failure_reasons(task: Dict[str, Any]) -> List[str]:
    return [
        str(entry.get("reason") or "")
        for entry in (task.get("status_history") or [])
        if entry.get("to") == "failed"
    ]


def select_infra_failures_for_recovery(
    tasks: Iterable[Dict[str, Any]],
    reasons: Iterable[str],
    max_attempts: int = 2,
) -> List[Dict[str, Any]]:
    """Pick `failed` tasks eligible for automatic supersede + re-dispatch.

    A task is eligible when all of the following hold:
    - its node has no live tasks (nothing left to race with; the node would
      otherwise stall forever, as seen in wf-nexusarchive-0917-01),
    - the failure reason belongs to the infrastructure `reasons` set
      (delivery fuse / process crash -- quality verdicts are NOT recovered),
    - its lineage (base id, ignoring -rN replacements) has fewer than
      `max_attempts` infrastructure failures so far,
    - it was not already superseded.
    Returns task dicts sorted by creation time (stable redisptach order).
    """
    reason_set = {str(r) for r in (reasons or []) if r}
    tasks = list(tasks or [])
    by_node: Dict[str, List[Dict[str, Any]]] = {}

    for task in tasks:
        node = task.get("node") or task.get("stage")
        if not node:
            continue
        by_node.setdefault(str(node), []).append(task)

    selected = []
    for node_tasks in by_node.values():
        if any(
            t.get("status") not in RECOVERY_TERMINAL_STATUSES
            for t in node_tasks
        ):
            continue

        attempts: Dict[str, int] = {}
        for task in node_tasks:
            if any(r in reason_set for r in _failure_reasons(task)):
                root = task_lineage_root(task.get("task_id"))
                attempts[root] = attempts.get(root, 0) + 1

        for task in node_tasks:
            if task.get("status") != "failed":
                continue
            if task.get("superseded_by"):
                continue
            if not any(r in reason_set for r in _failure_reasons(task)):
                continue
            root = task_lineage_root(task.get("task_id"))
            if attempts.get(root, 0) >= max_attempts:
                continue
            selected.append(dict(task))

    selected.sort(
        key=lambda item: (item.get("created_at") or 0, str(item.get("task_id") or ""))
    )
    return selected


class BoundedWait:
    """Track a bounded waiting episode (SLA) for actor interactions."""

    __slots__ = ("_sla", "_started", "_clock")

    def __init__(self, sla: Optional[float] = None, clock=time.time):
        self._sla = coordinator_delivery_sla() if sla is None else float(sla)
        self._clock = clock
        self._started = clock()

    def elapsed(self, now: Optional[float] = None) -> float:
        return (self._clock() if now is None else now) - self._started

    def expired(self, now: Optional[float] = None) -> bool:
        return self.elapsed(now) >= self._sla


class EpisodeStore:
    """JSON-backed attention/stall episode bookkeeping.

    Atomic writes + process-wide lock keep concurrent controller threads from
    clobbering each other. Cross-process ownership is split by file path:
    controller owns attention.json, sentinel owns stalls.json.

    The in-memory cache is invalidated by file identity (mtime_ns + size):
    a CLI recovery command such as ``herdr-task clear-escalation`` runs in
    its own process, so a long-lived controller must observe its writes on
    the next sweep instead of serving a stale snapshot forever.

    Read-modify-write cycles (upsert/clear) hold an exclusive advisory
    lock on a sidecar ``<file>.lock`` across load+mutate+save: ``os.replace``
    alone only makes single writes atomic, it cannot stop two processes
    from interleaving read-modify-write and resurrecting deleted episodes
    (or dropping fresh ones). On platforms without ``fcntl`` the lock is a
    best-effort no-op and only the thread lock applies.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cache: Optional[Dict[str, Dict[str, Any]]] = None
        self._loaded_sig = None
        self._lock_path = str(self.path) + ".lock"

    @contextlib.contextmanager
    def _file_lock(self, *, strict=False):
        """Exclusive cross-process guard for one read-modify-write cycle."""
        if fcntl is None:
            if strict:
                raise RuntimeError("episode file locking is unavailable")
            yield None
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            if strict:
                raise RuntimeError("episode file lock could not be opened")
            yield None
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield fd
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)

    @contextlib.contextmanager
    def transaction(self):
        """Expose one locked read-modify-write episode transaction.

        The yielded mapping is the authoritative in-file episode table.  It is
        saved only when the block exits normally, so a crashed/raising caller
        cannot publish a half-applied SLA claim.
        """
        with self._lock, self._file_lock(strict=True):
            episodes = self._load(strict=True)
            yield episodes
            self._save()

    def _stat_sig(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _load(self, *, strict=False) -> Dict[str, Dict[str, Any]]:
        sig = self._stat_sig()
        if self._cache is not None and sig == self._loaded_sig:
            return self._cache
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (OSError, TypeError, ValueError) as exc:
            if strict:
                raise RuntimeError("episode ledger could not be read") from exc
            data = {}
        episodes = data.get("episodes") if isinstance(data, dict) else None
        if not isinstance(episodes, dict):
            if strict:
                raise RuntimeError("episode ledger has an invalid shape")
            episodes = {}
        self._cache = dict(episodes)
        self._loaded_sig = sig
        return self._cache

    def _save(self) -> None:
        payload = {"episodes": self._cache or {}}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
        self._loaded_sig = self._stat_sig()

    def all(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return dict(self._load())

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            episode = self._load().get(key)
            return dict(episode) if episode else None

    def upsert(self, key: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock, self._file_lock():
            episodes = self._load()
            episode = dict(episodes.get(key) or {})
            episode.update(fields or {})
            episodes[key] = episode
            self._save()
            return dict(episode)

    def clear(self, key: str) -> bool:
        with self._lock, self._file_lock():
            episodes = self._load()
            if key not in episodes:
                return False
            episodes.pop(key, None)
            self._save()
            return True

    def mutate(self, key: str, updater) -> dict[str, Any] | None:
        """Atomically read-modify-write one episode.

        ``updater`` receives a copy of the current episode and returns the
        fields to merge.  Returning ``None`` leaves the episode untouched.
        The file lock spans the read, callback, and replace, so independent
        Controller processes cannot lose an SLA clock update or action claim.
        """
        with self._lock, self._file_lock():
            episodes = self._load()
            current = dict(episodes.get(key) or {})
            fields = updater(current)
            if fields is None:
                return dict(current) if current else None
            if not isinstance(fields, dict):
                raise TypeError("episode updater must return a dict or None")
            if "__replace__" in fields:
                replacement = fields["__replace__"]
                if not isinstance(replacement, dict):
                    raise TypeError("episode replacement must be a dict")
                current = dict(replacement)
            else:
                current.update(fields)
            episodes[key] = current
            self._save()
            return dict(current)

    def claim_action(
        self,
        key: str,
        *,
        claim_id: str,
        action: str,
        now: float,
        lease_seconds: float,
        expected: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Atomically claim one external action for an episode.

        The claim is a short durable lease, not a second status machine.  It
        prevents two Controller processes from sending the same bounded
        prompt; an expired lease is reclaimable after a process crash.
        """
        with self._lock, self._file_lock():
            episodes = self._load()
            current = dict(episodes.get(key) or {})
            if not current:
                return None
            for field, expected_value in (expected or {}).items():
                actual = current.get(field)
                # Older episodes may omit a zero-valued counter.  Treat that
                # legacy shape as its documented zero default, but never
                # coerce an unknown non-empty value.
                if actual != expected_value and (
                    field in current or expected_value not in (0, False)
                ):
                    if not (
                        field == "repush_state"
                        and field not in current
                        and expected_value in {"pending", "failed"}
                    ):
                        return None
            previous = current.get("action_claim")
            if isinstance(previous, dict):
                try:
                    claimed_at = float(previous.get("claimed_at") or 0)
                    if now - claimed_at < float(lease_seconds):
                        return None
                except (TypeError, ValueError):
                    # An unreadable lease is stale rather than a permission to
                    # duplicate a prompt.
                    return None
            current["action_claim"] = {
                "claim_id": str(claim_id),
                "action": str(action),
                "claimed_at": float(now),
                "lease_until": float(now) + float(lease_seconds),
            }
            episodes[key] = current
            self._save()
            return dict(current)

    def complete_action(
        self,
        key: str,
        *,
        claim_id: str,
        updates: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Finalize a claim only when its owner still owns the lease."""
        with self._lock, self._file_lock():
            episodes = self._load()
            current = dict(episodes.get(key) or {})
            claim = current.get("action_claim")
            if not isinstance(claim, dict) or claim.get("claim_id") != claim_id:
                return None
            current.update(dict(updates or {}))
            current.pop("action_claim", None)
            episodes[key] = current
            self._save()
            return dict(current)


def blocks_retry(store: EpisodeStore, key: str, now: float | None = None) -> bool:
    """True while an open episode throttles the next retry."""
    episode = store.get(key)
    if not episode:
        return False
    return float(episode.get("next_retry_at") or 0) > (time.time() if now is None else now)


def throttle_retry(
    store: EpisodeStore,
    key: str,
    interval: float | None = None,
    now: float | None = None,
) -> None:
    """Push the next allowed retry of an already-open episode into the future."""
    current = time.time() if now is None else now
    store.upsert(key, {"next_retry_at": current + (attention_retry_interval() if interval is None else interval)})
