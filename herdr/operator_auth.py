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
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from herdr.transitions import validate_operator

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


@dataclass(frozen=True)
class OperatorPrincipal:
    """Authenticated and authorized operator principal."""
    operator_id: str
    role: str = "human_operator"
    capabilities: tuple[str, ...] = DEFAULT_CAPABILITIES
    authenticated: bool = True
    auth_method: str = "token"  # "state_db_token" | "hmac_token" | "bearer_token" | "credentials_file" | "unauthenticated"


# ============================================================
# System Signing Secret Management
# ============================================================

def _get_signing_secret(explicit_secret: str | None = None) -> str:
    """Resolve signing secret for HMAC tokens."""
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
        key_file = cred_dir / "operator_hmac_signing.key"
        if key_file.is_file():
            val = key_file.read_text(encoding="utf-8").strip()
            if val:
                return val
        secret_val = hashlib.sha256(f"haflow_system_secret_{os.getuid()}_{time.time()}".encode()).hexdigest()
        key_file.write_text(secret_val, encoding="utf-8")
        try:
            key_file.chmod(0o600)
        except OSError:
            pass
        return secret_val
    except (OSError, ValueError, RuntimeError):
        return "haflow_default_local_signing_secret"


# ============================================================
# Signed HMAC Token Management
# ============================================================

def issue_operator_hmac_token(
    operator_id: str,
    role: str = "human_operator",
    capabilities: Sequence[str] = DEFAULT_CAPABILITIES,
    secret: str | None = None,
    expires_in: float | None = 3600.0,
) -> str:
    """Issue a tamper-evident signed HMAC token for an operator principal."""
    op_clean = validate_operator(operator_id)
    signing_secret = _get_signing_secret(secret)
    now = time.time()
    exp = (now + float(expires_in)) if expires_in is not None else None

    payload = {
        "sub": op_clean,
        "role": str(role).strip(),
        "caps": [str(c).strip() for c in capabilities],
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
) -> str | None:
    """Resolve explicitly presented token from arguments, files, or environment."""
    presented_token = str(token or "").strip() or os.environ.get("HERDR_OPERATOR_TOKEN", "").strip()
    if presented_token.startswith("Bearer "):
        presented_token = presented_token[7:].strip()

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

        for root in roots:
            if p.is_relative_to(root):
                raise OperatorAuthenticationError(
                    "operator credentials file cannot reside in worker clone or project workspace"
                )

        if not p.is_file():
            raise OperatorAuthenticationError(f"operator credentials file not found: {p}")
        try:
            presented_token = p.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise OperatorAuthenticationError(f"unreadable operator credentials file: {exc}") from exc

    return presented_token or None


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
    1. Operator format validity via validate_operator (SC-010).
    2. Anti-impersonation: Task worker agent cannot self-bypass (SC-010 / #307).
    3. Explicit token presentation: validates against signed HMAC token or state_db.
    4. Anti-spoofing: Prevents unauthenticated callers from using registered operator identities.
    5. Capability & Role authorization per gate_bypass_policy.yaml.
    """
    op_clean = validate_operator(operator_id)

    # 1. Anti-impersonation: Task worker independence
    if task:
        task_agent = str(task.get("agent") or "").strip().lower()
        if task_agent and op_clean.lower() == task_agent:
            raise OperatorImpersonationError(
                "the operator must be independent of the task worker; agent cannot bypass its own gate"
            )

    # 2. Resolve presented token
    presented_token = resolve_presented_token(
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

    # Strict auth determination
    is_strict = strict_auth
    if is_strict is None:
        is_strict = os.environ.get("HERDR_OPERATOR_AUTH_STRICT", "").lower() in ("1", "true", "yes")

    principal: OperatorPrincipal

    # 4. Authentication check
    if not presented_token:
        # If credentials exist for this operator in state_db, presentation is MANDATORY (anti-spoofing)
        if registered_token is not None:
            raise OperatorAuthenticationError(
                f"operator authentication required: missing operator token for '{op_clean}'"
            )
        if is_strict:
            raise OperatorAuthenticationError(
                f"operator authentication required: missing operator credentials for '{op_clean}'"
            )
        # Unauthenticated fallback (legacy compatibility when no credentials registered and non-strict)
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
            principal = OperatorPrincipal(
                operator_id=op_clean,
                role="human_operator",
                capabilities=DEFAULT_CAPABILITIES,
                authenticated=True,
                auth_method="state_db_token",
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
