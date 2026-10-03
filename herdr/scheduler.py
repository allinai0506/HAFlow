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


def node_is_complete(tasks_for_node: Sequence[Dict[str, Any]], required_task_ids=None) -> bool:
    """节点完成判定(与控制器 is_node_complete 同口径的纯函数版本)。

    - 零任务 -> 未完成;
    - 任一活跃任务不在完成集合 -> 未完成。
    """
    tasks = [t for t in (tasks_for_node or []) if isinstance(t, dict)]
    active = [t for t in tasks if t.get("status") != "superseded" and not t.get("superseded_by")]
    if not active:
        return False
    if required_task_ids is not None and (not isinstance(required_task_ids, list) or any(
            not isinstance(tid, str) or not tid.strip() for tid in required_task_ids)):
        return False
    obligations = list(required_task_ids or []) + [
        t.get("task_id") for t in tasks if t.get("replacement_pending")]
    if obligations:
        by_id = {t.get("task_id"): t for t in tasks}
        for required_id in obligations:
            if not required_id:
                return False
            seen = set()
            current = required_id
            while current not in seen:
                seen.add(current)
                task = by_id.get(current)
                if task is None:
                    return False
                if task.get("status") != "superseded" and not task.get("superseded_by"):
                    break
                if (task.get("replacement_pending") is False
                        and not task.get("superseded_by")
                        and required_id not in (required_task_ids or [])):
                    # Explicitly abandoning the lineage head resolves automatic
                    # replacement work, never a configured required output.
                    break
                current = task.get("superseded_by")
                replacement = by_id.get(current)
                if replacement and any(task.get(key) and replacement.get(key)
                        and task[key] != replacement[key] for key in ("workflow_id", "node")):
                    return False
            else:
                return False
    return all(
        str(t.get("status") or "") in NODE_DONE_STATUSES
        and (t.get("integration_mode") != "git"
             or t.get("status") in ("integrated", "cleanup_ready", "cleaned"))
        for t in active
    )


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


def _text(task, key):
    value = task.get(key)
    if value in (None, ""):
        return ""
    return str(value).strip()


def is_scheduler_managed_task(task):
    """Whether this task must prove completion evidence (PR #107 semantics).

    A task counts as scheduler-managed when the scheduler recorded a candidate
    claim for it, or it sits on a verifier node in a scheduler-engaged
    workflow. Those are exactly the tasks whose verdict decides whether a
    frozen candidate may pass the join gate, so for them "which revision was
    actually verified" is a load-bearing question rather than a nicety.

    A task with no claim at all is not scheduler-managed: nothing downstream
    treats it as evidence about a candidate, so refusing its verdict would
    break unrelated legacy work without closing any real hole.
    """
    if not isinstance(task, dict):
        return False
    return bool(_text(task, "candidate_sha"))


def extract_task_verified_sha(task):
    """Completion evidence: the revision verified when the verdict was written.

    ``verified_candidate_sha`` is re-read from the live clone at verdict-write
    time, so it is the only value that survives an agent that pulls, checks
    out, or rebases mid-task.

    ``baseline_commit`` / ``baseline_sha`` are *launch* evidence. For a
    scheduler-managed task they are deliberately NOT a fallback: silently
    degrading to them would reinstate precisely the "they happened to start
    from the same candidate" claim that completion evidence exists to
    eliminate. Legacy tasks keep the fallback so pre-Scheduler workflows
    retain their previous meaning.
    """
    if not isinstance(task, dict):
        return ""
    completion = _text(task, "verified_candidate_sha")
    if completion:
        return completion
    if is_scheduler_managed_task(task):
        return ""
    return _text(task, "baseline_commit") or _text(task, "baseline_sha")


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

    Returns (ok, claim, evidence). A scheduler-managed task that carries a
    candidate claim but no completion evidence is NOT consistent: it asserts a
    revision it never proved. Reporting that as consistent is what allowed
    launch evidence to stand in for completion evidence.

    A task with no claim at all is unaffected; it is not scheduler-managed and
    has no candidate assertion to support.
    """
    claim = extract_task_candidate_claim(task)
    evidence = extract_task_verified_sha(task)
    if not evidence:
        if claim:
            return False, claim, evidence
        return True, claim, evidence
    return shas_identical(claim, evidence), claim, evidence


#: Effective verification sources (§21). ``fresh`` outranks ``reuse``; a
#: verifier with neither is ``none`` and never counts as a pass.
EFFECTIVE_FRESH = "fresh"
EFFECTIVE_REUSE = "reuse"
EFFECTIVE_NONE = "none"


def resolve_effective_verification(
    tasks,
    workflow_id,
    verifier,
    candidate_sha,
    reuse_facts=None,
):
    """Resolve the verification that currently counts for one verifier.

    Returns a dict with ``status`` and ``source`` (§21):

    - ``fresh``  — a real task proved exactly ``candidate_sha``;
    - ``reuse``  — no fresh evidence exists and a reuse fact binds this
      verifier to exactly ``candidate_sha``;
    - ``none``   — no admissible evidence; the branch is unsatisfied.

    Precedence is the whole point of this function. A fresh verdict is
    authoritative even when it is BLOCKED: an old reuse fact must never
    resurrect a candidate the verifier actually rejected (§22, Case 12). A
    reuse fact therefore only applies where fresh evidence is *absent* — a
    running, incomplete, or unproven task is a live claim on the candidate, and
    silently substituting a reuse fact for it would let the gate pass on a
    verification that is still running.
    """
    expected = str(candidate_sha or "").strip()
    dep_tasks = node_tasks(tasks, workflow_id, verifier)
    reuse = _select_reuse_fact(reuse_facts, verifier, expected)

    if dep_tasks:
        complete = node_is_complete(dep_tasks)
        if not complete:
            return {"verifier": verifier, "candidate_sha": expected,
                    "status": "pending", "source": EFFECTIVE_NONE,
                    "tasks": [str(t.get("task_id") or "") for t in dep_tasks],
                    "reuse_ignored": bool(reuse)}
        unproven = [
            str(t.get("task_id") or "") for t in dep_tasks
            if extract_task_candidate_claim(t)
            and not extract_task_verified_sha(t)
        ]
        if unproven:
            return {"verifier": verifier, "candidate_sha": expected,
                    "status": "unproven", "source": EFFECTIVE_NONE,
                    "unproven_tasks": sorted(unproven),
                    "reuse_ignored": bool(reuse)}
        if not expected:
            return {"verifier": verifier, "candidate_sha": "",
                    "status": "unproven", "source": EFFECTIVE_NONE,
                    "reason": "no expected candidate to bind to"}
        matched = [t for t in dep_tasks if candidate_revision_matches(t, expected)]
        if not matched:
            return {"verifier": verifier, "candidate_sha": expected,
                    "status": "stale", "source": EFFECTIVE_NONE,
                    "verified": sorted({
                        extract_task_candidate_sha(t) for t in dep_tasks
                        if extract_task_candidate_sha(t)}),
                    "reuse_ignored": bool(reuse)}
        return {
            "verifier": verifier,
            "candidate_sha": expected,
            "status": node_verdict(matched) or "unproven",
            "source": EFFECTIVE_FRESH,
            "tasks": sorted(str(t.get("task_id") or "") for t in matched),
        }

    if reuse:
        return {
            "verifier": verifier,
            "candidate_sha": expected,
            "status": "pass",
            "source": EFFECTIVE_REUSE,
            "source_candidate_sha": str(reuse.get("from_candidate_sha") or ""),
            "source_task_id": str(reuse.get("source_task_id") or ""),
            "decision_event_id": reuse.get("event_id"),
            "policy_version": str(reuse.get("policy_version") or ""),
            "changed_paths": list(reuse.get("changed_paths") or []),
        }
    return {"verifier": verifier, "candidate_sha": expected,
            "status": "none", "source": EFFECTIVE_NONE}


def _select_reuse_fact(reuse_facts, verifier, candidate_sha):
    """The last reuse fact in ``reuse_facts`` bound to (verifier, candidate_sha).

    Selection is by position, not by recency: the caller supplies the facts in
    ledger order (oldest first), so the last match is the newest. Making that
    dependence explicit matters because ``evaluate_join_gate`` is a public
    pure function — a caller that passes an unordered list gets an
    order-dependent answer, and a fact with a ``timestamp`` would look like the
    tie-breaker it is not.
    """
    name = normalize_node_id(verifier)
    expected = str(candidate_sha or "").strip()
    if not name or not expected:
        return None
    found = None
    for fact in reuse_facts or []:
        if not isinstance(fact, dict):
            continue
        if normalize_node_id(fact.get("verifier")) != name:
            continue
        if not shas_identical(fact.get("to_candidate_sha"), expected):
            continue
        if str(fact.get("source_verdict") or "") != "pass":
            continue
        if not str(fact.get("source_verified_candidate_sha") or "").strip():
            continue
        found = fact
    return found


def evaluate_join_gate(gate_node, tasks, workflow_id, expected_candidate_sha="",
                       reuse_facts=None):
    """Deterministic join-gate verdict (pure, no LLM).

    Returns (passed, reason, details).

    ``reuse_facts`` carries the PR #108 reuse facts for this workflow. A branch
    with no task may be satisfied by a reuse fact bound to exactly the expected
    candidate; every other branch still needs its own fresh proof. Passing
    ``reuse_facts=None`` (the default) reproduces the PR #107 verdict exactly.
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
        # A reuse fact (PR #108) may only stand in for a branch that has NO
        # task at all. Any existing task — complete or not, passed or blocked —
        # is fresh evidence and keeps its own meaning: a verifier that actually
        # ran against the candidate must never have its verdict replaced by a
        # fact about an earlier candidate (§22, Case 12).
        #
        # The "no task" requirement is also what keeps the ledger and this gate
        # from disagreeing. The controller treats a reuse fact as satisfying
        # the node, so no new task is created for it; if the fact could also
        # satisfy a branch that still holds a task, the ledger would claim a
        # verification this gate refuses, and the workflow would stall on a
        # branch that is never going to re-run.
        reuse = None
        if not dep_tasks:
            reuse = _select_reuse_fact(reuse_facts, dep, expected)
        if reuse:
            branch_states[dep] = {
                "complete": True,
                "verdict": "pass",
                "candidate_shas": [expected],
                "claim_evidence_mismatch": [],
                "missing_completion_evidence": [],
                "evidence_source": EFFECTIVE_REUSE,
                "reuse": reuse,
            }
            continue
        complete = node_is_complete(dep_tasks)
        verdict = node_verdict(dep_tasks) if complete else None
        shas = sorted(
            {extract_task_candidate_sha(t) for t in dep_tasks
             if extract_task_candidate_sha(t)}
        )
        # Claim/evidence split: the dispatch claim must match the revision the
        # verifier actually proved, otherwise the task proves nothing about the
        # claimed revision. A scheduler-managed task with no completion
        # evidence at all is reported separately: it is a missing proof, not a
        # contradiction, and it must not be satisfied by launch evidence.
        inconsistent = []
        missing_evidence = []
        for task in dep_tasks:
            claim = extract_task_candidate_claim(task)
            evidence = extract_task_verified_sha(task)
            task_id = str(task.get("task_id") or "")
            if not claim:
                # No candidate assertion to support or contradict: a legacy
                # task outside scheduler semantics. Left untouched so
                # pre-Scheduler workflows keep their previous meaning.
                continue
            if not evidence:
                # A claim with nothing behind it: a missing proof, not a
                # contradiction. Checked before the mismatch report so it is
                # never described as a mere disagreement.
                missing_evidence.append({"task_id": task_id, "claim": claim})
            elif not shas_identical(claim, evidence):
                inconsistent.append({
                    "task_id": task_id,
                    "claim": claim,
                    "evidence": evidence,
                })
        branch_states[dep] = {
            "complete": complete,
            "verdict": verdict,
            "candidate_shas": shas,
            "claim_evidence_mismatch": inconsistent,
            "missing_completion_evidence": missing_evidence,
            "evidence_source": EFFECTIVE_FRESH if complete else EFFECTIVE_NONE,
        }
    details["branches"] = branch_states

    incomplete = sorted(d for d, st in branch_states.items() if not st["complete"])
    if incomplete:
        details["incomplete"] = incomplete
        return False, JOIN_WAITING, details

    # Missing completion evidence is checked before the mismatch report: a
    # verifier that proved nothing must never be reported as merely
    # contradicting its claim, because that reading implies evidence exists.
    unproven = {
        d: st["missing_completion_evidence"]
        for d, st in branch_states.items() if st["missing_completion_evidence"]
    }
    if unproven:
        details["missing_completion_evidence"] = unproven
        details["reason_detail"] = (
            "a scheduler-managed verifier carries a candidate claim but no "
            "verified_candidate_sha, so the revision it actually verified is "
            "unproven (launch baseline is not completion evidence)"
        )
        return False, JOIN_MISSING_CANDIDATE, details

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


def verifier_branch_node_ids(workflow_cfg):
    """Node ids a reuse fact may satisfy: the join gate's branch nodes.

    Derived from the same DAG the gate reads, so a verifier is admissible
    exactly when the gate will ask it for proof. A gate node itself is never a
    branch, and a node nobody gates on is not a verifier — permitting reuse for
    either would let a fact satisfy a node no gate consults.

    Pure: the caller supplies the workflow config. Failure to read a config
    yields an empty set, which means no verifier can be reused (fail-closed).
    """
    found = set()
    for node in (workflow_cfg or {}).get("nodes") or []:
        if not isinstance(node, dict):
            continue
        deps = node_dependencies(node)
        is_gate = str(node.get("node_type") or "") == "gate" and len(deps) >= 2
        if is_gate or len(deps) >= 2:
            found.update(deps)
    return sorted(n for n in found if n)


def join_ready(gate_node, tasks, workflow_id, expected_candidate_sha="",
               reuse_facts=None):
    """Boolean shortcut for evaluate_join_gate."""
    passed, _reason, _details = evaluate_join_gate(
        gate_node, tasks, workflow_id, expected_candidate_sha, reuse_facts
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
