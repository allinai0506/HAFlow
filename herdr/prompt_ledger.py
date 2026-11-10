"""Immutable snapshots of the exact text HAFlow hands to an agent transport.

The SHA-256 is the UTF-8 encoding of that Python string. A later redaction,
truncation, or screen excerpt is a different value and is never stored as the
snapshot. Command success is DISPATCHED. DELIVERY_VERIFIED means an
untruncated terminal buffer contained that same text. Neither status means
the model received or understood the prompt.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import time
import uuid
from pathlib import Path

from herdr.supervisor.state import redact_text

STATUSES = (
    "GENERATED",
    "DISPATCH_PENDING",
    "DISPATCHED",
    "DELIVERY_VERIFIED",
    "DELIVERY_FAILED",
    "DELIVERY_UNVERIFIED",
)
_EDGES = {
    "GENERATED": {"DISPATCH_PENDING"},
    "DISPATCH_PENDING": {"DISPATCHED", "DELIVERY_FAILED", "DELIVERY_UNVERIFIED"},
    "DISPATCHED": {"DELIVERY_VERIFIED", "DELIVERY_FAILED", "DELIVERY_UNVERIFIED"},
    "DELIVERY_UNVERIFIED": {"DELIVERY_VERIFIED", "DELIVERY_FAILED"},
    "DELIVERY_VERIFIED": set(),
    "DELIVERY_FAILED": set(),
}
_TERMINAL = {"DELIVERY_VERIFIED", "DELIVERY_FAILED"}
_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "RULES.md", "Agents.md", "Claude.md")
_REJECTED_BEFORE_INPUT = ("agent_blocked", "rejected_before_input")
RETENTION_SECONDS = 90 * 24 * 60 * 60
_CONTENT_BUDGET = 4_000_000
_REDACTION_POLICY = "local_operator_reveal"
_EXCERPT = 240


class PromptLedgerError(ValueError):
    pass


class PromptTransportError(RuntimeError):
    def __init__(self, outcome):
        self.outcome = outcome
        super().__init__(outcome["evidence"]["level"])


def prompt_sha256(text):
    if not isinstance(text, str):
        raise PromptLedgerError("prompt content must be the delivered str")
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _canon(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _component_digest(status, payload):
    if status == "UNOBSERVABLE":
        return "UNOBSERVABLE"
    if status != "OBSERVED":
        return "UNKNOWN"
    return hashlib.sha256(_canon(payload).encode("utf-8")).hexdigest()


def _safe_child(base, *parts):
    current = base
    for part in parts:
        current = current / part
        try:
            if current.is_symlink():
                return None
        except OSError:
            return None
    return current


def _file_sha(path):
    try:
        if path is None or path.is_symlink() or not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def observe_composition(task, store=None):
    """Record versions HAFlow can actually read. Hidden prompts stay UNOBSERVABLE."""
    root = str((task or {}).get("clone_path") or "").strip()
    rules, skills = [], []
    rules_status = skills_status = "UNKNOWN"
    if root and Path(root).is_dir():
        rules_status = skills_status = "OBSERVED"
        base = Path(root)
        for name in _INSTRUCTION_FILES:
            digest = _file_sha(_safe_child(base, name))
            if digest:
                rules.append({"path": name, "sha256": digest})
        skill_root = _safe_child(base, ".agents", "skills")
        if skill_root is not None and skill_root.is_dir():
            names = sorted(
                path.name for path in skill_root.iterdir()
                if path.is_dir() and not path.is_symlink()
            )[:32]
            for name in names:
                digest = _file_sha(_safe_child(skill_root, name, "SKILL.md"))
                if digest:
                    skills.append({
                        "name": name,
                        "path": f".agents/skills/{name}/SKILL.md",
                        "sha256": digest,
                    })
    workflow_payload, workflow_status = None, "UNKNOWN"
    workflow_id = (task or {}).get("workflow_id")
    if store is not None and workflow_id:
        try:
            record = store.get_workflow(workflow_id) or {}
            config = record.get("config_json") if "config_json" in record else record.get("config")
            if isinstance(config, str):
                try:
                    config = json.loads(config)
                except ValueError:
                    config = {"raw_sha256": hashlib.sha256(config.encode("utf-8")).hexdigest()}
            if config is not None:
                workflow_payload = {
                    "workflow_id": workflow_id,
                    "config_sha256": hashlib.sha256(_canon(config).encode("utf-8")).hexdigest(),
                }
                workflow_status = "OBSERVED"
        except Exception:
            workflow_status = "UNKNOWN"
    context_id = (task or {}).get("working_context_id") or (task or {}).get("context_id")
    composition = {
        "rules": {"status": rules_status, "files": rules},
        "skills": {"status": skills_status, "files": skills},
        "workflow": {"status": workflow_status, "record": workflow_payload},
        "dynamic_protocol": {
            "status": "OBSERVED",
            "assembler": "herdr.supervisor_delivery.deliver",
            "note": "prompt_content is the final transport text",
        },
        "runtime_context": {
            "status": "OBSERVED" if context_id else "UNKNOWN",
            "working_context_id": context_id or None,
        },
        "agent_global_instructions": {
            "status": "UNOBSERVABLE",
            "reason": "Vendor CLI global instructions are not part of the HAFlow delivery text",
        },
        "model_system_prompt": {
            "status": "UNOBSERVABLE",
            "reason": "The model-internal system prompt is not observable to HAFlow",
        },
    }
    return composition, {
        "rules_digest": _component_digest(rules_status, rules),
        "skills_digest": _component_digest(skills_status, skills),
        "workflow_digest": _component_digest(workflow_status, workflow_payload),
    }


def classify_transport_result(*, returncode, stdout="", stderr="", timed_out=False, errno_name=None):
    detail = redact_text(f"{stdout or ''}\n{stderr or ''}")[:500]
    evidence = {
        "level": "transport_unknown",
        "model_receipt": "UNOBSERVABLE",
        "tty_channel": "NOT_CONFIRMED",
        "detail": detail,
        "returncode": returncode,
        "timed_out": bool(timed_out),
    }
    if errno_name == "E2BIG":
        evidence["level"] = "argv_rejected"
        evidence["channel_bytes"] = "NONE"
        return {"status": "DELIVERY_FAILED", "evidence": evidence}
    if timed_out:
        evidence["level"] = "transport_timeout"
        evidence["channel_bytes"] = "UNKNOWN"
        return {"status": "DELIVERY_UNVERIFIED", "evidence": evidence}
    blob = f"{stdout or ''}\n{stderr or ''}"
    if returncode not in (0, None) and any(token in blob for token in _REJECTED_BEFORE_INPUT):
        evidence["level"] = "rejected_before_input"
        evidence["channel_bytes"] = "NONE"
        return {"status": "DELIVERY_FAILED", "evidence": evidence}
    if returncode == 0:
        evidence["level"] = "transport_command_accepted"
        return {"status": "DISPATCHED", "evidence": evidence}
    evidence["channel_bytes"] = "UNKNOWN"
    return {"status": "DELIVERY_UNVERIFIED", "evidence": evidence}


def raise_for_transport(result):
    outcome = classify_transport_result(
        returncode=getattr(result, "returncode", None),
        stdout=getattr(result, "stdout", "") or "",
        stderr=getattr(result, "stderr", "") or "",
    )
    if outcome["status"] != "DISPATCHED":
        raise PromptTransportError(outcome)
    return outcome


def assess_tty_buffer(prompt, pane_text, *, truncated=False):
    if truncated:
        return {"verified": False, "reason": "pane_read_truncated"}
    if isinstance(prompt, str) and prompt and isinstance(pane_text, str) and prompt in pane_text:
        return {"verified": True, "reason": "tty_buffer_contains_full_prompt"}
    return {"verified": False, "reason": "tty_echo_not_observed"}


def upgrade_with_tty_buffer(outcome, prompt, pane_text, *, truncated=False):
    assessment = assess_tty_buffer(prompt, pane_text, truncated=truncated)
    evidence = dict(outcome["evidence"])
    evidence["tty_channel"] = assessment["reason"]
    if assessment["verified"] and outcome["status"] in {"DISPATCHED", "DELIVERY_UNVERIFIED"}:
        evidence["level"] = "tty_channel_accepted"
        evidence["model_receipt"] = "UNOBSERVABLE"
        return {"status": "DELIVERY_VERIFIED", "evidence": evidence}
    return {"status": outcome["status"], "evidence": evidence}


def apply_readiness(outcome, *, interactive_ready=None, agent_status=None):
    evidence = dict(outcome["evidence"])
    evidence["agent_status"] = agent_status if agent_status is not None else "UNKNOWN"
    evidence["interactive_ready"] = interactive_ready if interactive_ready is not None else "UNKNOWN"
    status = outcome["status"]
    not_ready = interactive_ready is False or agent_status in {"launch_pending", "starting"}
    if not_ready and status == "DELIVERY_VERIFIED":
        status = "DISPATCHED"
        evidence["level"] = "transport_command_accepted"
        evidence["tty_channel"] = "NOT_CONFIRMED"
        evidence["verification_withheld"] = "agent_not_interactive_ready"
    return {"status": status, "evidence": evidence}


def legacy_terminal_outcome(
    *, primary_returncode, timed_out, screen, prompt, fallback, agent_status=None, screen_truncated=True,
):
    """Classify the pre-receipt dispatch tail without changing its control flow.

    screen_truncated defaults to true because `herdr pane read` text output does
    not prove the buffer was complete. Callers pass false only with that proof.
    """
    if fallback == "e2big":
        return classify_transport_result(returncode=1, errno_name="E2BIG")
    if fallback == "rejected":
        outcome = {
            "status": "DELIVERY_FAILED",
            "evidence": {
                "level": "send_text_command_rejected",
                "model_receipt": "UNOBSERVABLE",
                "tty_channel": "NOT_CONFIRMED",
                "channel_bytes": "UNKNOWN",
                "returncode": primary_returncode,
                "timed_out": bool(timed_out),
            },
        }
        return apply_readiness(outcome, agent_status=agent_status)
    if fallback == "accepted":
        outcome = classify_transport_result(returncode=0)
        outcome["evidence"]["level"] = "send_text_command_accepted"
    elif timed_out or primary_returncode == 124:
        outcome = classify_transport_result(returncode=124, timed_out=True)
    elif primary_returncode == 0:
        outcome = classify_transport_result(returncode=0)
    else:
        outcome = classify_transport_result(returncode=primary_returncode, timed_out=bool(timed_out))
    outcome = upgrade_with_tty_buffer(outcome, prompt, screen or "", truncated=bool(screen_truncated))
    return apply_readiness(outcome, agent_status=agent_status)


def _warn(exc):
    print(f"[PROMPT LEDGER] {type(exc).__name__}: {exc}", flush=True)


def _connect(store):
    from herdr import state_db
    return state_db.get_db_connection(Path(store.db_path))


def _identity(task):
    return {
        "task_id": task.get("task_id"),
        "workflow_id": task.get("workflow_id"),
        "run_id": task.get("run_id"),
        "node_id": task.get("node") or task.get("stage"),
        "agent_id": task.get("agent"),
    }


def _emit(conn, task, prompt_id, from_status, to_status, evidence):
    from herdr import state_db
    identity = _identity(task)
    state_db.record_event({
        "workflow_id": identity["workflow_id"],
        "node_id": identity["node_id"],
        "task_id": identity["task_id"],
        "agent_id": identity["agent_id"],
        "event_type": "prompt_ledger_transition",
        "source": "prompt-ledger",
        "run_id": identity["run_id"],
        "payload": {
            "prompt_id": prompt_id,
            "from_status": from_status,
            "to_status": to_status,
            "evidence_level": (evidence or {}).get("level"),
            "model_receipt": "UNOBSERVABLE",
        },
    }, conn=conn)


def _row(conn, prompt_id):
    found = conn.execute("SELECT * FROM prompt_snapshots WHERE prompt_id=?", (prompt_id,)).fetchone()
    return dict(found) if found else None


def _find_intervention(conn, task, intervention_id):
    if not intervention_id:
        return None
    found = conn.execute(
        "SELECT * FROM prompt_snapshots WHERE task_id=? AND ifnull(run_id,'')=? AND intervention_id=?",
        (task.get("task_id"), task.get("run_id") or "", intervention_id),
    ).fetchone()
    return dict(found) if found else None


def _instance_name(task):
    """Worker instance recorded at launch. The agent type is not that name."""
    from herdr.runtime_state import get_task_runtime

    runtime = get_task_runtime(task) or {}
    for value in (runtime.get("agent_name"), task.get("agent_name")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "UNKNOWN"


def record_generated(conn, *, task, prompt, action, intervention_id, store=None):
    if not isinstance(prompt, str):
        raise PromptLedgerError("refusing to snapshot a non-str prompt")
    encoded = prompt.encode("utf-8", "surrogatepass")
    if len(encoded) > _CONTENT_BUDGET:
        raise PromptLedgerError("prompt exceeds the ledger content budget")
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _find_intervention(conn, task, intervention_id)
        if existing:
            if existing["prompt_sha256"] != prompt_sha256(prompt):
                raise PromptLedgerError("intervention already has a different immutable snapshot")
            if own:
                conn.commit()
            return existing
        composition, digests = observe_composition(task, store)
        composition["dynamic_protocol"]["assembler"] = (
            "herdr.task_cli_execution.handle_dispatch_task"
            if action == "LEGACY"
            else "herdr.supervisor_delivery.deliver"
        )
        now = time.time()
        prompt_id = "ps_" + uuid.uuid4().hex
        identity = _identity(task)
        conn.execute(
            """INSERT INTO prompt_snapshots (
                prompt_id, task_id, workflow_id, run_id, node_id, agent_kind, agent_name,
                candidate_sha, prompt_content, prompt_sha256, content_bytes, created_at,
                dispatch_status, delivery_evidence_json, rules_digest, skills_digest,
                workflow_digest, composition_json, action, intervention_id, redaction_policy,
                retain_until, content_purged
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (
                prompt_id, identity["task_id"], identity["workflow_id"], identity["run_id"], identity["node_id"],
                task.get("agent_kind") or task.get("agent") or "UNKNOWN",
                _instance_name(task),
                task.get("candidate_sha") or "",
                prompt, prompt_sha256(prompt), len(encoded), now, "GENERATED",
                _canon({"level": "none", "model_receipt": "UNOBSERVABLE"}),
                digests["rules_digest"], digests["skills_digest"], digests["workflow_digest"],
                _canon(composition), action, intervention_id or None, _REDACTION_POLICY,
                int(now) + RETENTION_SECONDS,
            ),
        )
        _emit(conn, task, prompt_id, None, "GENERATED", {"level": "none"})
        if own:
            conn.commit()
        return _row(conn, prompt_id)
    except Exception:
        if own:
            conn.rollback()
        raise


def _path(source, target):
    if source == target:
        return []
    if target not in STATUSES or source not in _EDGES:
        raise PromptLedgerError(f"unknown prompt status {source}->{target}")
    queue = [(source, [])]
    seen = {source}
    while queue:
        node, steps = queue.pop(0)
        for nxt in _EDGES.get(node, ()):
            path = steps + [nxt]
            if nxt == target:
                return path
            if nxt not in seen and nxt not in _TERMINAL:
                seen.add(nxt)
                queue.append((nxt, path))
    raise PromptLedgerError(f"illegal prompt transition {source}->{target}")


def _transition_one(conn, task, row, to_status, evidence):
    if to_status not in _EDGES.get(row["dispatch_status"], ()):
        raise PromptLedgerError(f"illegal prompt transition {row['dispatch_status']}->{to_status}")
    dispatched_at = row["dispatched_at"]
    if to_status == "DISPATCH_PENDING" and dispatched_at is None:
        dispatched_at = time.time()
    cursor = conn.execute(
        """UPDATE prompt_snapshots
           SET dispatch_status=?, dispatched_at=?, delivery_evidence_json=?
           WHERE prompt_id=? AND dispatch_status=?""",
        (to_status, dispatched_at, _canon(evidence), row["prompt_id"], row["dispatch_status"]),
    )
    if cursor.rowcount not in (1, -1):
        raise PromptLedgerError("prompt status changed before transition")
    fresh = _row(conn, row["prompt_id"])
    if fresh["dispatch_status"] != to_status:
        raise PromptLedgerError("prompt status changed before transition")
    _emit(conn, task, row["prompt_id"], row["dispatch_status"], to_status, evidence)
    return fresh


def apply_outcome(store, prompt_id, outcome, *, task):
    if not prompt_id:
        return None
    target = outcome["status"]
    evidence = outcome.get("evidence") or {"level": "none", "model_receipt": "UNOBSERVABLE"}
    conn = _connect(store)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _row(conn, prompt_id)
        if row is None:
            raise PromptLedgerError("prompt snapshot not found")
        steps = _path(row["dispatch_status"], target)
        interim = {"level": "none", "model_receipt": "UNOBSERVABLE", "intermediate": True}
        for index, step in enumerate(steps):
            if index == len(steps) - 1:
                step_evidence = evidence
            elif step == "DISPATCHED":
                step_evidence = {
                    "level": "transport_command_accepted",
                    "model_receipt": "UNOBSERVABLE",
                    "intermediate": True,
                }
            else:
                step_evidence = interim
            row = _transition_one(conn, task, row, step, step_evidence)
        conn.commit()
        return _public(row, reveal=False)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_transport_ambiguous(store, prompt_id, *, task):
    if not prompt_id:
        return None
    conn = _connect(store)
    try:
        row = _row(conn, prompt_id)
    finally:
        conn.close()
    if row is None or row["dispatch_status"] in {
        "DISPATCHED", "DELIVERY_VERIFIED", "DELIVERY_FAILED", "DELIVERY_UNVERIFIED",
    }:
        return _public(row, reveal=False) if row else None
    return apply_outcome(store, prompt_id, {
        "status": "DELIVERY_UNVERIFIED",
        "evidence": {
            "level": "transport_outcome_unknown",
            "model_receipt": "UNOBSERVABLE",
            "tty_channel": "NOT_CONFIRMED",
            "channel_bytes": "UNKNOWN",
        },
    }, task=task)


def open_snapshot(store, task, prompt, *, action, intervention_id):
    conn = _connect(store)
    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        row = record_generated(
            conn, task=task, prompt=prompt, action=action,
            intervention_id=intervention_id, store=store,
        )
        conn.commit()
        return row
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _in_savepoint(conn, fn):
    name = "prompt_ledger_" + uuid.uuid4().hex[:8]
    conn.execute(f"SAVEPOINT {name}")
    try:
        result = fn()
    except Exception:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    conn.execute(f"RELEASE {name}")
    return result


class DeliveryHook:
    """Best-effort ledger beside receipt delivery. It must not change that delivery."""

    def __init__(self, store, task, action, intervention_id):
        self.store = store
        self.task = task
        self.action = action
        self.intervention_id = intervention_id
        self.prompt_id = None

    def on_prepared(self, conn, body):
        try:
            row = _in_savepoint(conn, lambda: record_generated(
                conn, task=self.task, prompt=body["prompt"], action=self.action,
                intervention_id=self.intervention_id, store=self.store,
            ))
            self.prompt_id = row["prompt_id"]
            body["prompt_id"] = row["prompt_id"]
            body["prompt_sha256"] = row["prompt_sha256"]
        except Exception as exc:
            _warn(exc)

    def _remember(self):
        if self.prompt_id:
            return
        conn = _connect(self.store)
        try:
            row = _find_intervention(conn, self.task, self.intervention_id)
        finally:
            conn.close()
        if row:
            self.prompt_id = row["prompt_id"]

    def on_ambiguous(self, prompt):
        try:
            self._remember()
            if self.prompt_id is None and isinstance(prompt, str):
                row = open_snapshot(
                    self.store, self.task, prompt, action=self.action,
                    intervention_id=self.intervention_id,
                )
                self.prompt_id = row["prompt_id"]
            mark_transport_ambiguous(self.store, self.prompt_id, task=self.task)
        except Exception as exc:
            _warn(exc)

    def on_pending(self):
        try:
            self._remember()
            apply_outcome(self.store, self.prompt_id, {
                "status": "DISPATCH_PENDING",
                "evidence": {"level": "none", "model_receipt": "UNOBSERVABLE"},
            }, task=self.task)
        except Exception as exc:
            _warn(exc)

    def on_exception(self, exc):
        try:
            self._remember()
            if isinstance(exc, PromptTransportError):
                outcome = exc.outcome
            else:
                import errno
                import subprocess
                if isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.E2BIG:
                    outcome = classify_transport_result(
                        returncode=1, stderr=str(exc), errno_name="E2BIG",
                    )
                elif isinstance(exc, subprocess.TimeoutExpired):
                    outcome = classify_transport_result(
                        returncode=124,
                        stdout=getattr(exc, "stdout", "") or "",
                        stderr=getattr(exc, "stderr", "") or "",
                        timed_out=True,
                    )
                else:
                    outcome = {
                        "status": "DELIVERY_UNVERIFIED",
                        "evidence": {
                            "level": "transport_unknown",
                            "model_receipt": "UNOBSERVABLE",
                            "tty_channel": "NOT_CONFIRMED",
                            "channel_bytes": "UNKNOWN",
                            "detail": redact_text(str(exc))[:500],
                        },
                    }
            apply_outcome(self.store, self.prompt_id, outcome, task=self.task)
        except Exception as inner:
            _warn(inner)

    def on_success(self, result):
        try:
            self._remember()
            if isinstance(result, dict) and result.get("status") in STATUSES:
                outcome = {"status": result["status"], "evidence": result.get("evidence") or {}}
            else:
                outcome = classify_transport_result(returncode=0)
            apply_outcome(self.store, self.prompt_id, outcome, task=self.task)
        except Exception as exc:
            _warn(exc)

    def reconcile_dispatched(self):
        """Catch a snapshot left behind after the send callable already returned."""
        try:
            self._remember()
            if not self.prompt_id:
                return
            conn = _connect(self.store)
            try:
                row = _row(conn, self.prompt_id)
            finally:
                conn.close()
            if row and row["dispatch_status"] in {"GENERATED", "DISPATCH_PENDING"}:
                apply_outcome(
                    self.store, self.prompt_id, classify_transport_result(returncode=0), task=self.task,
                )
        except Exception as exc:
            _warn(exc)


def _public(row, *, reveal):
    if row is None:
        return None
    data = dict(row)
    data["delivery_evidence"] = json.loads(data.pop("delivery_evidence_json") or "{}")
    data["composition"] = json.loads(data.pop("composition_json") or "{}")
    data["content_redacted"] = not reveal
    if not reveal:
        data["delivery_evidence"].pop("detail", None)
        content = "" if data.get("content_purged") else (data.get("prompt_content") or "")
        data["prompt_excerpt"] = redact_text(content)[:_EXCERPT]
        data.pop("prompt_content", None)
    return data


def _query(store, sql, params):
    conn = _connect(store)
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def list_snapshots(store, *, task_id=None, workflow_id=None, node_id=None, limit=100):
    clauses, params = [], []
    if task_id:
        clauses.append("task_id=?")
        params.append(task_id)
    if workflow_id:
        clauses.append("workflow_id=?")
        params.append(workflow_id)
    if node_id:
        clauses.append("node_id=?")
        params.append(node_id)
    if not clauses:
        raise PromptLedgerError("prompt query requires task_id, workflow_id, or node_id")
    params.append(int(limit))
    rows = _query(
        store,
        "SELECT * FROM prompt_snapshots WHERE " + " AND ".join(clauses)
        + " ORDER BY created_at ASC, prompt_id ASC LIMIT ?",
        params,
    )
    return [_public(row, reveal=False) for row in rows]


def get_snapshot(store, prompt_id, *, reveal=False):
    rows = _query(store, "SELECT * FROM prompt_snapshots WHERE prompt_id=?", (prompt_id,))
    return _public(rows[0], reveal=reveal) if rows else None


def verify_snapshot(store, prompt_id):
    row = get_snapshot(store, prompt_id, reveal=True)
    if row is None:
        raise PromptLedgerError("prompt snapshot not found")
    if row.get("content_purged"):
        return {
            "prompt_id": prompt_id,
            "status": "HASH_RETAINED_CONTENT_PURGED",
            "recomputable": False,
            "prompt_sha256": row["prompt_sha256"],
        }
    actual = prompt_sha256(row["prompt_content"])
    return {
        "prompt_id": prompt_id,
        "ok": actual == row["prompt_sha256"],
        "recomputable": True,
        "expected": row["prompt_sha256"],
        "actual": actual,
        "dispatch_status": row["dispatch_status"],
    }


def diff_prompts(store, left_id, right_id, *, reveal=False):
    left = get_snapshot(store, left_id, reveal=True)
    right = get_snapshot(store, right_id, reveal=True)
    if left is None or right is None:
        raise PromptLedgerError("prompt snapshot not found")
    result = {
        "left_id": left_id,
        "right_id": right_id,
        "left_sha256": left["prompt_sha256"],
        "right_sha256": right["prompt_sha256"],
        "content_available": not (left.get("content_purged") or right.get("content_purged")),
    }
    if not result["content_available"]:
        result["equal"] = None
        result["diff"] = None
        return result
    result["equal"] = left["prompt_content"] == right["prompt_content"]
    if not reveal:
        result["diff"] = None
        result["access"] = "reveal_required"
        return result
    text = "".join(difflib.unified_diff(
        left["prompt_content"].splitlines(keepends=True),
        right["prompt_content"].splitlines(keepends=True),
        fromfile=left_id, tofile=right_id,
    ))
    result["diff"] = text[:20000]
    result["diff_truncated"] = len(text) > 20000
    return result


def snapshot_history(store, prompt_id):
    row = get_snapshot(store, prompt_id, reveal=False)
    if row is None:
        raise PromptLedgerError("prompt snapshot not found")
    conn = _connect(store)
    try:
        found = conn.execute(
            """SELECT id, timestamp, payload_json FROM events
               WHERE task_id=? AND event_type='prompt_ledger_transition'
                 AND json_extract(payload_json, '$.prompt_id')=?
               ORDER BY id ASC LIMIT 200""",
            (row["task_id"], prompt_id),
        ).fetchall()
    finally:
        conn.close()
    return {
        "prompt_id": prompt_id,
        "dispatch_status": row["dispatch_status"],
        "transitions": [
            {
                "id": item["id"],
                "timestamp": item["timestamp"],
                "payload": json.loads(item["payload_json"] or "{}"),
            }
            for item in found
        ],
    }


def task_usage(store, task_id):
    rows = list_snapshots(store, task_id=task_id, limit=200)
    return {
        "task_id": task_id,
        "snapshots": [
            {
                "prompt_id": row["prompt_id"],
                "agent_kind": row["agent_kind"],
                "agent_name": row["agent_name"],
                "rules_digest": row["rules_digest"],
                "skills_digest": row["skills_digest"],
                "workflow_digest": row["workflow_digest"],
                "dispatch_status": row["dispatch_status"],
                "candidate_sha": row["candidate_sha"],
                "model_system_prompt": "UNOBSERVABLE",
                "agent_global_instructions": "UNOBSERVABLE",
            }
            for row in rows
        ],
    }


def purge_expired(store, now=None):
    now = time.time() if now is None else now
    # The trigger compares retain_until to SQLite's integer second. Stay one
    # second behind that clock, and do not let one rejected row undo the rest.
    cutoff = int(now) - 1
    conn = _connect(store)
    cleared = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """SELECT prompt_id FROM prompt_snapshots
               WHERE content_purged=0 AND retain_until IS NOT NULL AND retain_until<=?""",
            (cutoff,),
        ).fetchall()
        for item in rows:
            name = "prompt_purge_" + uuid.uuid4().hex[:8]
            conn.execute(f"SAVEPOINT {name}")
            try:
                conn.execute(
                    """UPDATE prompt_snapshots
                       SET prompt_content='', content_purged=1
                       WHERE prompt_id=? AND content_purged=0""",
                    (item["prompt_id"],),
                )
            except Exception:
                conn.execute(f"ROLLBACK TO {name}")
                conn.execute(f"RELEASE {name}")
                continue
            conn.execute(f"RELEASE {name}")
            cleared += 1
        conn.commit()
        return cleared
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cli_prompt_ledger(args):
    import sys
    from herdr.state_store import get_state_store
    store = get_state_store()
    command = args.ledger_command
    try:
        if command == "list":
            payload = list_snapshots(
                store, task_id=args.task_id, workflow_id=args.workflow_id, node_id=args.node_id,
            )
        elif command == "get":
            payload = get_snapshot(store, args.prompt_id, reveal=bool(args.reveal))
            if payload is None:
                raise PromptLedgerError("prompt snapshot not found")
        elif command == "verify":
            payload = verify_snapshot(store, args.prompt_id)
        elif command == "diff":
            payload = diff_prompts(store, args.left, args.right, reveal=bool(args.reveal))
        elif command == "history":
            payload = snapshot_history(store, args.prompt_id)
        elif command == "usage":
            payload = task_usage(store, args.task_id)
        else:
            raise PromptLedgerError(f"unknown prompt-ledger command {command}")
    except PromptLedgerError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
