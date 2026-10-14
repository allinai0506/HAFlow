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
) -> Tuple[Optional[BusinessRuleExperience], str]:
    """Extract candidate business_rule from task facts and human interventions.

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
        # If outcome object not directly passed, check task facts
        if task.get("status") not in {"completed", "committed", "integrated"}:
            return None, "unqualified_success"
        if task.get("acceptance_verdict") is False or task.get("stage_verdict") == "blocked":
            return None, "unqualified_success"

    # 2. Gather human corrections
    human_instructions: List[str] = []
    evidence_refs: List[str] = []

    # From steers
    if steers:
        for s in steers:
            op = s.get("operator") or "human"
            instruction = s.get("instruction") or ""
            steer_id = s.get("steer_id") or ""
            if op == "human" and instruction.strip():
                human_instructions.append(instruction.strip())
                if steer_id:
                    evidence_refs.append(f"steer:{steer_id}")

    # From rework prompts
    if rework_prompts:
        for rp in rework_prompts:
            if rp and rp.strip():
                human_instructions.append(rp.strip())
                evidence_refs.append("rework_prompt")

    rework_p = task.get("rework_prompt")
    if rework_p and rework_p.strip() and rework_p not in human_instructions:
        human_instructions.append(rework_p.strip())
        evidence_refs.append("task:rework_prompt")

    # From human decisions
    if decisions:
        for d in decisions:
            dec = d.get("decision") or d.get("detail") or d.get("question") or ""
            note_id = d.get("note_id") or d.get("decision_id") or ""
            if dec.strip():
                human_instructions.append(dec.strip())
                if note_id:
                    evidence_refs.append(f"decision:{note_id}")

    if not human_instructions:
        return None, "no_human_intervention"

    # 3. Filter trivial phrases
    meaningful = [
        _clean_and_redact(inst)
        for inst in human_instructions
        if not _is_trivial_instruction(inst)
    ]

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

    customer = (
        task.get("customer")
        or payload.get("customer")
        or payload.get("customer_id")
        or context_meta.get("customer")
        or context_meta.get("customer_id")
        or "default_customer"
    )

    task_type = (
        task.get("task_type")
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
