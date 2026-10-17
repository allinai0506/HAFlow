"""herdr.experience.extractor — Evidence-based Business Rule Extractor.

Pure functional extractor: analyzes execution facts, verification receipts,
and human interventions to extract actionable business rules.

Invariants:
- Never invents rules without human intervention evidence.
- Never extracts candidates when qualified_success is False or verification failed.
- Redacts all sensitive credentials/tokens before candidate creation.
- Deterministic identity generation for idempotency.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from herdr.supervisor.state import redact_text
from .models import (
    AppliesTo,
    AuditEvent,
    BusinessRuleExperience,
    ExperienceSource,
    ExperienceStatus,
    ExperienceType,
)

# Common trivial words/phrases that do NOT represent business rules
_TRIVIAL_WORDS = {
    "ok", "yes", "no", "继续", "重试", "好的", "通过", "重新运行", "重新跑一下",
    "同意", "知道了", "继续执行", "pass", "retry", "run", "again", "continue",
    "proceed", "go", "ahead", "fine", "sure", "y", "n", "lgtm",
}


def _clean_and_redact(text: str) -> str:
    """Strip ANSI, redact tokens/passwords, and normalize whitespace."""
    if not text:
        return ""
    # Strip ANSI
    cleaned = re.sub(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", text)
    # Redact credentials using existing herdr supervisor redaction
    cleaned = redact_text(cleaned)
    # Additional credential scrubbing for common token formats
    cleaned = re.sub(r"\b(sk-[a-zA-Z0-9]{20,})\b", "[redacted]", cleaned)
    cleaned = re.sub(r"\b(ghp_[a-zA-Z0-9]{20,})\b", "[redacted]", cleaned)
    cleaned = re.sub(r"\b(bearer\s+[a-zA-Z0-9\-._~+/]{8,})\b", "[redacted]", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _is_trivial_instruction(text: str) -> bool:
    cleaned = text.strip().lower()
    if not cleaned or len(cleaned) < 4:
        return True
    # Split into words/tokens
    tokens = [t for t in re.split(r"[\s,;，。！？!?]+", cleaned) if t]
    if not tokens:
        return True
    # If all tokens are trivial words, the whole instruction is trivial
    if all(t in _TRIVIAL_WORDS for t in tokens):
        return True
    return False


def extract_candidate_from_facts(
    task: Dict[str, Any],
    outcome: Optional[Dict[str, Any]] = None,
    steers: Optional[List[Dict[str, Any]]] = None,
    decisions: Optional[List[Dict[str, Any]]] = None,
    rework_prompts: Optional[List[str]] = None,
    contributor: str = "local_employee",
    state_db_path: Optional[Path] = None,
) -> Tuple[Optional[BusinessRuleExperience], str]:
    """Extract candidate business_rule from task facts and human interventions.

    state_db_path must identify the same database the task was read from, so a
    non-default state database is not silently missed by the authoritative
    outcome lookup below.

    Returns:
        (experience_or_None, reason_code)
        reason_code is one of:
        - 'ready': candidate successfully extracted
        - 'unqualified_success': outcome verification not passed or not qualified
        - 'no_human_intervention': no human correction records found
        - 'no_actionable_rule': human inputs were trivial/empty
    """
    if not task:
        return None, "task_missing"

    task_id = str(task.get("task_id") or "")
    run_id = str(task.get("run_id") or (outcome.get("run_id") if outcome else "") or f"run_{task_id}")
    workflow_id = task.get("workflow_id")

    # 1. Verification & Outcome check: MUST be qualified_success
    if outcome is not None:
        if not outcome.get("qualified_success") or not outcome.get("verification_passed"):
            return None, "unqualified_success"
    else:
        # P1-08: outcome=None 不得仅凭 Task 状态推断已经通过 qualified_success。
        # 必须从 execution_outcome 权威事实层核查真实结算记录。
        from herdr import execution_outcome
        real_outcome = execution_outcome.get_execution_outcome(task_id, run_id, state_db_path)
        if not real_outcome or not real_outcome.get("qualified_success") or not real_outcome.get("verification_passed"):
            return None, "unqualified_success"
        outcome = real_outcome

    # 2. Gather human corrections
    human_instructions: List[str] = []
    evidence_refs: List[str] = []

    # From steers
    if steers:
        for s in steers:
            op = str(s.get("operator") or "human").strip().lower()
            instruction = (s.get("instruction") or "").strip()
            steer_id = s.get("steer_id") or ""
            # Accept human operators, coordinators, authorized users, admins
            is_human_op = op == "human" or any(k in op for k in ("coordinator", "user", "admin", "controller"))
            if is_human_op and instruction and instruction not in human_instructions:
                human_instructions.append(instruction)
                if steer_id:
                    evidence_refs.append(f"steer:{steer_id}")

    # From rework prompts
    if rework_prompts:
        for rp in rework_prompts:
            if rp and rp.strip() and rp.strip() not in human_instructions:
                human_instructions.append(rp.strip())
                evidence_refs.append("rework_prompt")

    rework_p = task.get("rework_prompt")
    if rework_p and rework_p.strip() and rework_p.strip() not in human_instructions:
        human_instructions.append(rework_p.strip())
        evidence_refs.append("task:rework_prompt")

    # From task goal if it is a rework task
    goal = str(task.get("goal") or "").strip()
    if (task.get("supersedes") or any(k in goal for k in ("返工", "修复", "DEFECT", "补偿", "闭环"))) and goal:
        if goal not in human_instructions:
            human_instructions.append(goal)
            evidence_refs.append(f"task:{task_id}:rework_goal")

    # From human decisions (P1-08: Must be resolved decisions, never unconfirmed questions or stale notes)
    if decisions:
        for d in decisions:
            if d.get("stale"):
                continue
            status = d.get("decision_status") or d.get("status")
            if d.get("decision_id") and status and status != "resolved":
                # Unanswered open question: reject!
                continue
            dec = (
                d.get("decision")
                or d.get("selected_option")
                or d.get("answer")
                or d.get("detail")
                or d.get("body")
                or d.get("title")
                or ""
            ).strip()
            if not dec:
                # Do NOT fall back to d.get("question")
                continue
            if dec not in human_instructions:
                human_instructions.append(dec)
                note_id = d.get("note_id") or d.get("decision_id") or ""
                if note_id:
                    evidence_refs.append(f"decision:{note_id}")

    if not human_instructions:
        return None, "no_human_intervention"

    # 3. Filter trivial phrases
    meaningful = []
    for inst in human_instructions:
        cleaned = _clean_and_redact(inst)
        if not _is_trivial_instruction(cleaned) and cleaned not in meaningful:
            meaningful.append(cleaned)

    if not meaningful:
        return None, "no_actionable_rule"

    combined_rule_content = "\n\n".join(meaningful)
    if len(combined_rule_content.strip()) < 8:
        return None, "no_actionable_rule"

    # 4. Extract scope (customer, task_type)
    payload = task.get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    elif not isinstance(payload, dict):
        payload = {}

    context_meta = task.get("context_metadata") or {}
    if isinstance(context_meta, str):
        try:
            context_meta = json.loads(context_meta)
        except Exception:
            context_meta = {}

    # P1-06: 未知客户保持 unknown，严禁视为全局或 default_customer
    raw_customer = (
        task.get("customer")
        or payload.get("customer")
        or payload.get("customer_id")
        or context_meta.get("customer")
        or context_meta.get("customer_id")
    )
    if not raw_customer or str(raw_customer).strip() in {"default_customer", "unknown", "None", ""}:
        customer = "unknown"
    else:
        customer = str(raw_customer).strip()

    task_type = (
        task.get("business_task_type")
        or task.get("task_type")
        or payload.get("task_type")
        or task.get("stage")
        or task.get("node")
        or "general"
    )

    # 5. Build title & summary
    # Use first line or summary for title
    first_line = meaningful[0].split("\n")[0].strip()
    if len(first_line) > 50:
        first_line = first_line[:47] + "..."
    title = f"{customer} - {task_type} 业务规则: {first_line}"

    # 6. Deterministic experience_id
    raw_id_seed = f"{task_id}:{run_id}:{customer}:{combined_rule_content}"
    exp_hash = hashlib.sha256(raw_id_seed.encode("utf-8")).hexdigest()[:16]
    experience_id = f"exp_{exp_hash}"

    source = ExperienceSource(
        task_id=task_id,
        run_id=run_id,
        workflow_id=workflow_id,
        outcome_id=outcome.get("outcome_id") if outcome else None,
        evidence_refs=evidence_refs,
        correction_summary=first_line,
    )

    applies_to = AppliesTo(
        customer=str(customer),
        task_type=str(task_type),
        tags=[str(customer), str(task_type)],
        conditions={},
    )

    recorded = outcome.get("recorded_at") if outcome else None
    try:
        now = float(recorded) if recorded is not None else time.time()
    except (ValueError, TypeError):
        now = time.time()

    audit = [
        AuditEvent(
            action="candidate_extracted",
            actor=contributor,
            timestamp=now,
            note=f"自动提炼候选业务规则 (来源任务 {task_id}, 纠偏项数: {len(meaningful)})",
        )
    ]

    exp = BusinessRuleExperience(
        experience_id=experience_id,
        type=ExperienceType.BUSINESS_RULE.value,
        title=title,
        content=combined_rule_content,
        contributor=contributor,
        source=source,
        applies_to=applies_to,
        status=ExperienceStatus.PENDING_REVIEW.value,
        version=1,
        created_at=now,
        updated_at=now,
        audit_trail=audit,
    )

    return exp, "ready"


def extract_candidates_from_lessons(
    lessons_markdown: str,
    contributor: str = "local_employee",
    min_lesson_num: int = 1,
) -> List[BusinessRuleExperience]:
    """Extract structured corporate business rules from docs/lessons/lessons-learned.md.

    Each lesson section (e.g. '## 164. <Title>') is parsed. The '经验教训' and '操作规范'
    sections are extracted as actionable business rules.
    """
    if not lessons_markdown:
        return []

    candidates = []
    pattern = re.compile(r"^##\s+(\d+)\.\s+(.+?)(?=\n##\s+\d+|\Z)", re.MULTILINE | re.DOTALL)
    for match in pattern.finditer(lessons_markdown):
        lesson_num = int(match.group(1))
        if lesson_num < min_lesson_num:
            continue
        raw_title = match.group(2).split("\n")[0].strip()
        body = match.group(2)

        clean_title = re.sub(r"[（(]\d{4}-\d{2}-\d{2}[)）]", "", raw_title).strip()

        customer = "HAFlow"
        task_type = "engineering"
        if "nexusarchive" in body.lower():
            customer = "nexusarchive"
            task_type = "archive"
        elif "xiyu" in body.lower():
            customer = "xiyu"
            task_type = "bid"

        rule_sections = []
        for sec_name in ("经验教训", "操作规范", "相关规范"):
            sec_match = re.search(r"###\s+" + sec_name + r"\s*\n(.*?)(?=\n###|\Z)", body, re.DOTALL)
            if sec_match:
                cleaned_sec = _clean_and_redact(sec_match.group(1).strip())
                if cleaned_sec:
                    rule_sections.append(f"【{sec_name}】\n{cleaned_sec}")

        if not rule_sections:
            cleaned_body = _clean_and_redact(body.strip())
            if len(cleaned_body) >= 20:
                rule_sections.append(cleaned_body)

        if not rule_sections:
            continue

        combined_content = "\n\n".join(rule_sections)
        raw_seed = f"lesson_{lesson_num}:{customer}:{clean_title}"
        exp_hash = hashlib.sha256(raw_seed.encode("utf-8")).hexdigest()[:16]
        experience_id = f"exp_l{lesson_num}_{exp_hash[:8]}"

        line_offset = lessons_markdown[:match.start()].count("\n") + 1
        now = time.time()
        source = ExperienceSource(
            task_id=f"lesson-{lesson_num}",
            run_id=f"run_lesson_{lesson_num}",
            workflow_id=None,
            evidence_refs=[f"docs/lessons/lessons-learned.md#L{line_offset}"],
            correction_summary=clean_title,
        )
        applies_to = AppliesTo(
            customer=customer,
            task_type=task_type,
            tags=[customer, task_type, f"lesson_{lesson_num}"],
            conditions={},
        )
        audit = [
            AuditEvent(
                action="candidate_extracted",
                actor=contributor,
                timestamp=now,
                note=f"从企业经验库提炼业务规则资产 (Lesson #{lesson_num})",
            )
        ]
        exp = BusinessRuleExperience(
            experience_id=experience_id,
            type=ExperienceType.BUSINESS_RULE.value,
            title=f"{customer} - 业务规则: {clean_title}",
            content=combined_content,
            contributor=contributor,
            source=source,
            applies_to=applies_to,
            status=ExperienceStatus.PENDING_REVIEW.value,
            version=1,
            created_at=now,
            updated_at=now,
            audit_trail=audit,
        )
        candidates.append(exp)

    return candidates
