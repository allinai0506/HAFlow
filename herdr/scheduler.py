"""Critical-Path Scheduler v1 (HAFlow PR #107).

纯函数调度与汇聚校验核心:决定「哪些节点可以启动」,
以及「汇聚门禁是否允许通过」。绝不决定具体 Agent 的选择
(那是 agent_router 的职责),绝不调用 LLM,绝不做 I/O。

术语约定:
- Ready Node:依赖全部满足、可以派发的节点。
- Join Gate:多个并行分支的汇聚门禁,所有前置分支必须在
  同一个 candidate_sha 上通过,当前候选仍是最新的。
- candidate_sha:不可变的交付候选快照,全链路唯一交付身份
  (权威定义见 herdr/delivery_record.py)。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:
    from .transitions import COMPLETED_TASK_STATUSES
except ImportError:  # pragma: no cover - script-style fallback
    from herdr.transitions import COMPLETED_TASK_STATUSES

# 节点完成态:与 services/herdr-controller.py#is_node_complete 的
# 完成集合保持一致,避免两处完成语义漂移。
NODE_DONE_STATUSES = frozenset(COMPLETED_TASK_STATUSES)

# Join Gate 判决结果。
JOIN_SATISFIED = "join_satisfied"
JOIN_WAITING = "join_waiting"
JOIN_BLOCKED = "join_blocked"
JOIN_STALE = "join_stale"
JOIN_CANDIDATE_MISMATCH = "join_candidate_mismatch"
JOIN_MISSING_CANDIDATE = "join_missing_candidate"
JOIN_EVIDENCE_MISMATCH = "join_evidence_mismatch"


def normalize_node_id(value: Any) -> str:
    """归一化节点 id,非法输入返回空字符串。"""
    if value is None:
        return ""
    return str(value).strip()


def node_dependencies(node: Dict[str, Any]) -> List[str]:
    """返回节点的 depends_on 依赖(归一化、去重、保序)。"""
    raw = (node or {}).get("depends_on") or []
    if isinstance(raw, str):
        raw = [raw]
    seen: Set[str] = set()
    deps: List[str] = []
    for item in raw or []:
        dep = normalize_node_id(item)
        if dep and dep not in seen:
            seen.add(dep)
            deps.append(dep)
    return deps


def compute_ready_nodes(
    nodes: Sequence[Dict[str, Any]],
    completed_node_ids: Set[str],
    running_node_ids: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """计算可以启动的节点(纯函数,无副作用)。

    Ready 规则:
    1. 节点自身不在 completed_node_ids 中;
    2. 节点的 depends_on 全部落在 completed_node_ids 中;
    3. 默认排除正在执行中的节点(running_node_ids),避免重复派发。
    """
    completed = {normalize_node_id(n) for n in (completed_node_ids or set())}
    completed.discard("")
    running = {normalize_node_id(n) for n in (running_node_ids or set())}
    running.discard("")

    ready: List[Dict[str, Any]] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        node_id = normalize_node_id(node.get("id"))
        if not node_id:
            continue
        if node_id in completed:
            continue
        if node_id in running:
            continue
        deps = node_dependencies(node)
        if all(dep in completed for dep in deps):
            ready.append(node)
    return ready


def dependencies_satisfied(
    node: Dict[str, Any],
    completed_node_ids: Set[str],
) -> bool:
    """单个节点的依赖是否全部满足。"""
    completed = {normalize_node_id(n) for n in (completed_node_ids or set())}
    return all(dep in completed for dep in node_dependencies(node))


def node_tasks(
    tasks: Sequence[Dict[str, Any]],
    workflow_id: str,
    node_id: str,
) -> List[Dict[str, Any]]:
    """提取某 workflow 下某节点的活跃任务(排除 superseded 谱系)。"""
    wid = normalize_node_id(workflow_id)
    nid = normalize_node_id(node_id)
    matched: List[Dict[str, Any]] = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        if normalize_node_id(task.get("workflow_id")) != wid:
            continue
        if normalize_node_id(task.get("node")) != nid and normalize_node_id(
            task.get("stage")
        ) != nid:
            continue
        if task.get("status") == "superseded" or task.get("superseded_by"):
            continue
        matched.append(task)
    return matched


def node_is_complete(tasks_for_node: Sequence[Dict[str, Any]]) -> bool:
    """节点完成判定(与控制器 is_node_complete 同口径的纯函数版本)。

    - 零任务 -> 未完成;
    - 任一活跃任务不在完成集合 -> 未完成。
    """
    active = [t for t in (tasks_for_node or []) if isinstance(t, dict)]
    if not active:
        return False
    return all(str(t.get("status") or "") in NODE_DONE_STATUSES for t in active)


def node_verdict(tasks_for_node):
    """Node acceptance verdict: any blocked wins, else any pass."""
    verdict = None
    for task in tasks_for_node or []:
        if not isinstance(task, dict):
            continue
        task_verdict = str(task.get("stage_verdict") or "").strip()
        if task_verdict == "blocked":
            return "blocked"
        if task_verdict == "pass":
            verdict = "pass"
    return verdict


def extract_task_candidate_claim(task):
    """The candidate revision the scheduler TOLD this task to verify (claim).

    A claim alone proves nothing: it is what dispatch injected, not what the
    agent actually verified. Gate decisions must read evidence instead.
    """
    if not isinstance(task, dict):
        return ""
    value = task.get("candidate_sha")
    return str(value).strip() if value not in (None, "") else ""


#: Shortest abbreviation this module treats as a trustworthy prefix of a full
#: object ID. Git's own default abbreviation is 7, and ``core.abbrev`` never
#: drops below 4; 7 keeps the pure-core comparison conservative. The
#: authoritative canonicalisation (``rev-parse <sha>^{commit}``) happens at the
#: boundary in bin/herdr-task, where the repository is reachable.
MIN_ABBREVIATED_SHA_LEN = 7

# A git object ID (or an abbreviation of one). Values outside this shape are
# still compared verbatim, so synthetic identifiers used by callers and tests
# keep working; they simply never gain prefix semantics.
_HEX_SHA_RE = re.compile(r"[0-9a-f]{4,40}\Z")


def normalize_sha(sha):
    """Lower-case and trim a revision string for comparison.

    This normalises *form* only. It deliberately does not expand an
    abbreviation: that needs a repository, and this module is pure.
    """
    return str(sha or "").strip().lower()


def _is_hex_object_id(sha):
    return bool(_HEX_SHA_RE.match(sha))


def shas_identical(left, right):
    """Whether two revision strings name the same commit (no ancestor semantics).

    Equality is strict. Abbreviation is tolerated only in the one direction git
    actually allows: a full object ID and an abbreviation that prefixes it, and
    only when both sides look like object IDs. A shortened value never matches a
    full value it is not a prefix of, and no ancestor/descendant relation is ever
    accepted.
    """
    a = normalize_sha(left)
    b = normalize_sha(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if not (_is_hex_object_id(a) and _is_hex_object_id(b)):
        return False
    short, full = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < MIN_ABBREVIATED_SHA_LEN:
        return False
    return full.startswith(short)


def extract_task_verified_sha(task):
    """The revision the clone was ACTUALLY on when verification completed.

    ``verified_candidate_sha`` is re-read from the live clone at verdict-write
    time, so it is completion evidence: it survives an agent that pulls,
    checks out, or rebases mid-task.

    ``baseline_commit`` is only *launch* evidence - the clone HEAD recorded
    when the worker started. It is used as a fallback so tasks that predate the
    completion field keep their previous meaning, but it must never be
    described as a verified revision.
    """
    if not isinstance(task, dict):
        return ""
    for key in ("verified_candidate_sha", "baseline_commit", "baseline_sha"):
        value = task.get(key)
        if value not in (None, ""):
            text = str(value).strip()
            if text:
                return text
    return ""


def extract_task_candidate_sha(task):
    """Verified revision, falling back to the claim when evidence is absent.

    Evidence first: a task whose clone baseline is known reports what the
    agent truly verified. Legacy tasks without baseline_commit fall back to
    the dispatch claim so pre-Scheduler workflows keep their old meaning.
    """
    return extract_task_verified_sha(task) or extract_task_candidate_claim(task)


def candidate_revision_matches(task, expected_sha):
    """Whether the task VERIFIED the expected candidate (empty never matches)."""
    expected = str(expected_sha or "").strip()
    if not expected:
        return False
    return shas_identical(extract_task_verified_sha(task), expected)


def task_claim_evidence_consistent(task):
    """Whether a task's dispatch claim matches its verification evidence.

    Returns (ok, claim, evidence). Absent evidence is not a mismatch here:
    legacy tasks predate the field and are handled by the caller via
    expect_evidence=False.
    """
    claim = extract_task_candidate_claim(task)
    evidence = extract_task_verified_sha(task)
    if not evidence:
        return True, claim, evidence
    return shas_identical(claim, evidence), claim, evidence


def evaluate_join_gate(gate_node, tasks, workflow_id, expected_candidate_sha=""):
    """Deterministic join-gate verdict (pure, no LLM).

    Returns (passed, reason, details).
    """
    gate_node = gate_node or {}
    gate_id = normalize_node_id(gate_node.get("id"))
    deps = node_dependencies(gate_node)
    details = {"gate": gate_id, "depends_on": deps}
    branch_states = {}

    expected = str(expected_candidate_sha or "").strip()
    if expected:
        details["expected_candidate_sha"] = expected

    for dep in deps:
        dep_tasks = node_tasks(tasks, workflow_id, dep)
        complete = node_is_complete(dep_tasks)
        verdict = node_verdict(dep_tasks) if complete else None
        shas = sorted(
            {extract_task_candidate_sha(t) for t in dep_tasks
             if extract_task_candidate_sha(t)}
        )
        # Claim/evidence split: the dispatch claim must match the clone
        # baseline the worker actually recorded, otherwise the task proves
        # nothing about the claimed revision.
        inconsistent = []
        for task in dep_tasks:
            ok, claim, evidence = task_claim_evidence_consistent(task)
            if not ok:
                inconsistent.append({
                    "task_id": str(task.get("task_id") or ""),
                    "claim": claim,
                    "evidence": evidence,
                })
        branch_states[dep] = {
            "complete": complete,
            "verdict": verdict,
            "candidate_shas": shas,
            "claim_evidence_mismatch": inconsistent,
        }
    details["branches"] = branch_states

    incomplete = sorted(d for d, st in branch_states.items() if not st["complete"])
    if incomplete:
        details["incomplete"] = incomplete
        return False, JOIN_WAITING, details

    mismatched = {
        d: st["claim_evidence_mismatch"]
        for d, st in branch_states.items() if st["claim_evidence_mismatch"]
    }
    if mismatched:
        details["evidence_mismatch"] = mismatched
        return False, JOIN_EVIDENCE_MISMATCH, details

    blocked = sorted(d for d, st in branch_states.items() if st["verdict"] == "blocked")
    if blocked:
        details["blocked"] = blocked
        return False, JOIN_BLOCKED, details

    missing = sorted(d for d, st in branch_states.items() if not st["candidate_shas"])
    if missing:
        details["missing_candidate_sha"] = missing
        return False, JOIN_MISSING_CANDIDATE, details

    # Group by normalised form so one branch recording an abbreviated SHA and
    # another recording the full object ID still count as the same revision.
    # Reported values keep their original spelling: the audit trail must show
    # what was actually recorded, not the comparison key.
    observed = {}
    for st in branch_states.values():
        for raw in st["candidate_shas"]:
            observed.setdefault(normalize_sha(raw) or raw, raw)
    details["observed_candidate_shas"] = sorted(observed.values())
    if len(observed) != 1:
        return False, JOIN_CANDIDATE_MISMATCH, details

    observed_sha = sorted(observed)[0]
    reported_sha = observed[observed_sha]
    details["candidate_sha"] = reported_sha
    if expected and not shas_identical(observed_sha, expected):
        details["stale_reason"] = (
            "verified %s but current candidate is %s" % (reported_sha, expected)
        )
        return False, JOIN_STALE, details

    unpassed = sorted(d for d, st in branch_states.items() if st["verdict"] != "pass")
    if unpassed:
        details["unpassed"] = unpassed
        return False, JOIN_BLOCKED, details

    return True, JOIN_SATISFIED, details


def join_ready(gate_node, tasks, workflow_id, expected_candidate_sha=""):
    """Boolean shortcut for evaluate_join_gate."""
    passed, _reason, _details = evaluate_join_gate(
        gate_node, tasks, workflow_id, expected_candidate_sha
    )
    return passed


def candidate_frozen_for_nodes(tasks, workflow_id, node_ids, expected_candidate_sha):
    """Check parallel branches are all bound to the expected candidate.

    Each listed node must have >=1 active task bound to the expected SHA
    and no active task bound to any other SHA.
    """
    expected = str(expected_candidate_sha or "").strip()
    details = {"expected_candidate_sha": expected, "nodes": {}}
    if not expected:
        details["reason"] = "empty expected candidate_sha"
        return False, details

    ok = True
    for node_id in node_ids or []:
        nid = normalize_node_id(node_id)
        dep_tasks = node_tasks(tasks, workflow_id, nid) if nid else []
        bound = [t for t in dep_tasks if candidate_revision_matches(t, expected)]
        foreign = sorted({
            extract_task_candidate_sha(t) for t in dep_tasks
            if extract_task_candidate_sha(t)
            and not candidate_revision_matches(t, expected)
        })
        inconsistent = [
            str(t.get("task_id") or "")
            for t in dep_tasks
            if not task_claim_evidence_consistent(t)[0]
        ]
        node_ok = bool(bound) and not foreign and not inconsistent
        details["nodes"][nid] = {
            "bound_tasks": [str(t.get("task_id") or "") for t in bound],
            "foreign_candidate_shas": foreign,
            "claim_evidence_mismatch": sorted(inconsistent),
            "ok": node_ok,
        }
        if not node_ok:
            ok = False
    return ok, details


def parallel_section_metrics(tasks, workflow_id, node_ids):
    """Parallel-section observability (pure): per-branch windows + overlap.

    Only task-carried timestamps are used; tasks without any timestamp
    are marked unknown instead of guessed.
    """
    nodes = [normalize_node_id(n) for n in (node_ids or []) if normalize_node_id(n)]
    windows = {}
    for nid in nodes:
        stamps = []
        task_ids = []
        for task in node_tasks(tasks, workflow_id, nid):
            task_ids.append(str(task.get("task_id") or ""))
            task_stamps = []
            for key in ("started_at", "created_at", "updated_at"):
                try:
                    value = float(task.get(key) or 0)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    task_stamps.append(value)
            if task_stamps:
                stamps.append((min(task_stamps), max(task_stamps)))
        if stamps:
            windows[nid] = {
                "task_ids": sorted(task_ids),
                "start": min(start for start, _end in stamps),
                "end": max(end for _start, end in stamps),
            }
        else:
            windows[nid] = {"task_ids": sorted(task_ids), "unknown": True}

    known = {nid: w for nid, w in windows.items() if "unknown" not in w}
    overlap = {"pairwise_seconds": {}, "max_overlap_seconds": 0.0}
    names = sorted(known)
    for i, left in enumerate(names):
        for right in names[i + 1:]:
            start = max(known[left]["start"], known[right]["start"])
            end = min(known[left]["end"], known[right]["end"])
            seconds = max(0.0, end - start)
            overlap["pairwise_seconds"][left + "||" + right] = seconds
            overlap["max_overlap_seconds"] = max(
                overlap["max_overlap_seconds"], seconds)

    return {
        "nodes": nodes,
        "windows": windows,
        "overlap": overlap,
        "parallel_evidence": bool(overlap["max_overlap_seconds"] > 0),
    }


def resolve_candidate_sha_for_branch(repo_path, branch, timeout=10):
    """Resolve a branch to its immutable HEAD SHA (bounded git call).

    Returns "" on any failure (unknown refs, missing git, timeout):
    callers treat empty as unprovable and fail closed.
    """
    import subprocess

    branch = str(branch or "").strip()
    if not branch or not repo_path:
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--verify", branch],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return str(result.stdout or "").strip()
