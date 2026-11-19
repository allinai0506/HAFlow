"""Linear Connector V1 main implementation conforming to Connector protocol."""

import json
from typing import Any, Mapping

from herdr.intake.protocol import Delivery, Scope
from .client import ConnectionTestReport, HealthStatus, LinearClient
from .manifest import ConfigReport, LINEAR_MANIFEST, validate_config
from .normalizer import LinearNormalizer
from .verifier import LinearVerifier, VerifiedDelivery

CAPABILITY = "issue-webhook"


class LinearConnector:
    """Linear Connector V1 managing webhook verification, payload normalization, and health checks."""

    def __init__(
        self,
        scope: Scope,
        organization_id: str,
        signing_secret: str,
        *,
        api_token: str | None = None,
        max_skew_seconds: int = 60,
        config_revision: int = 1,
    ):
        self.scope = scope
        self.organization_id = organization_id
        self.signing_secret = signing_secret
        self.api_token = api_token
        self.config_revision = config_revision

        self.verifier = LinearVerifier(signing_secret, max_skew_seconds=max_skew_seconds)
        self.normalizer = LinearNormalizer(organization_id)
        self.client = LinearClient(api_token) if api_token else None

    @classmethod
    def manifest(cls):
        return LINEAR_MANIFEST

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> ConfigReport:
        return validate_config(config)

    def test_connection(self, expected_team_keys: list[str] | None = None) -> ConnectionTestReport:
        if not self.client:
            return ConnectionTestReport(
                ok=False,
                error="API_TOKEN_NOT_CONFIGURED: api_token is required for connection testing",
            )
        return self.client.test_connection(
            expected_org_id=self.organization_id,
            expected_team_keys=expected_team_keys,
        )

    def health_check(self) -> HealthStatus:
        if not self.client:
            return HealthStatus(status="OK", message="Webhook-only mode active (API token not configured)")
        return self.client.health_check(expected_org_id=self.organization_id)

    def process_webhook(
        self,
        raw_bytes: bytes,
        headers: Mapping[str, str],
        *,
        now_ms: int | None = None,
    ) -> Delivery | None:
        """Verify, normalize, and construct an Intake Delivery from incoming webhook bytes.

        Returns:
            Delivery if the payload is an Issue event, or None if the payload is a valid
            non-Issue event (e.g. Project, Comment) which is safely ignored.
        """
        verified = self.verifier.verify(raw_bytes, headers, now_ms=now_ms)
        if verified.event_type != "Issue":
            return None

        payload = verified.payload if verified.payload is not None else json.loads(raw_bytes.decode("utf-8"))

        fact = self.normalizer.normalize(payload)
        source = f"urn:haflow:intake:linear:{self.organization_id}:{CAPABILITY}"

        occurred_time = None
        data = payload.get("data")
        action = str(payload.get("action") or "").lower()
        if action == "remove":
            if payload.get("createdAt"):
                occurred_time = str(payload["createdAt"])
            elif isinstance(data, dict) and data.get("updatedAt"):
                occurred_time = str(data["updatedAt"])
        elif isinstance(data, dict) and data.get("updatedAt"):
            occurred_time = str(data["updatedAt"])

        envelope_dict = self.normalizer.to_cloudevents_envelope(
            fact, source, occurred_time, lifecycle_action=action or None
        )
        envelope_bytes = json.dumps(envelope_dict).encode("utf-8")

        return Delivery(
            envelope=envelope_bytes,
            raw=raw_bytes,
            capability=CAPABILITY,
            verified_via="SIGNATURE",
            scope=self.scope,
            config_revision=self.config_revision,
            transport_id=verified.delivery_id,
            provider_event_id=verified.delivery_id,
        )
