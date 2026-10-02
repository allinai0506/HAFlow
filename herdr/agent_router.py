#!/opt/homebrew/bin/python3
import json
import fcntl
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

HOME = Path.home()
ROOT = HOME / ".herdr-controller"
POOLS_FILE = ROOT / "agent-pools.json"
PROJECTS_FILE = ROOT / "projects.json"
WORKFLOWS_FILE = ROOT / "workflows.json"
TASKS_FILE = ROOT / "tasks.json"
RESERVATIONS_FILE = ROOT / "agent-reservations.json"
ROUTER_LOCK_FILE = ROOT / "agent-router.lock"

try:
    from .projects import workflow_config_for
    from .workflow import find_node
except ImportError:
    from herdr.projects import workflow_config_for
    from herdr.workflow import find_node

DEFAULT_ALLOWED = ["opencode", "codex", "qodercli", "claude", "agy", "pi", "grok", "kimi"]

DEFAULT_STAGE_PREFERENCES = {
    "requirements": ["claude", "qodercli", "opencode", "codex", "agy", "pi", "grok", "kimi"],
    "plan": ["claude", "qodercli", "codex", "opencode", "agy", "pi", "grok", "kimi"],
    "implementation": ["opencode", "codex", "qodercli", "agy", "claude", "pi", "grok", "kimi"],
    "test": ["codex", "opencode", "qodercli", "claude", "agy", "pi", "grok", "kimi"],
    "review": ["claude", "codex", "qodercli", "opencode", "agy", "pi", "grok", "kimi"],
    "wrapup": ["claude", "qodercli", "opencode", "codex", "agy", "pi", "grok", "kimi"],
}

DEFAULT_TASK_TYPE_PREFERENCES = {
    "feat": ["opencode", "codex", "qodercli", "agy", "claude", "pi", "grok", "kimi"],
    "fix": ["opencode", "codex", "qodercli", "agy", "claude", "pi", "grok", "kimi"],
    "refactor": ["codex", "opencode", "qodercli", "claude", "agy", "pi", "grok", "kimi"],
    "docs": ["claude", "qodercli", "opencode", "codex", "agy", "pi", "grok", "kimi"],
    "test": ["codex", "opencode", "qodercli", "claude", "agy", "pi", "grok", "kimi"],
    "chore": ["codex", "opencode", "qodercli", "claude", "agy", "pi", "grok", "kimi"],
    "perf": ["codex", "opencode", "qodercli", "claude", "agy", "pi", "grok", "kimi"],
    "ci": ["codex", "opencode", "qodercli", "claude", "agy", "pi", "grok", "kimi"],
}

# Preflight 快照保鲜期:超过该秒数的 healthy_agents 名单不再被信任为
# "当前健康"(Agent 凭证/配额会在工作流运行中途过期,如 pi AUTH_REQUIRED)。
# 过期后路由回落到 allowed - disabled - unhealthy,而不是把任务派给一个
# 数小时前健康、现在可能已死的 Agent。缺失/不可解析的时间戳视为新鲜,
# 保持 legacy 记录与既有单 Agent 优雅回退语义不变。
DEFAULT_PREFLIGHT_TTL_SECONDS = 1800.0


def preflight_ttl_seconds() -> float:
    raw = os.environ.get("HERDR_PREFLIGHT_TTL")
    if raw is None:
        return DEFAULT_PREFLIGHT_TTL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_PREFLIGHT_TTL_SECONDS
    return value if value > 0 else DEFAULT_PREFLIGHT_TTL_SECONDS


def _preflight_checked_ts(record) -> float | None:
    raw = (record or {}).get("preflight_checked_at")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def preflight_snapshot_fresh(record, now=None, current_identity=None) -> bool:
    identities = (record or {}).get("preflight_identities")
    if identities is not None:
        if not isinstance(identities, dict) or not identities:
            return False
        for agent, identity in identities.items():
            current = current_identity.get(agent) if current_identity is not None else None
            if current_identity is not None and current is None:
                return False
            if not preflight_snapshot_fresh(
                {"preflight_identity": identity, "preflight_checked_at": record.get("preflight_checked_at")},
                now=now, current_identity=current,
            ):
                return False
    identity = (record or {}).get("preflight_identity")
    if identity is not None:
        if not isinstance(identity, dict) or not identity.get("verifiable"):
            return False
        if current_identity is None:
            from herdr.deep_preflight import preflight_identity
            from herdr.agent_binary import AGENT_BINARIES, resolve_binary
            agent = identity.get("agent")
            if not agent or not identity.get("project_root"):
                return False
            current_identity = preflight_identity(
                agent, resolve_binary(AGENT_BINARIES.get(agent, agent)),
                identity["project_root"], identity.get("launch_mode", "noninteractive"),
            )
        if not current_identity.get("verifiable") or identity.get("fingerprint") != current_identity.get("fingerprint"):
            return False
    ts = _preflight_checked_ts(record)
    if ts is None:
        return identity is None and identities is None
    current = time.time() if now is None else now
    return 0 <= current - ts <= preflight_ttl_seconds()

def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)

def default_pool():
    return {
        "allowed_agents": list(DEFAULT_ALLOWED),
        "disabled_agents": [],
        "stage_preferences": {k: list(v) for k, v in DEFAULT_STAGE_PREFERENCES.items()},
        "task_type_preferences": {k: list(v) for k, v in DEFAULT_TASK_TYPE_PREFERENCES.items()},
    }

def load_pools():
    return _load(POOLS_FILE, {"projects": {}})

def save_pools(data):
    _save(POOLS_FILE, data)

def ensure_pool_for_project(project_id):
    data = load_pools()
    projects = data.setdefault("projects", {})
    if project_id not in projects:
        projects[project_id] = default_pool()
        save_pools(data)
    return projects[project_id]

def _get_store():
    try:
        from .state_store import get_state_store
    except ImportError:
        from herdr.state_store import get_state_store
    if os.environ.get("HERDR_STATE_DB"):
        return get_state_store(Path(os.environ["HERDR_STATE_DB"]))
    t_file = globals().get("TASKS_FILE") or os.environ.get("TASKS_FILE")
    if t_file and Path(t_file) != (ROOT / "tasks.json"):
        p = Path(t_file)
        db_path = p.parent / "state.db" if p.name == "tasks.json" else p.with_suffix(".db")
        if db_path.parent.exists():
            return get_state_store(db_path=db_path)
    wf_file = globals().get("WORKFLOWS_FILE") or os.environ.get("WORKFLOWS_FILE")
    if wf_file and Path(wf_file) != (ROOT / "workflows.json"):
        p = Path(wf_file)
        db_path = p.parent / "state.db" if p.name == "workflows.json" else p.with_suffix(".db")
        if db_path.parent.exists():
            return get_state_store(db_path=db_path)
    if os.environ.get("TASKS_FILE"):
        p = Path(os.environ["TASKS_FILE"])
        db_path = p.parent / "state.db" if p.name == "tasks.json" else p.with_suffix(".db")
        if db_path.parent.exists():
            return get_state_store(db_path=db_path)
    if os.environ.get("WORKFLOWS_FILE"):
        p = Path(os.environ["WORKFLOWS_FILE"])
        db_path = p.parent / "state.db" if p.name == "workflows.json" else p.with_suffix(".db")
        if db_path.parent.exists():
            return get_state_store(db_path=db_path)
    return get_state_store()


def workflow_record(workflow_id):
    store = _get_store()
    wf = store.get_workflow(workflow_id)
    if wf and not (wf.get("status") == "unknown" and not wf.get("project_id")):
        return wf
    return {}


def set_workflow_agent_override(workflow_id, agent):
    store = _get_store()
    record = store.get_workflow(workflow_id)
    if not record:
        return

    record["agent_override"] = agent or "auto"
    store.save_workflow(record)

    # 投影必须经 StateStore 同步(环境变量感知)，严禁直写硬编码路径。
    try:
        from .state_store import sync_workflows_projection
    except ImportError:
        from herdr.state_store import sync_workflows_projection
    sync_workflows_projection(store=store)


def _clean_reservations(data, ttl=300):
    now = time.time()
    store = _get_store()
    tasks = store.list_tasks()

    registered = {
        t.get("task_id")
        for t in tasks
        if t.get("task_id")
    }

    cleaned = {}
    for task_id, item in data.get("reservations", {}).items():
        if task_id in registered:
            continue
        created_at = float(item.get("created_at", 0) or 0)
        if created_at and (now - created_at) <= ttl:
            cleaned[task_id] = item

    data["reservations"] = cleaned
    return data


def release_agent_reservation(task_id):
    ROUTER_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

    with open(ROUTER_LOCK_FILE, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

        data = _load(
            RESERVATIONS_FILE,
            {"reservations": {}},
        )
        data = _clean_reservations(data)
        data.setdefault("reservations", {}).pop(task_id, None)
        _save(RESERVATIONS_FILE, data)

        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _active_agent_loads(project_id):
    store = _get_store()
    tasks = store.list_tasks()

    active = {
        "pending", "dispatched", "working", "blocked", "agent_done",
        "rework",
    }
    loads = {}
    for task in tasks:
        if task.get("project_id") != project_id:
            continue
        if task.get("status") not in active:
            continue
        agent = task.get("agent")
        if agent:
            loads[agent] = loads.get(agent, 0) + 1
    return loads


def _candidate_order(pool, stage, task_type, node_policy=None):
    node_policy = node_policy or {}
    preferred = list(node_policy.get("preferred", []))
    excluded = set(node_policy.get("exclude", []))

    ordered = []
    for agent in [
        *preferred,
        *pool.get("stage_preferences", {}).get(stage, []),
        *pool.get("task_type_preferences", {}).get(task_type, []),
        *pool.get("allowed_agents", []),
    ]:
        if agent in excluded:
            continue
        if agent not in ordered:
            ordered.append(agent)
    return ordered


def _isolation_opt_out(node_policy=None):
    """Return (opt_out_enabled, reason) for FR-6.1 isolation bypass (R9).

    Enabled only when allow_reuse_implementation_agents (or legacy
    opt_out_isolation) is truthy AND a non-empty reuse reason is present.
    No approval workflow; the reason + event留痕 is the audit trail.
    """
    policy = node_policy or {}
    enabled = bool(
        policy.get("allow_reuse_implementation_agents")
        or policy.get("opt_out_isolation")
    )
    reason = str(
        policy.get("reuse_reason")
        or policy.get("opt_out_reason")
        or policy.get("reason")
        or ""
    ).strip()
    return enabled, reason


def _record_router_opt_out(
    workflow_id,
    stage,
    selected,
    reason,
    excluded,
    *,
    task_id="",
    run_id="",
):
    """Persist the opt-out audit or fail closed before reusing an agent."""
    try:
        store = _get_store()
        receipt = store.record_event(
            "router_opt_out_used",
            {
                "workflow_id": workflow_id,
                "run_id": run_id or "",
                "task_id": task_id or "",
                "stage": stage,
                "selected": selected or "",
                "reason": reason,
                "excluded": sorted(set(excluded or [])),
                "audit_required": True,
            },
            workflow_id=workflow_id,
            node_id=stage,
            task_id=task_id or None,
            run_id=run_id or None,
            source="agent-router",
        )
        if receipt is False:
            raise RuntimeError("router opt-out audit was not durably recorded")
        return True
    except (OSError, ValueError, RuntimeError, AttributeError, sqlite3.Error) as exc:
        raise RuntimeError(
            "FR-6 isolation opt-out audit persistence failed; refusing "
            f"agent reuse ({type(exc).__name__}: {exc})"
        ) from exc

def _choose_agent_impl(
    workflow_id,
    stage,
    task_type,
    requested="auto",
    reservation_key=None,
    run_id=None,
    decided_at=None,
):
    if workflow_id:
        record = workflow_record(workflow_id)
        project_id = record.get("project_id")
        if not project_id:
            raise RuntimeError(
                f"Workflow not found in authoritative StateStore: {workflow_id}"
            )
    else:
        # FR-6.3: no workflow context must never silently return the
        # implementation default. Explicit requests still pass through;
        # auto without context fails closed.
        if requested and requested != "auto":
            return requested, {
                "workflow_id": "",
                "stage": stage,
                "task_type": task_type,
                "candidates": [requested],
                "active_loads": {},
                "reserved_loads": {},
                "run_id": run_id or "",
                "task_id": reservation_key or "",
            }
        raise RuntimeError(
            "Agent selection requires explicit --agent when workflow_id is absent; "
            "refusing to default to 'opencode' (FR-6.3 fail-closed)"
        )

    node_policy = {}
    if workflow_id:
        wf_cfg = workflow_config_for(workflow_id)
        if wf_cfg:
            node = find_node(wf_cfg, stage)
            if node:
                node_policy = node.get("agent_policy", {})

    exclude_stages = (
        node_policy.get("exclude_stage_agents")
        or node_policy.get("disallow_from_stages")
        or []
    )
    if isinstance(exclude_stages, str):
        exclude_stages = [exclude_stages]
    # FR-6.1 default: test/review automatically exclude implementation
    # agents even without explicit node_policy (fail-closed by default).
    if not exclude_stages and stage in ("test", "review"):
        exclude_stages = ["implementation"]

    stage_used_agents = set()
    if workflow_id and exclude_stages:
        store = _get_store()
        tasks = store.list_tasks()
        target_stages = set(exclude_stages)
        for t in tasks:
            if t.get("workflow_id") == workflow_id:
                t_stage = t.get("node") or t.get("stage")
                if t_stage in target_stages and t.get("agent"):
                    stage_used_agents.add(t.get("agent"))

    pool = ensure_pool_for_project(project_id)
    allowed = set(pool.get("allowed_agents", []))
    disabled = set(pool.get("disabled_agents", []))
    workflow_override = record.get("agent_override", "auto")

    if workflow_override and workflow_override != "auto":
        selected = workflow_override
    elif requested and requested != "auto":
        selected = requested
    elif node_policy.get("fixed"):
        selected = node_policy["fixed"]
    else:
        selected = None

    healthy = set(record.get("healthy_agents", []))
    unhealthy_agents = set((record.get("unhealthy_agents") or {}).keys())
    snapshot_fresh = preflight_snapshot_fresh(record)

    if selected:
        if selected in stage_used_agents:
            _opt_out, _opt_reason = _isolation_opt_out(node_policy)
            if not (_opt_out and _opt_reason):
                raise RuntimeError(
                    f"Agent '{selected}' is prohibited for stage '{stage}' "
                    f"because it was used in stage(s): {', '.join(exclude_stages)} "
                    f"(excluded agents: {sorted(stage_used_agents)}). "
                    "Provide explicit opt-out with reason "
                    "(agent_policy.allow_reuse_implementation_agents=true + "
                    "reuse_reason) to bypass."
                )
        if selected not in allowed:
            raise RuntimeError(
                f"Agent '{selected}' is not allowed for project {project_id}"
            )
        if selected in disabled:
            raise RuntimeError(
                f"Agent '{selected}' is disabled for project {project_id}"
            )
        if healthy and selected not in healthy:
            status = record.get("unhealthy_agents", {}).get(
                selected,
                "NOT_READY",
            )
            raise RuntimeError(
                f"Agent '{selected}' failed Workflow Deep Preflight: {status}"
            )
        if selected in stage_used_agents:
            _opt_out, _opt_reason = _isolation_opt_out(node_policy)
            _record_router_opt_out(
                workflow_id,
                stage,
                selected,
                _opt_reason,
                stage_used_agents,
                task_id=reservation_key or "",
                run_id=run_id or "",
            )
        return selected, {
            "workflow_id": workflow_id or "",
            "stage": stage,
            "task_type": task_type,
            "candidates": [selected],
            "active_loads": {},
            "reserved_loads": {},
            "run_id": run_id or "",
            "task_id": reservation_key or "",
        }

    # Canary config probe (one small file read; missing/invalid means
    # disabled). Read outside the lock; only an enabled config makes the
    # locked section do any canary work. Any unexpected failure here is
    # also "disabled": canary config loading can never break routing.
    _canary = _load_canary_module()
    canary_cfg = None
    if _canary is not None:
        try:
            canary_cfg, _ = _canary.read_canary_config()
        except Exception:  # noqa: BLE001 -- canary fails open
            canary_cfg = None

    ROUTER_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

    with open(ROUTER_LOCK_FILE, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

        reservations = _load(
            RESERVATIONS_FILE,
            {"reservations": {}},
        )
        reservations = _clean_reservations(reservations)

        active_loads = _active_agent_loads(project_id)
        reserved_loads = {}

        for item in reservations.get("reservations", {}).values():
            if item.get("project_id") != project_id:
                continue
            agent = item.get("agent")
            if agent:
                reserved_loads[agent] = reserved_loads.get(agent, 0) + 1

        candidates = [
            agent
            for agent in _candidate_order(pool, stage, task_type, node_policy)
            if (
                agent in allowed
                and agent not in disabled
                # 已知不健康的 Agent 永不自动入选:冷启动(healthy 为空)时
                # 也不能把任务派给 AUTH_REQUIRED / TOKEN_EXHAUSTED 的 Agent。
                and agent not in unhealthy_agents
                # 新鲜快照才信任 healthy 名单做交集;过期快照只做减法
                # (unhealthy),不做"只允许当时健康的集合"的限制,避免把
                # 任务锁死在一个可能已全灭的旧集合里。
                and (
                    not snapshot_fresh
                    or not healthy
                    or agent in healthy
                )
            )
        ]

        if stage_used_agents:
            _opt_out, _opt_reason = _isolation_opt_out(node_policy)
            if _opt_out:
                if not _opt_reason:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                    raise RuntimeError(
                        f"Isolation opt-out for stage '{stage}' requires "
                        "a non-empty reuse_reason (R9 fail-closed)"
                    )
                # Defer the audit until ranking identifies the reused agent.
            else:
                filtered = [a for a in candidates if a not in stage_used_agents]
                if not filtered:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                    raise RuntimeError(
                        f"No available Agent for stage '{stage}': all candidates "
                        f"{candidates} were used in stage(s) "
                        f"{', '.join(exclude_stages)} "
                        f"(excluded: {sorted(stage_used_agents)}). "
                        "Isolation is fail-closed; provide explicit opt-out "
                        "(agent_policy.allow_reuse_implementation_agents=true + "
                        "reuse_reason) to bypass."
                    )
                candidates = filtered

        if not candidates:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            raise RuntimeError(
                f"No enabled Agent available for project {project_id}"
            )

        # Canary gate (Adaptive Router v2): planning happens inside the
        # lock because the diversion target must come from this exact
        # validated candidate list and the reservation must record the
        # final agent in the same critical section. Reads are bounded;
        # the gate is closed entirely unless the operator enabled it.
        canary_plan = None
        canary_plan_ctx = None
        canary_error = None
        if canary_cfg is not None:
            try:
                canary_plan = _canary.plan_canary(
                    config=canary_cfg,
                    candidates=candidates,
                    node=stage,
                    task_type=task_type,
                    task_id=reservation_key or "",
                    run_id=run_id or "",
                    db_path=getattr(_get_store(), "db_path", None),
                    decided_at=(
                        float(decided_at)
                        if decided_at is not None else time.time()
                    ),
                    active_loads=active_loads,
                    reserved_loads=reserved_loads,
                    exclude_run_id=run_id or None,
                )
            except Exception as exc:  # noqa: BLE001 -- canary fails open
                canary_error = f"{type(exc).__name__}: {exc}"
            if canary_plan is not None:
                canary_plan_ctx = {
                    "workflow_id": workflow_id or "",
                    "stage": stage,
                    "task_type": task_type,
                    "run_id": run_id or "",
                    "task_id": reservation_key or "",
                    "recommended": canary_plan.recommended,
                    "rankings": [dict(row) for row in canary_plan.rankings],
                    "gate": dict(canary_plan.gate),
                }

        ranked = sorted(
            enumerate(candidates),
            key=lambda item: (
                active_loads.get(item[1], 0)
                + reserved_loads.get(item[1], 0),
                item[0],
            ),
        )

        legacy_pick = ranked[0][1]
        selected = legacy_pick
        canary_persisted = False
        if canary_plan is not None and canary_plan.hash_divert \
                and canary_plan.recommended != legacy_pick:
            # No persisted canary decision, no canary execution: the
            # diversion fact must be durably recorded inside this
            # critical section BEFORE the diverted agent is committed
            # and reserved. A failed write means the legacy pick runs.
            if _persist_canary_diversion_in_lock(
                workflow_id,
                stage,
                task_type,
                reservation_key or "",
                run_id or "",
                legacy_pick,
                decided_at,
                canary_plan,
            ):
                selected = canary_plan.recommended
                canary_persisted = True
            else:
                canary_error = (
                    "canary decision persistence failed; "
                    "diverted back to legacy"
                )

        if selected in stage_used_agents:
            _opt_out, _opt_reason = _isolation_opt_out(node_policy)
            if _opt_out and _opt_reason:
                _record_router_opt_out(
                    workflow_id,
                    stage,
                    selected,
                    _opt_reason,
                    stage_used_agents,
                    task_id=reservation_key or "",
                    run_id=run_id or "",
                )

        if reservation_key:
            reservations.setdefault("reservations", {})[reservation_key] = {
                "project_id": project_id,
                "workflow_id": workflow_id,
                "stage": stage,
                "task_type": task_type,
                "agent": selected,
                "created_at": time.time(),
            }
            _save(RESERVATIONS_FILE, reservations)

        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    result_ctx = {
        "workflow_id": workflow_id or "",
        "stage": stage,
        "task_type": task_type,
        "candidates": list(candidates),
        "active_loads": dict(active_loads),
        "reserved_loads": dict(reserved_loads),
        "run_id": run_id or "",
        "task_id": reservation_key or "",
    }
    if canary_plan is not None:
        result_ctx["canary_plan"] = canary_plan_ctx
        result_ctx["canary_legacy_pick"] = legacy_pick
        result_ctx["canary_persisted"] = canary_persisted
    if canary_error:
        result_ctx["canary_error"] = canary_error
    return selected, result_ctx


def _record_shadow_best_effort(selected, shadow_ctx, decided_at):
    """Persist the Adaptive Router shadow recommendation (fail-open).

    Any failure is recorded as a route_decision_error event on a
    best-effort basis and never propagates: production routing always
    falls back to the legacy decision.
    """
    try:
        try:
            from . import adaptive_router as _adaptive
        except ImportError:  # pragma: no cover - script-style import fallback
            from herdr import adaptive_router as _adaptive
        store = _get_store()
        context = _adaptive.ShadowContext(
            workflow_id=shadow_ctx.get("workflow_id", ""),
            stage=shadow_ctx.get("stage", ""),
            task_type=shadow_ctx.get("task_type", ""),
            candidates=list(shadow_ctx.get("candidates", [])),
            active_loads=dict(shadow_ctx.get("active_loads", {})),
            reserved_loads=dict(shadow_ctx.get("reserved_loads", {})),
            run_id=shadow_ctx.get("run_id", ""),
            task_id=shadow_ctx.get("task_id", ""),
        )
        _adaptive.record_shadow_decision(
            store=store,
            decided_at=decided_at,
            actual_agent=selected,
            context=context,
        )
    except Exception as exc:
        ctx = shadow_ctx or {}
        try:
            store = _get_store()
            store.record_event(
                "route_decision_error",
                {
                    "mode": "shadow",
                    "workflow_id": ctx.get("workflow_id", ""),
                    "run_id": ctx.get("run_id", ""),
                    "task_id": ctx.get("task_id", ""),
                    "actual_agent": selected,
                    "algorithm_version": "adaptive-router-v1",
                    "error": f"{type(exc).__name__}: {exc}",
                    "created_at": decided_at,
                },
                workflow_id=ctx.get("workflow_id") or None,
                node_id=ctx.get("stage") or None,
                task_id=ctx.get("task_id") or None,
                agent_id=selected or None,
                source="adaptive-router-shadow",
                timestamp=decided_at,
                run_id=ctx.get("run_id") or None,
            )
        except Exception:
            pass


def _load_canary_module():
    """Import the canary gate module (standalone-import safe) or None."""
    try:
        from . import canary_router
    except ImportError:
        try:
            from herdr import canary_router
        except ImportError:
            return None
    return canary_router


def _build_canary_payload(selected, legacy_pick, decided_at, canary):
    """Build the canary route_decision payload (pure, no I/O).

    ``canary`` is the ctx["canary_plan"] dict ({recommended, rankings,
    gate}). ``diverted`` is derived from (selected != legacy_pick) so
    the persisted fact always matches what will really execute.
    """
    try:
        from . import adaptive_router as _adaptive
    except ImportError:  # pragma: no cover - script-style import fallback
        from herdr import adaptive_router as _adaptive
    gate = dict(canary.get("gate") or {})
    gate["would_divert"] = bool(canary.get("recommended") != legacy_pick)
    return _adaptive.build_canary_decision(
        workflow_id=str(canary.get("workflow_id") or ""),
        run_id=str(canary.get("run_id") or ""),
        task_id=str(canary.get("task_id") or ""),
        node=str(canary.get("stage") or ""),
        task_type=str(canary.get("task_type") or ""),
        actual_agent=selected,
        recommended_agent=str(canary.get("recommended") or ""),
        legacy_agent=str(legacy_pick or ""),
        diverted=bool(selected != legacy_pick),
        rankings=list(canary.get("rankings") or []),
        gate=gate,
        created_at=decided_at,
    )


def _persist_canary_diversion_in_lock(
    workflow_id,
    stage,
    task_type,
    task_id,
    run_id,
    legacy_pick,
    decided_at,
    plan,
):
    """Durably record the diversion decision BEFORE committing it.

    Returns True only when the route_decision(mode=canary, diverted)
    row is confirmed persisted (record_event neither raised nor
    reported a False receipt). Any failure keeps the execution on the
    legacy pick: no persisted canary decision, no canary execution.
    """
    try:
        try:
            from . import adaptive_router as _adaptive
        except ImportError:  # pragma: no cover - script-style import fallback
            from herdr import adaptive_router as _adaptive
        gate = dict(plan.gate)
        gate["would_divert"] = True
        payload = _adaptive.build_canary_decision(
            workflow_id=workflow_id or "",
            run_id=run_id,
            task_id=task_id,
            node=stage or "",
            task_type=task_type,
            actual_agent=plan.recommended,
            recommended_agent=plan.recommended,
            legacy_agent=legacy_pick,
            diverted=True,
            rankings=[dict(row) for row in plan.rankings],
            gate=gate,
            created_at=decided_at,
        )
        store = _get_store()
        receipt = store.record_event(
            "route_decision",
            payload,
            workflow_id=workflow_id or None,
            node_id=stage or None,
            task_id=task_id or None,
            agent_id=plan.recommended or None,
            source="adaptive-router-canary",
            timestamp=float(decided_at),
            run_id=run_id or None,
        )
        if receipt is False:
            return False
        return True
    except Exception:
        return False


def _record_canary_best_effort(selected, shadow_ctx, decided_at, canary):
    """Persist a non-diverted canary decision (fail-open bookkeeping).

    Only hash-miss / same-agent / reverted-to-legacy routings reach
    this: the execution itself is the legacy path, so a lost event can
    only shrink the legacy arm of the evaluation, never orphan a
    diverted run. Diverted decisions are persisted inside the routing
    critical section by ``_persist_canary_diversion_in_lock`` instead.
    """
    ctx = shadow_ctx or {}
    legacy_pick = str(ctx.get("canary_legacy_pick") or "")
    try:
        payload = _build_canary_payload(selected, legacy_pick, decided_at,
                                        canary)
        store = _get_store()
        receipt = store.record_event(
            "route_decision",
            payload,
            workflow_id=ctx.get("workflow_id") or None,
            node_id=ctx.get("stage") or None,
            task_id=ctx.get("task_id") or None,
            agent_id=selected or None,
            source="adaptive-router-canary",
            timestamp=float(decided_at),
            run_id=ctx.get("run_id") or None,
        )
        if receipt is False:
            raise RuntimeError("canary decision was not durably recorded")
    except Exception as exc:
        _record_canary_error_best_effort(
            ctx, decided_at, f"{type(exc).__name__}: {exc}", selected)


def _record_canary_error_best_effort(shadow_ctx, decided_at, error, selected):
    """Best-effort fail-open audit for the canary gate (never raises)."""
    ctx = shadow_ctx or {}
    try:
        store = _get_store()
        store.record_event(
            "route_decision_error",
            {
                "mode": "canary",
                "workflow_id": ctx.get("workflow_id", ""),
                "run_id": ctx.get("run_id", ""),
                "task_id": ctx.get("task_id", ""),
                "actual_agent": selected,
                "algorithm_version": "adaptive-router-v2-canary",
                "error": error,
                "created_at": decided_at,
            },
            workflow_id=ctx.get("workflow_id") or None,
            node_id=ctx.get("stage") or None,
            task_id=ctx.get("task_id") or None,
            agent_id=selected or None,
            source="adaptive-router-canary",
            timestamp=decided_at,
            run_id=ctx.get("run_id") or None,
        )
    except Exception:
        pass


def choose_agent(
    workflow_id,
    stage,
    task_type,
    requested="auto",
    reservation_key=None,
    run_id=None,
):
    """Select the production agent.

    Legacy semantics are unchanged. The Adaptive Router v2 canary may
    divert the auto-selection to the recommendation when the operator
    enabled it, the bucket is whitelisted and sufficient, and the
    deterministic hash puts this identity in the canary slice; any
    canary failure fails open to the legacy pick.
    """
    decided_at = time.time()
    selected, shadow_ctx = _choose_agent_impl(
        workflow_id,
        stage,
        task_type,
        requested=requested,
        reservation_key=reservation_key,
        run_id=run_id,
        decided_at=decided_at,
    )
    canary = shadow_ctx.get("canary_plan")
    if canary is not None:
        if shadow_ctx.get("canary_persisted"):
            # The diverted decision was already durably recorded inside
            # the routing critical section (no persisted canary
            # decision, no canary execution). Record nothing here.
            pass
        else:
            # Non-diverted canary routing (hash miss / same-agent
            # agreement / diversion reverted to legacy): best-effort
            # evaluation bookkeeping only.
            _record_canary_best_effort(selected, shadow_ctx, decided_at, canary)
    else:
        canary_error = shadow_ctx.get("canary_error")
        if canary_error:
            _record_canary_error_best_effort(
                shadow_ctx, decided_at, canary_error, selected)
        _record_shadow_best_effort(selected, shadow_ctx, decided_at)
    return selected


def describe_project_pool(project_id):
    return {"project_id": project_id, **ensure_pool_for_project(project_id)}

def initialize_registered_projects():
    projects = _load(PROJECTS_FILE, {"projects": {}})
    for project in projects.get("projects", {}).values():
        project_id = project.get("project_id")
        if project_id:
            ensure_pool_for_project(project_id)

if __name__ == "__main__":
    initialize_registered_projects()
    print(json.dumps(load_pools(), ensure_ascii=False, indent=2))
