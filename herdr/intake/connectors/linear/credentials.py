"""Credential resolution and rotation security for Linear Connector."""

import os
import re
from typing import Callable

CANONICAL_AUTH_REF_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def validate_canonical_auth_ref(auth_ref: str) -> str:
    """Validate that auth_ref is non-empty, non-default, and follows canonical lowercase kebab-case."""
    if not auth_ref or not isinstance(auth_ref, str):
        raise SecretResolutionError("MISSING_AUTH_REF: connection auth_ref is empty")
    if auth_ref.lower() in ("none", "default"):
        raise SecretResolutionError(
            f"DISALLOWED_DEFAULT_AUTH_REF: Linear webhook requires an explicit, immutable auth_ref (cannot use '{auth_ref}')"
        )
    if not CANONICAL_AUTH_REF_RE.fullmatch(auth_ref):
        raise SecretResolutionError(
            f"NON_CANONICAL_AUTH_REF: auth_ref '{auth_ref}' must be canonical lowercase kebab-case (^[a-z0-9]+(-[a-z0-9]+)*$) to preserve distinct credential identities"
        )
    return auth_ref


class SecretResolutionError(ValueError):
    """Raised when the webhook signing secret for an authorized auth_ref cannot be resolved or is mismatched."""


# In-memory registry for programmatic secret injection and testing
_SECRET_REGISTRY: dict[str, str] = {}


def register_secret(auth_ref: str, secret: str) -> None:
    """Register a secret for an auth_ref in memory."""
    if not auth_ref or not isinstance(auth_ref, str):
        raise ValueError("auth_ref must be a non-empty string")
    if not secret or not isinstance(secret, str):
        raise ValueError("secret must be a non-empty string")
    _SECRET_REGISTRY[auth_ref] = secret


def unregister_secret(auth_ref: str) -> None:
    """Remove a secret from in-memory registry."""
    _SECRET_REGISTRY.pop(auth_ref, None)


def clear_secret_registry() -> None:
    """Clear all in-memory registered secrets."""
    _SECRET_REGISTRY.clear()


def resolve_signing_secret(
    auth_ref: str,
    *,
    loaded_secret: str | None = None,
    loaded_auth_ref: str | None = None,
    custom_resolver: Callable[[str], str | None] | None = None,
) -> str:
    """Resolve the authoritative webhook signing secret for the connection's auth_ref.

    Fail-closed policy:
    1. Reject empty, 'none', or 'default' auth_ref (DISALLOWED_DEFAULT_AUTH_REF).
    2. If custom_resolver is provided, query it.
    3. Check the in-memory secret registry.
    4. Check environment variables:
       - HAFLOW_INTAKE_SECRET_<AUTH_REF>
       - LINEAR_WEBHOOK_SECRET_<AUTH_REF>
       - <AUTH_REF> (if auth_ref matches an env var name)
    5. If loaded_auth_ref is explicitly specified:
       - If loaded_auth_ref != auth_ref, raise SecretResolutionError (fail closed).
       - If loaded_auth_ref == auth_ref and loaded_secret is present, return loaded_secret.
    6. If loaded_secret is provided without an explicit loaded_auth_ref:
       - If LINEAR_WEBHOOK_SECRET_REF env var is set, compare with auth_ref; if mismatch, fail closed.
       - Otherwise, fail closed: cannot verify unreferenced secret matches authorized auth_ref.

    Raises:
        SecretResolutionError if secret cannot be resolved or reference mismatches.
    """
    auth_ref = validate_canonical_auth_ref(auth_ref)

    # 1. Custom resolver hook
    if custom_resolver is not None:
        val = custom_resolver(auth_ref)
        if val and isinstance(val, str) and val.strip():
            return val.strip()

    # 2. Check loaded credentials: if loaded_auth_ref was explicitly specified,
    # it MUST match the authorized auth_ref in DB, otherwise FAIL CLOSED!
    if loaded_auth_ref:
        if loaded_auth_ref != auth_ref:
            raise SecretResolutionError(
                f"AUTH_REF_MISMATCH: loaded secret ref '{loaded_auth_ref}' does not match connection authorized auth_ref '{auth_ref}'"
            )
        if loaded_secret and isinstance(loaded_secret, str) and loaded_secret.strip():
            return loaded_secret.strip()

    # 3. In-memory registry
    if auth_ref in _SECRET_REGISTRY:
        val = _SECRET_REGISTRY[auth_ref]
        if val and isinstance(val, str) and val.strip():
            return val.strip()

    # 4. Environment variables specific to auth_ref
    sanitized_ref = auth_ref.upper().replace("-", "_")
    env_keys = [
        f"HAFLOW_INTAKE_SECRET_{sanitized_ref}",
        f"LINEAR_WEBHOOK_SECRET_{sanitized_ref}",
        auth_ref,
    ]
    for key in env_keys:
        val = os.environ.get(key)
        if val and val.strip():
            return val.strip()

    # 5. Loaded secret without loaded_auth_ref
    if loaded_secret and isinstance(loaded_secret, str) and loaded_secret.strip():
        env_ref = os.environ.get("LINEAR_WEBHOOK_SECRET_REF")
        if env_ref:
            if env_ref == auth_ref:
                return loaded_secret.strip()
            raise SecretResolutionError(
                f"AUTH_REF_MISMATCH: environment secret ref '{env_ref}' does not match connection authorized auth_ref '{auth_ref}'"
            )
        raise SecretResolutionError(
            f"AUTH_REF_MISMATCH: connection requires authorized auth_ref '{auth_ref}', but loaded credential has no matching reference"
        )

    raise SecretResolutionError(
        f"CREDENTIAL_UNRESOLVED: cannot resolve signing secret for auth_ref '{auth_ref}'"
    )
