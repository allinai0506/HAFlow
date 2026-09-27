#!/usr/bin/env python3
"""Adaptive Router v2 Canary gate (herdr/canary_router.py).

Canary is the first mode where the Adaptive Router's recommendation
actually executes for a small deterministic slice of production
routings. Everything here is gated by four protections (task spec):

1. Default OFF: no config file (or ``enabled`` not true) disables the
   whole module; routing behavior stays byte-identical to shadow mode.
2. Whitelist buckets only: a plan exists solely for the exact
   ``recommended_agent x node x task_type`` entries in the config.
   There is deliberately no global switch.
3. Deterministic split: sha256("canary-v2|{run_id}|{task_id}") mod 100
   picks the canary slice. Never Python ``hash()`` (randomized), never
   randomness: the same identity lands in the same arm in every
   process, so runs are reproducible and auditable.
4. Fail-open to legacy: ``plan_canary`` may raise; the caller
   (agent_router) treats any exception as "no canary", records a
   route_decision_error, and keeps the legacy pick.

Admission reuses the authoritative shadow evidence functions
(``shadow_rows`` join + ``shadow_sufficiency`` statuses). A bucket is
admitted only when ``model_data_status`` AND ``evaluation_data_status``
are both ``sufficient``; a truncated scan that cannot prove
sufficiency is refused (fail-closed admission).

The diversion target is always a member of the caller's candidate
list — the pool/health/isolation filtering already applied by
``agent_router._choose_agent_impl`` — so canary can never route to an
agent the legacy router would have been forbidden to pick.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import shadow_rows
from .adaptive_router import normalize_task_type, rank_candidates
from .shadow_sufficiency import (
    SUFFICIENT_THRESHOLD,
    evaluate_data_sufficiency,
)

#: Decision semantics differ from shadow: the recommendation executes.
CANARY_ALGORITHM_VERSION = "adaptive-router-v2-canary"

#: Hash domain separator. Changing it re-shuffles every assignment;
#: treat it as part of the persisted contract.
CANARY_HASH_SALT = "canary-v2"

CONFIG_ENV_VAR = "HERDR_ROUTE_CANARY_CONFIG"
DEFAULT_CONFIG_FILENAME = "route-canary.json"
CONFIG_ROOT = Path.home() / ".herdr-controller"

#: Bounded admission window (matched decisions == scanned decisions cap).
#: 30 calibrated samples prove sufficiency; 500 leaves wide margin while
#: keeping the routing hot path far from unbounded scans.
DEFAULT_ADMISSION_SCAN_CAP = 500

MAX_ADMISSION_SCAN_CAP = shadow_rows.DEFAULT_SCAN_CAP


@dataclass(frozen=True)
class CanaryBucket:
    """One whitelisted ``agent x node x task_type`` canary target."""

    agent: str
    node: str
    task_type: str
    #: Per-bucket split override; None falls back to the global
    #: percentage. This is the #104 rollout knob.
    percentage: Optional[int] = None


@dataclass(frozen=True)
class CanaryConfig:
    percentage: int
    buckets: Tuple[CanaryBucket, ...]
    admission_scan_cap: int = DEFAULT_ADMISSION_SCAN_CAP


@dataclass(frozen=True)
class CanaryPlan:
    """A gated canary decision for one routing (in-memory only).

    ``hash_divert`` is the deterministic split decision;
    ``recommended`` is always a member of the caller's validated
    candidate list. The plan exists for eligible buckets regardless of
    the hash, so hash-missed executions still record mode="canary"
    decisions and form the legacy arm of the evaluation.
    """

    recommended: str
    rankings: Tuple[Dict[str, Any], ...]
    hash_bucket: Optional[int]
    hash_divert: bool
    gate: Dict[str, Any]


def config_path(path: Optional[Path] = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get(CONFIG_ENV_VAR)
    if env:
        return Path(env)
    return CONFIG_ROOT / DEFAULT_CONFIG_FILENAME


def _invalid(errors: List[str], message: str) -> Tuple[None, List[str]]:
    errors.append(message)
    return None, errors


def _parse_bucket(raw: Any, errors: List[str], index: int) -> Optional[CanaryBucket]:
    if not isinstance(raw, dict):
        errors.append(f"buckets[{index}] must be an object")
        return None
    fields = {}
    for key in ("agent", "node", "task_type"):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"buckets[{index}].{key} must be a non-empty string")
            return None
        fields[key] = value.strip()
    percentage = raw.get("percentage")
    if percentage is not None:
        if isinstance(percentage, bool) or not isinstance(percentage, int) \
                or not 1 <= percentage <= 100:
            errors.append(
                f"buckets[{index}].percentage must be an int in [1, 100]")
            return None
    return CanaryBucket(
        agent=fields["agent"],
        node=fields["node"],
        task_type=fields["task_type"],
        percentage=percentage,
    )


def read_canary_config(
    path: Optional[Path] = None,
) -> Tuple[Optional[CanaryConfig], List[str]]:
    """Load the canary config; anything invalid means disabled.

    Returns ``(config, errors)``. ``(None, [])`` is "no config / disabled"
    (the default steady state); ``(None, [..])`` is a malformed config the
    operator must fix — either way the gate stays closed.
    """
    errors: List[str] = []
    cfg_path = config_path(path)
    try:
        raw_text = cfg_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, errors
    except OSError as exc:
        return _invalid(errors, f"canary config unreadable: {exc}")
    try:
        raw = json.loads(raw_text)
    except ValueError as exc:
        return _invalid(errors, f"canary config is not valid JSON: {exc}")
    if not isinstance(raw, dict):
        return _invalid(errors, "canary config must be a JSON object")
    if raw.get("enabled") is not True:
        return None, errors

    percentage = raw.get("percentage")
    if isinstance(percentage, bool) or not isinstance(percentage, int) \
            or not 1 <= percentage <= 100:
        return _invalid(errors, "percentage must be an int in [1, 100]")
    raw_buckets = raw.get("buckets")
    if not isinstance(raw_buckets, list) or not raw_buckets:
        return _invalid(errors, "buckets must be a non-empty list")
    buckets: List[CanaryBucket] = []
    for index, raw_bucket in enumerate(raw_buckets):
        bucket = _parse_bucket(raw_bucket, errors, index)
        if bucket is None:
            return None, errors
        buckets.append(bucket)
    scan_cap = raw.get("admission_scan_cap", DEFAULT_ADMISSION_SCAN_CAP)
    if isinstance(scan_cap, bool) or not isinstance(scan_cap, int) \
            or not 1 <= scan_cap <= MAX_ADMISSION_SCAN_CAP:
        return _invalid(
            errors,
            f"admission_scan_cap must be an int in "
            f"[1, {MAX_ADMISSION_SCAN_CAP}]",
        )
    return (
        CanaryConfig(
            percentage=percentage,
            buckets=tuple(buckets),
            admission_scan_cap=scan_cap,
        ),
        errors,
    )


def canary_hash_bucket(task_id: Any, run_id: Any) -> Optional[int]:
    """Deterministic 0..99 split bucket for one execution identity.

    Both task_id and run_id must be present: a missing identity cannot be
    reproduced, so it never enters the canary slice.
    """
    task = str(task_id or "").strip()
    run = str(run_id or "").strip()
    if not task or not run:
        return None
    digest = hashlib.sha256(
        f"{CANARY_HASH_SALT}|{run}|{task}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 100


def _match_bucket(
    config: CanaryConfig,
    agent: str,
    node: str,
    task_type: str,
) -> Optional[CanaryBucket]:
    for bucket in config.buckets:
        if bucket.agent == agent and bucket.node == node \
                and bucket.task_type == task_type:
            return bucket
    return None


def _bucket_admission(
    db_path: Optional[Path],
    *,
    agent: str,
    node: str,
    task_type: str,
    scan_cap: int,
) -> Dict[str, Any]:
    """Authoritative per-bucket sufficiency over a bounded window.

    Same join, same statuses as Shadow Evaluation (shadow_rows +
    shadow_sufficiency) restricted to one bucket. When the window
    truncates before proving sufficiency the bucket is refused:
    admission uncertainty always fails closed.
    """
    rows, meta = shadow_rows._collect_rows_with_meta(
        db_path,
        node=node,
        task_type=task_type,
        agent=agent,
        limit=scan_cap,
        scan_cap=scan_cap,
        mode="all",
    )
    execution_rows = shadow_rows.select_authoritative_execution_rows(rows)
    buckets = evaluate_data_sufficiency(rows, execution_rows)
    target = next(
        (
            bucket
            for bucket in buckets
            if bucket["agent"] == agent
            and bucket["node"] == node
            and bucket["task_type"] == task_type
        ),
        None,
    )
    truncated = bool(meta.get("truncated"))
    admission: Dict[str, Any] = {
        "model_data_status": None,
        "evaluation_data_status": None,
        "model_sample_count": None,
        "evaluation_sample_count": None,
        "calibration_sample_count": None,
        "truncated": truncated,
        "matched_rows": int(meta.get("matched_rows") or 0),
    }
    if target is None:
        return admission
    admission.update({
        "model_data_status": target["model_data_status"],
        "evaluation_data_status": target["evaluation_data_status"],
        "model_sample_count": target["model_sample_count"],
        "evaluation_sample_count": target["evaluation_sample_count"],
        "calibration_sample_count": target["calibration_sample_count"],
    })
    return admission


def _admission_sufficient(admission: Dict[str, Any]) -> bool:
    model_n = admission.get("model_sample_count")
    eval_n = admission.get("calibration_sample_count")
    statuses_ok = (
        admission.get("model_data_status") == "sufficient"
        and admission.get("evaluation_data_status") == "sufficient"
    )
    if not statuses_ok:
        return False
    if admission.get("truncated"):
        # Counts from a truncated window are lower bounds only; they
        # prove sufficiency when they already clear the threshold but
        # never "warm up into" eligibility mid-scan.
        return (
            isinstance(model_n, int)
            and isinstance(eval_n, int)
            and model_n >= SUFFICIENT_THRESHOLD
            and eval_n >= SUFFICIENT_THRESHOLD
        )
    return True


def plan_canary(
    *,
    config: CanaryConfig,
    candidates: List[str],
    node: str,
    task_type: Any,
    task_id: str,
    run_id: str,
    db_path: Optional[Path] = None,
    decided_at: float,
    active_loads: Optional[Dict[str, int]] = None,
    reserved_loads: Optional[Dict[str, int]] = None,
    exclude_run_id: Optional[str] = None,
) -> Optional[CanaryPlan]:
    """Gate one routing into the canary slice (pure read, may raise).

    Order: rank (frozen belief) -> whitelist the recommended bucket ->
    sufficient-only admission -> deterministic hash split. Any failure
    raises; the caller fails open to the legacy pick.
    """
    normalized_type = normalize_task_type(task_type)
    node_name = str(node or "")
    rankings = rank_candidates(
        list(candidates),
        db_path=db_path,
        node=node_name,
        task_type=normalized_type,
        cutoff=float(decided_at),
        exclude_run_id=exclude_run_id,
        active_loads=dict(active_loads or {}),
        reserved_loads=dict(reserved_loads or {}),
    )
    if not rankings:
        return None
    recommended = str(rankings[0]["agent"] or "")
    if not recommended:
        return None
    bucket = _match_bucket(config, recommended, node_name, normalized_type)
    if bucket is None:
        return None
    admission = _bucket_admission(
        db_path,
        agent=recommended,
        node=node_name,
        task_type=normalized_type,
        scan_cap=config.admission_scan_cap,
    )
    if not _admission_sufficient(admission):
        return None
    hash_bucket = canary_hash_bucket(task_id, run_id)
    effective_percentage = (
        bucket.percentage if bucket.percentage is not None
        else config.percentage
    )
    hash_divert = hash_bucket is not None and hash_bucket < effective_percentage
    gate = {
        "bucket_key": f"{recommended}/{node_name}/{normalized_type}",
        "hash_bucket": hash_bucket,
        "effective_percentage": effective_percentage,
        "hash_divert": hash_divert,
        "truncated": bool(admission.get("truncated")),
        "admission": {
            key: admission.get(key)
            for key in (
                "model_data_status",
                "evaluation_data_status",
                "model_sample_count",
                "evaluation_sample_count",
                "calibration_sample_count",
            )
        },
    }
    return CanaryPlan(
        recommended=recommended,
        rankings=tuple(dict(row) for row in rankings),
        hash_bucket=hash_bucket,
        hash_divert=hash_divert,
        gate=gate,
    )


__all__ = [
    "CANARY_ALGORITHM_VERSION",
    "CANARY_HASH_SALT",
    "CanaryBucket",
    "CanaryConfig",
    "CanaryPlan",
    "DEFAULT_ADMISSION_SCAN_CAP",
    "canary_hash_bucket",
    "config_path",
    "plan_canary",
    "read_canary_config",
]
