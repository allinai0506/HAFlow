"""Data models for HAFlow Experience Hub V1.

V1 asset type: business_rule.
"""

from __future__ import annotations

import enum
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


class ExperienceType(str, enum.Enum):
    BUSINESS_RULE = "business_rule"


class ExperienceStatus(str, enum.Enum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    PUBLISHED = "published"
    REJECTED = "rejected"
    REVOKED = "revoked"


@dataclass
class AppliesTo:
    customer: Optional[str] = None
    task_type: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    conditions: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "customer": self.customer,
            "task_type": self.task_type,
            "tags": list(self.tags),
            "conditions": dict(self.conditions),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "AppliesTo":
        if not data or not isinstance(data, dict):
            return cls()
        return cls(
            customer=data.get("customer") or None,
            task_type=data.get("task_type") or None,
            tags=list(data.get("tags") or []),
            conditions=dict(data.get("conditions") or {}),
        )


@dataclass
class ExperienceSource:
    task_id: str
    run_id: str
    workflow_id: Optional[str] = None
    outcome_id: Optional[str] = None
    evidence_refs: List[str] = field(default_factory=list)
    correction_summary: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "outcome_id": self.outcome_id,
            "evidence_refs": list(self.evidence_refs),
            "correction_summary": self.correction_summary,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "ExperienceSource":
        if not data or not isinstance(data, dict):
            raise ValueError("source must be a valid dict")
        return cls(
            task_id=str(data.get("task_id") or ""),
            run_id=str(data.get("run_id") or ""),
            workflow_id=data.get("workflow_id") or None,
            outcome_id=data.get("outcome_id") or None,
            evidence_refs=list(data.get("evidence_refs") or []),
            correction_summary=data.get("correction_summary") or None,
        )


@dataclass
class ReviewInfo:
    reviewer: Optional[str] = None
    reviewed_at: Optional[float] = None
    comment: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reviewer": self.reviewer,
            "reviewed_at": self.reviewed_at,
            "comment": self.comment,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> Optional["ReviewInfo"]:
        if not data or not isinstance(data, dict):
            return None
        return cls(
            reviewer=data.get("reviewer"),
            reviewed_at=data.get("reviewed_at"),
            comment=data.get("comment"),
        )


@dataclass
class AuditEvent:
    action: str
    actor: str
    timestamp: float = field(default_factory=time.time)
    note: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "actor": self.actor,
            "timestamp": self.timestamp,
            "note": self.note,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AuditEvent":
        return cls(
            action=str(data.get("action") or ""),
            actor=str(data.get("actor") or "unknown"),
            timestamp=float(data.get("timestamp") or time.time()),
            note=str(data.get("note") or ""),
            details=dict(data.get("details") or {}),
        )


@dataclass
class BusinessRuleExperience:
    experience_id: str
    title: str
    content: str
    contributor: str
    source: ExperienceSource
    applies_to: AppliesTo = field(default_factory=AppliesTo)
    type: str = ExperienceType.BUSINESS_RULE.value
    status: str = ExperienceStatus.PENDING_REVIEW.value
    version: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    review: Optional[ReviewInfo] = None
    audit_trail: List[AuditEvent] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "experience_id": self.experience_id,
            "type": self.type,
            "title": self.title,
            "content": self.content,
            "contributor": self.contributor,
            "applies_to": self.applies_to.to_dict(),
            "source": self.source.to_dict(),
            "status": self.status,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "review": self.review.to_dict() if self.review else None,
            "audit_trail": [a.to_dict() for a in self.audit_trail],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BusinessRuleExperience":
        if not isinstance(data, dict):
            raise ValueError("Data must be a dict")
        return cls(
            experience_id=str(data["experience_id"]),
            title=str(data.get("title") or ""),
            content=str(data.get("content") or ""),
            contributor=str(data.get("contributor") or "unknown"),
            source=ExperienceSource.from_dict(data.get("source")),
            applies_to=AppliesTo.from_dict(data.get("applies_to")),
            type=str(data.get("type") or ExperienceType.BUSINESS_RULE.value),
            status=str(data.get("status") or ExperienceStatus.PENDING_REVIEW.value),
            version=int(data.get("version") or 1),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            review=ReviewInfo.from_dict(data.get("review")),
            audit_trail=[AuditEvent.from_dict(a) for a in (data.get("audit_trail") or [])],
        )
