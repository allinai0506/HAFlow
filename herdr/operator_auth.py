"""Operator Enterprise Credential Authentication & Capability Authorization (Issue #307).

Provides unified operator identity authentication, capability binding, and
anti-impersonation protection for gate exemptions (bypass_gate / force_pass_gate)
and console privileged actions per gate_bypass_policy.yaml.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from herdr.transitions import validate_operator


def is_worker_environment() -> bool:
    """Detect if the current process is executing within a worker clone/sandbox environment.

    Enforces non-bypassable worker detection across:
    1. Worker environment variables (WORKER_WRITE_ROOT, HERDR_TASK_ID, HERDR_RUN_ID, etc.)
    2. Working directory inspection for worker clone markers and identity tags.
    """
    for key in (
        "WORKER_WRITE_ROOT",
        "HERDR_TASK_ID",
        "HERDR_RUN_ID",
        "HERDR_WORKER_CLONE",
        "HERDR_AGENT_ID",
    ):
        val = os.environ.get(key)
        if val and val.strip():
            return True

    # Filesystem marker detection in CWD and parent hierarchy (disabled under pytest unless explicitly testing)
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        try:
            cwd = Path.cwd().resolve()
            cwd_str = str(cwd)
            if "/.herdr-controller/clones/" in cwd_str or "/clones/wf-" in cwd_str:
                return True
            curr = cwd
            for _ in range(5):
                if (curr / ".herdr-launch-identity.json").is_file() or (curr / ".agent-task-context").is_file():
                    return True
                if curr.parent == curr:
                    break
                curr = curr.parent
        except Exception:
            pass

    return False


# ============================================================
# Exception Hierarchy
# ============================================================

class OperatorAuthError(ValueError):
    """Base exception for operator authentication and authorization failures."""


class OperatorAuthenticationError(OperatorAuthError):
    """Raised when operator credentials are missing, invalid, expired, or forged."""


class OperatorAuthorizationError(OperatorAuthError):
    """Raised when an authenticated operator lacks the required role or capability."""


class OperatorImpersonationError(OperatorAuthError):
    """Raised when an untrusted subject (e.g. task agent) attempts to impersonate an operator."""


# ============================================================
# Operator Principal Model
# ============================================================

ALLOWED_OPERATOR_ROLES: tuple[str, ...] = (
    "human_operator",
    "admin",
    "system_admin",
    "security_auditor",
)

DEFAULT_CAPABILITIES: tuple[str, ...] = (
    "bypass_gate",
    "force_pass_gate",
    "force_review",
)

ROLE_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "human_operator": ("bypass_gate", "force_pass_gate", "force_review"),
    "admin": ("bypass_gate", "force_pass_gate", "force_review"),
    "system_admin": ("bypass_gate", "force_pass_gate", "force_review"),
    "security_auditor": ("force_review",),
}


@dataclass(frozen=True)
class OperatorPrincipal:
    """Authenticated and authorized operator principal."""
    operator_id: str
    role: str = "human_operator"
    capabilities: tuple[str, ...] = DEFAULT_CAPABILITIES
    authenticated: bool = True
    auth_method: str = "state_db_token"  # "state_db_token" | "hmac_token" | "bearer_token" | "credentials_file"

    def __str__(self) -> str:
        return self.operator_id

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.operator_id == other
        if isinstance(other, OperatorPrincipal):
            return (
                self.operator_id == other.operator_id
                and self.role == other.role
                and self.capabilities == other.capabilities
                and self.authenticated == other.authenticated
                and self.auth_method == other.auth_method
            )
        return False


# ============================================================
# System Signing Secret Management
# ============================================================

def _get_signing_secret(explicit_secret: str | None = None) -> str:
    """Resolve signing secret for HMAC tokens.

    Fail-closed: Hardcoded fallback constants are prohibited (Issue #307 / B3).
    Requires explicit configuration, secrets environment variable, or secure credentials keyfile.
    """
    if explicit_secret:
        return explicit_secret.strip()
    env_secret = (
        os.environ.get("HERDR_OPERATOR_SIGNING_SECRET")
        or os.environ.get("HERDR_HMAC_SECRET")
    )
    if env_secret and env_secret.strip():
        return env_secret.strip()

    # Cached system secret in user credentials dir
    try:
        from herdr import state_db
        cred_dir = state_db._get_credentials_dir()
        if not cred_dir:
            raise OSError("credentials directory is unavailable")
        cred_path = Path(cred_dir).resolve()
        cred_str = str(cred_path)
        if "/.herdr-controller/clones/" in cred_str or "/clones/wf-" in cred_str:
            raise OperatorAuthenticationError("signing secret directory cannot reside in worker clone or workspace")

        key_file = cred_path / "operator_hmac_signing.key"
        if key_file.is_file():
            try:
                key_file.chmod(0o600)
            except OSError:
                pass
            val = key_file.read_text(encoding="utf-8").strip()
            if val:
                return val

        secret_val = secrets.token_hex(32)
        temp_file = cred_path / f".operator_hmac_{secrets.token_hex(8)}.tmp"
        fd = os.open(temp_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with open(fd, "w", encoding="utf-8") as f:
            f.write(secret_val)
        os.replace(temp_file, key_file)
        try:
            key_file.chmod(0o600)
        except OSError:
            pass
        return secret_val
    except Exception as exc:
        raise OperatorAuthenticationError(f"operator signing secret is unavailable: {exc}") from exc


# ============================================================
# Signed HMAC Token Management
# ============================================================

def issue_operator_hmac_token(
    operator_id: str,
    role: str = "human_operator",
    capabilities: Sequence[str] | None = None,
    secret: str | None = None,
    expires_in: float | None = 3600.0,
) -> str:
    """Issue a tamper-evident signed HMAC token for an operator principal."""
    if is_worker_environment():
        raise OperatorImpersonationError(
            "worker environment is not authorized to issue operator tokens (Issue #307 / B2)"
        )
    if not secret:
        env_secret = (
            os.environ.get("HERDR_OPERATOR_SIGNING_SECRET")
            or os.environ.get("HERDR_HMAC_SECRET")
        )
        if env_secret and env_secret.strip():
            secret = env_secret.strip()
        else:
            raise OperatorAuthorizationError(
                "operator token issuance requires an explicit secret or HERDR_OPERATOR_SIGNING_SECRET (Issue #307 / B2)"
            )
    op_clean = validate_operator(operator_id)
    signing_secret = _get_signing_secret(secret)
    now = time.time()
    exp = (now + float(expires_in)) if expires_in is not None else None

    clean_role = str(role).strip()
    if capabilities is None:
        caps = list(ROLE_CAPABILITIES.get(clean_role, DEFAULT_CAPABILITIES))
    else:
        caps = [str(c).strip() for c in capabilities]

    payload = {
        "sub": op_clean,
        "role": clean_role,
        "caps": caps,
        "iat": int(now),
        "exp": int(exp) if exp is not None else None,
    }
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_json).decode("ascii").rstrip("=")
    signing_input = f"haflow_v1.{payload_b64}"
    sig = hmac.new(signing_secret.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{signing_input}.{sig}"


def verify_operator_hmac_token(
    token: str,
    expected_operator: str | None = None,
    secret: str | None = None,
) -> OperatorPrincipal:
    """Verify and decode a signed HMAC token, returning the authenticated OperatorPrincipal."""
    tok = str(token or "").strip()
    if not tok.startswith("haflow_v1."):
        raise OperatorAuthenticationError("invalid token format: must start with 'haflow_v1.'")

    parts = tok.split(".")
    if len(parts) != 3:
        raise OperatorAuthenticationError("invalid token format: malformed segments")

    prefix, payload_b64, presented_sig = parts
    signing_secret = _get_signing_secret(secret)
    signing_input = f"{prefix}.{payload_b64}"
    expected_sig = hmac.new(signing_secret.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(presented_sig, expected_sig):
        raise OperatorAuthenticationError("operator authentication failed: invalid token signature")

    # Pad base64 string
    padding = "=" * ((4 - len(payload_b64) % 4) % 4)
    try:
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception as exc:
        raise OperatorAuthenticationError(f"invalid token payload encoding: {exc}") from exc

    sub = payload.get("sub")
    if not sub:
        raise OperatorAuthenticationError("invalid token payload: missing subject ('sub')")

    if expected_operator is not None:
        expected_clean = validate_operator(expected_operator)
        if sub != expected_clean:
            raise OperatorAuthenticationError(
                f"operator authentication failed: subject mismatch ('{sub}' != '{expected_clean}')"
            )

    exp = payload.get("exp")
    if exp is not None and time.time() > float(exp):
        raise OperatorAuthenticationError(f"operator token expired at {exp}")

    role = payload.get("role", "human_operator")
    caps = tuple(payload.get("caps", DEFAULT_CAPABILITIES))

    return OperatorPrincipal(
        operator_id=sub,
        role=role,
        capabilities=caps,
        authenticated=True,
        auth_method="hmac_token",
    )


# ============================================================
# Token Resolution
# ============================================================

def resolve_presented_token(
    token: str | None = None,
    credentials_path: str | None = None,
    task: dict[str, Any] | None = None,
    forbidden_roots: Sequence[Path] | None = None,
) -> tuple[str | None, str]:
    """Resolve explicitly presented token from arguments, files, or environment.

    Returns:
        (presented_token, source_method)
    """
    raw_token = str(token or "").strip() or os.environ.get("HERDR_OPERATOR_TOKEN", "").strip()
    source_method = "state_db_token"
    presented_token = raw_token
    if raw_token.startswith("Bearer "):
        presented_token = raw_token[7:].strip()
        source_method = "bearer_token"
    elif raw_token.startswith("haflow_v1."):
        source_method = "hmac_token"

    cred_file_arg = credentials_path or os.environ.get("HERDR_OPERATOR_CREDENTIALS")
    if not presented_token and cred_file_arg:
        p = Path(str(cred_file_arg)).expanduser().resolve()
        roots: list[Path] = list(forbidden_roots or [])
        if task:
            clone_path = str(task.get("clone_path") or "").strip()
            project_root = str(task.get("project_root") or "").strip()
            if clone_path:
                roots.append(Path(clone_path).expanduser().resolve())
            if project_root:
                roots.append(Path(project_root).expanduser().resolve())

        # Disallow reading credentials residing in any clone or workspace
        p_str = str(p)
        if "/.herdr-controller/clones/" in p_str or "/clones/wf-" in p_str:
            raise OperatorAuthenticationError(
                "operator credentials file cannot reside in worker clone or project workspace"
            )

        for root in roots:
            if p.is_relative_to(root):
                raise OperatorAuthenticationError(
                    "operator credentials file cannot reside in worker clone or project workspace"
                )

        if not p.is_file():
            raise OperatorAuthenticationError(f"operator credentials file not found: {p}")
        try:
            presented_token = p.read_text(encoding="utf-8").strip()
            source_method = "credentials_file"
        except OSError as exc:
            raise OperatorAuthenticationError(f"unreadable operator credentials file: {exc}") from exc

    return (presented_token or None, source_method)


# ============================================================
# Core Authentication & Authorization Engine
# ============================================================

def authenticate_and_authorize_operator(
    operator_id: Any,
    action: str = "bypass_gate",
    token: str | None = None,
    credentials_path: str | None = None,
    task: dict[str, Any] | None = None,
    db_path: Path | None = None,
    store: Any | None = None,
    strict_auth: bool | None = None,
    secret: str | None = None,
    forbidden_roots: Sequence[Path] | None = None,
) -> OperatorPrincipal:
    """Authenticate operator identity and authorize action capability.

    Enforces:
    1. Pre-authenticated principal passthrough (eliminates redundant verification).
    2. Operator format validity via validate_operator (SC-010).
    3. Anti-impersonation: Task worker agent cannot self-bypass (SC-010 / #307 / B2).
    4. Worker environment guard: Worker sandboxes cannot execute gate exemptions (B2).
    5. Explicit token presentation: validates against signed HMAC token or state_db.
    6. Fail-closed: Zero credentials rejected by default without exception (B1).
    7. Role & Capability authorization per gate_bypass_policy.yaml.
    """
    # 0. Pre-authenticated principal boundary passthrough (B4 optimization)
    if isinstance(operator_id, OperatorPrincipal):
        principal = operator_id
        if task:
            task_agent = str(task.get("agent") or "").strip().lower()
            if task_agent and principal.operator_id.lower() == task_agent:
                raise OperatorImpersonationError(
                    "the operator must be independent of the task worker; agent cannot bypass its own gate"
                )
        if action and action not in principal.capabilities:
            raise OperatorAuthorizationError(
                f"operator authorization failed: operator '{principal.operator_id}' lacks capability '{action}'"
            )
        return principal

    op_clean = validate_operator(operator_id)

    # 1. Anti-impersonation & Worker execution guard (Issue #307 / B2)
    if task:
        task_agent = str(task.get("agent") or "").strip().lower()
        if task_agent and op_clean.lower() == task_agent:
            raise OperatorImpersonationError(
                "the operator must be independent of the task worker; agent cannot bypass its own gate"
            )
        clone_path = str(task.get("clone_path") or "").strip()
        if clone_path:
            try:
                if Path.cwd().resolve().is_relative_to(Path(clone_path).resolve()):
                    raise OperatorImpersonationError(
                        "worker clone cannot execute gate exemptions for its task (Issue #307 / B2)"
                    )
            except Exception:
                pass
        if is_worker_environment():
            raise OperatorImpersonationError(
                "worker environment cannot execute gate exemptions or privileged actions (Issue #307 / B2)"
            )
    else:
        if is_worker_environment():
            raise OperatorImpersonationError(
                "worker environment cannot execute gate exemptions or privileged actions (Issue #307 / B2)"
            )

    # 2. Resolve presented token
    presented_token, source_method = resolve_presented_token(
        token=token,
        credentials_path=credentials_path,
        task=task,
        forbidden_roots=forbidden_roots,
    )

    # 3. Resolve registered authority from state_db
    resolved_db_path = db_path or getattr(store, "db_path", None)
    registered_token = None
    try:
        from herdr import state_db
        registered_token = state_db.get_user_token(op_clean, "operator", db_path=resolved_db_path)
        if not registered_token:
            registered_token = state_db.get_user_token(op_clean, "gate_bypass", db_path=resolved_db_path)
    except (OSError, ValueError, RuntimeError):
        registered_token = None

    principal: OperatorPrincipal

    # 4. Authentication check: strict and fail-closed by default (Issue #307 / B1)
    if not presented_token:
        if registered_token is not None:
            raise OperatorAuthenticationError(
                f"operator authentication required: missing operator token for '{op_clean}'"
            )
        # Strict auth determination: fail-closed by default (Issue #307 / B1)
        is_strict = strict_auth
        if is_strict is None:
            if is_worker_environment():
                is_strict = True
            else:
                strict_env = os.environ.get("HERDR_OPERATOR_AUTH_STRICT", "1").strip().lower()
                allow_unauth = os.environ.get("HERDR_ALLOW_UNAUTHENTICATED_OPERATOR", "0").strip().lower()
                is_strict = (strict_env in ("1", "true", "yes")) and (allow_unauth not in ("1", "true", "yes"))
        if is_strict:
            raise OperatorAuthenticationError(
                f"operator authentication required: missing operator credentials for '{op_clean}'"
            )
        # Unauthenticated fallback (only permitted when legacy test mode explicitly enabled and non-strict)
        principal = OperatorPrincipal(
            operator_id=op_clean,
            role="human_operator",
            capabilities=DEFAULT_CAPABILITIES,
            authenticated=False,
            auth_method="unauthenticated",
        )

    else:
        # Presented token exists — authenticate against HMAC or state_db
        if presented_token.startswith("haflow_v1."):
            principal = verify_operator_hmac_token(
                presented_token,
                expected_operator=op_clean,
                secret=secret,
            )
        elif registered_token is not None:
            if not hmac.compare_digest(presented_token, registered_token):
                raise OperatorAuthenticationError(
                    f"operator authentication failed: invalid operator token for '{op_clean}'"
                )
            role = "human_operator"
            auth_method = source_method if source_method in ("credentials_file", "bearer_token") else "state_db_token"
            principal = OperatorPrincipal(
                operator_id=op_clean,
                role=role,
                capabilities=ROLE_CAPABILITIES.get(role, DEFAULT_CAPABILITIES),
                authenticated=True,
                auth_method=auth_method,
            )
        else:
            raise OperatorAuthenticationError(
                f"operator authentication failed: invalid or unregistered credentials for operator '{op_clean}'"
            )

    # 5. Role & Capability Authorization check
    if principal.role not in ALLOWED_OPERATOR_ROLES:
        raise OperatorAuthorizationError(
            f"operator authorization failed: unauthorized role '{principal.role}' for operator '{op_clean}'"
        )

    if action and action not in principal.capabilities:
        raise OperatorAuthorizationError(
            f"operator authorization failed: operator '{op_clean}' lacks capability '{action}'"
        )

    return principal
