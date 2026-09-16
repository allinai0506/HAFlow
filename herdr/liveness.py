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

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ============================================================
# Defaults (env-overridable for tests / operations)
# ============================================================

DEFAULT_COORDINATOR_DELIVERY_SLA = 900.0
DEFAULT_STAGE_ADVANCE_SLA = 600.0
DEFAULT_ATTENTION_RETRY_INTERVAL = 600.0
DEFAULT_ATTENTION_GRACE = 120.0
DEFAULT_COORDINATOR_DECISION_TIMEOUT = 180.0
DEFAULT_TASK_STALL_AFTER = 1800.0
DEFAULT_SUBSCRIBE_BACKOFF_BASE = 2.0
DEFAULT_SUBSCRIBE_BACKOFF_CAP = 300.0
DEFAULT_SUBSCRIBE_MAX_ATTEMPTS = 8

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
    """

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cache: Optional[Dict[str, Dict[str, Any]]] = None

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if self._cache is not None:
            return self._cache
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        episodes = data.get("episodes") if isinstance(data, dict) else None
        self._cache = dict(episodes) if isinstance(episodes, dict) else {}
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

    def all(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return dict(self._load())

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            episode = self._load().get(key)
            return dict(episode) if episode else None

    def upsert(self, key: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            episodes = self._load()
            episode = dict(episodes.get(key) or {})
            episode.update(fields or {})
            episodes[key] = episode
            self._save()
            return dict(episode)

    def clear(self, key: str) -> bool:
        with self._lock:
            episodes = self._load()
            if key not in episodes:
                return False
            episodes.pop(key, None)
            self._save()
            return True


def blocks_retry(store: EpisodeStore, key: str, now: Optional[float] = None) -> bool:
    """True while an open episode throttles the next retry."""
    episode = store.get(key)
    if not episode:
        return False
    return float(episode.get("next_retry_at") or 0) > (time.time() if now is None else now)


def throttle_retry(
    store: EpisodeStore,
    key: str,
    interval: Optional[float] = None,
    now: Optional[float] = None,
) -> None:
    """Push the next allowed retry of an already-open episode into the future."""
    current = time.time() if now is None else now
    store.upsert(key, {"next_retry_at": current + (attention_retry_interval() if interval is None else interval)})
