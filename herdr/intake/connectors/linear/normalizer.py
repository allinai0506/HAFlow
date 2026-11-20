"""Normalize Linear Webhook payloads into HAFlow CloudEvents 1.0 work-item facts."""

import json
from datetime import datetime, timezone
from typing import Any

from herdr.intake.materializer import DATA_SCHEMA, EVENT_TYPE


class OrganizationMismatchError(ValueError):
    """Raised when the incoming payload organizationId does not match the configured binding."""


class UnsupportedEventTypeError(ValueError):
    """Raised when the webhook event payload type is not supported by this connector."""


class BotEchoIgnoredError(UnsupportedEventTypeError):
    """Raised when the webhook event was authored by HAFlow's bot user to prevent echo loops."""


def _clean_str(value: str | None) -> str:
    if not value:
        return ""
    # Strip ASCII control characters < 32 (preserving \n, \r, \t) and 127 DEL
    return "".join(c for c in value if (ord(c) >= 32 and ord(c) != 127) or c in "\n\r\t")


def _truncate_utf8(text: str, max_bytes: int, suffix: str = "...") -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    suffix_bytes = suffix.encode("utf-8")
    cut_limit = max_bytes - len(suffix_bytes)
    # Truncate at character boundary
    truncated = encoded[:cut_limit].decode("utf-8", errors="ignore")
    return truncated + suffix


def _parse_iso_ms(iso_str: str) -> int:
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


class LinearNormalizer:
    """Normalizes and defends against external Linear Issue Webhook payload variations."""

    def __init__(self, organization_id: str, bot_user_ids: tuple[str, ...] | None = None):
        if not organization_id or not isinstance(organization_id, str):
            raise ValueError("organization_id must be a non-empty string")
        self.organization_id = organization_id
        self.bot_user_ids = tuple(bot_user_ids or ())

    def normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dictionary")

        # Echo-loop detection: check if event actor is HAFlow's bot
        actor = payload.get("actor")
        if isinstance(actor, dict):
            actor_id = str(actor.get("id") or "").strip()
            actor_name = str(actor.get("name") or "").strip().lower()
            actor_type = str(actor.get("type") or "").strip().lower()
            if (
                (actor_id and actor_id in self.bot_user_ids)
                or actor_name in ("haflow", "haflow bot", "haflow-bot")
                or (actor_type == "application" and "haflow" in actor_name)
            ):
                raise BotEchoIgnoredError(
                    f"BOT_ECHO_IGNORED: event authored by bot actor '{actor_id or actor_name}', ignoring to prevent recursive loops"
                )

        payload_type = str(payload.get("type") or "").strip()
        if payload_type != "Issue":
            raise UnsupportedEventTypeError(
                f"UNSUPPORTED_EVENT_TYPE: event type '{payload_type}' is not supported; Linear V1 only subscribes to 'Issue'"
            )

        payload_org = payload.get("organizationId")
        if payload_org != self.organization_id:
            raise OrganizationMismatchError(
                f"ORGANIZATION_MISMATCH: payload organization '{payload_org}' != configured '{self.organization_id}'"
            )

        action = str(payload.get("action") or "update").lower()
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("payload data must be a dictionary")

        issue_id = str(data.get("id") or "").strip()
        if not issue_id:
            raise ValueError("missing Linear issue id")

        identifier = str(data.get("identifier") or "").strip()
        raw_title = _clean_str(str(data.get("title") or "Untitled Issue")).strip()
        title_with_prefix = f"[{identifier}] {raw_title}" if identifier else raw_title

        # Determine action
        state = data.get("state") if isinstance(data.get("state"), dict) else {}
        state_type = str(state.get("type") or "").lower()

        is_closed = (action == "remove") or (state_type in ("completed", "canceled"))
        if is_closed:
            work_action = "closed"
        elif action == "create":
            work_action = "work"
        elif action == "update":
            work_action = "update"
        else:
            work_action = "metadata"

        # Defensive Title handling (<= 240 bytes)
        title_truncated = False
        if len(title_with_prefix.encode("utf-8")) > 240:
            title = _truncate_utf8(title_with_prefix, 240, suffix="...")
            title_truncated = True
        else:
            title = title_with_prefix

        # Defensive Description handling: fallback if empty, truncate if > 8000 bytes
        raw_desc = _clean_str(data.get("description") if data.get("description") is not None else "")
        if not raw_desc.strip():
            desc = f"Linear issue {identifier}: {raw_title}\n\n(No description provided in Linear)"
        else:
            desc = raw_desc.strip()

        if title_truncated:
            desc = f"Full Title: {title_with_prefix}\n\n{desc}"

        if len(desc.encode("utf-8")) > 8000:
            desc = _truncate_utf8(
                desc,
                7900,
                suffix="\n\n... [Content truncated by Linear Connector (exceeded 8KB limit)]",
            )

        # Version timestamp
        # For remove actions, the deletion event timestamp in payload.createdAt is the
        # lifecycle fact time, whereas data.updatedAt may retain the pre-deletion timestamp.
        if action == "remove":
            updated_at = payload.get("createdAt") or data.get("updatedAt")
        else:
            updated_at = data.get("updatedAt") or payload.get("createdAt")
        if updated_at:
            version = _parse_iso_ms(str(updated_at))
        else:
            version = int(time.time() * 1000)

        # Assignee
        identities = {}
        assignee_data = data.get("assignee")
        if isinstance(assignee_data, dict):
            name_or_id = str(assignee_data.get("name") or assignee_data.get("id") or "").strip()
            if name_or_id:
                identities["assignee"] = _truncate_utf8(_clean_str(name_or_id), 128)

        fact = {
            "object_type": "issue",
            "external_id": issue_id,
            "version": version,
            "action": work_action,
            "title": title,
            "description": desc,
        }
        if identities:
            fact["identities"] = identities

        return fact

    def to_cloudevents_envelope(
        self,
        fact: dict[str, Any],
        source: str,
        occurred_time: str | None = None,
        *,
        lifecycle_action: str | None = None,
    ) -> dict[str, Any]:
        """Wrap the work-item fact into a CloudEvents 1.0 JSON object."""
        action_tag = lifecycle_action or fact.get("action") or "work"
        envelope: dict[str, Any] = {
            "specversion": "1.0",
            "source": source,
            "id": f"issue:{fact['external_id']}:{action_tag}:{fact['version']}",
            "type": EVENT_TYPE,
            "dataschema": DATA_SCHEMA,
            "subject": f"issue/{fact['external_id']}",
            "data": fact,
        }
        if occurred_time:
            envelope["time"] = occurred_time
        return envelope
