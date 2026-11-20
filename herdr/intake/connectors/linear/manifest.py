"""Manifest and configuration validation for Linear Connector V1."""

import re
from dataclasses import dataclass
from typing import Any

from herdr.intake.protocol import token, text
from .credentials import CANONICAL_AUTH_REF_RE

ORGANIZATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TEAM_KEY_RE = re.compile(r"^[A-Z0-9]{1,16}$")


@dataclass(frozen=True)
class ConfigReport:
    valid: bool
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class Manifest:
    provider: str
    manifest_version: str
    connector_version: str
    configuration_schema: dict[str, Any]
    auth: list[dict[str, Any]]
    transports: dict[str, Any]
    object_read: bool
    events: list[dict[str, Any]]
    health: dict[str, Any]


LINEAR_MANIFEST = Manifest(
    provider="linear",
    manifest_version="1",
    connector_version="1.0.0",
    configuration_schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["organization_id", "webhook_signing_secret_ref"],
        "properties": {
            "organization_id": {
                "type": "string",
                "pattern": "^[A-Za-z0-9_-]{1,64}$",
            },
            "webhook_signing_secret_ref": {
                "type": "string",
                "pattern": "^[A-Za-z0-9_-]{1,128}$",
            },
            "api_token_ref": {
                "type": "string",
                "pattern": "^[A-Za-z0-9_-]{1,128}$",
            },
            "team_keys": {
                "type": "array",
                "items": {"type": "string", "pattern": "^[A-Z0-9]{1,16}$"},
            },
        },
    },
    auth=[
        {
            "method": "webhook_hmac_sha256",
            "ref_slot": "signing_secret",
            "scope": "registered_organization_and_webhook",
            "required": True,
        },
        {
            "method": "personal_api_key",
            "ref_slot": "api_read_token",
            "scope": ["read"],
            "purpose": "object_read_and_health",
            "required": False,
        },
    ],
    transports={
        "webhook": {
            "ack_profile": "linear_http_200_after_commit",
            "response_deadline_ms": 5000,
            "max_body_bytes": 262144,
            "signed_timestamp_max_skew_seconds": 60,
        },
    },
    object_read=True,
    events=[
        {
            "provider_type": "Issue:create",
            "type": "com.haflow.intake.work-item.observed.v1",
            "data_schema": "urn:haflow:intake:work-item:v1",
        },
        {
            "provider_type": "Issue:update",
            "type": "com.haflow.intake.work-item.observed.v1",
            "data_schema": "urn:haflow:intake:work-item:v1",
        },
        {
            "provider_type": "Issue:remove",
            "type": "com.haflow.intake.work-item.observed.v1",
            "data_schema": "urn:haflow:intake:work-item:v1",
        },
    ],
    health={
        "checks": ["verified_organization", "last_delivery_receipt", "read_grant_if_configured"],
    },
)


def validate_config(config: dict[str, Any]) -> ConfigReport:
    """Validate connection configuration against schema boundaries without external side effects."""
    errors: list[str] = []
    if not isinstance(config, dict):
        return ConfigReport(valid=False, errors=("config must be a dictionary",))

    org_id = config.get("organization_id")
    if not org_id or not isinstance(org_id, str) or not ORGANIZATION_ID_RE.fullmatch(org_id):
        errors.append("organization_id is required and must match ^[A-Za-z0-9_-]{1,64}$")

    secret_ref = config.get("webhook_signing_secret_ref")
    if not secret_ref or not isinstance(secret_ref, str):
        errors.append("webhook_signing_secret_ref is required")
    elif secret_ref.lower() in ("none", "default"):
        errors.append("webhook_signing_secret_ref cannot be 'none' or 'default'")
    elif not CANONICAL_AUTH_REF_RE.fullmatch(secret_ref):
        errors.append(
            f"webhook_signing_secret_ref '{secret_ref}' must be canonical lowercase kebab-case (^[a-z0-9]+(-[a-z0-9]+)*$)"
        )
    else:
        try:
            token(secret_ref)
        except ValueError:
            errors.append("webhook_signing_secret_ref must be a valid token")

    token_ref = config.get("api_token_ref")
    if token_ref is not None:
        try:
            token(token_ref)
        except ValueError:
            errors.append("api_token_ref must be a valid token")

    team_keys = config.get("team_keys")
    if team_keys is not None:
        if not isinstance(team_keys, list):
            errors.append("team_keys must be a list of strings")
        else:
            for key in team_keys:
                if not isinstance(key, str) or not TEAM_KEY_RE.fullmatch(key):
                    errors.append(f"invalid team_key '{key}': must match ^[A-Z0-9]{{1,16}}$")

    return ConfigReport(valid=len(errors) == 0, errors=tuple(errors))
