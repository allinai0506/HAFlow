"""Linear webhook signature verification and clock-skew enforcement."""

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Mapping


class SignatureVerificationError(ValueError):
    """Raised when the webhook signature is missing, malformed, or does not match."""


class TimestampSkewError(ValueError):
    """Raised when the webhook timestamp exceeds the allowed clock-skew window."""


@dataclass(frozen=True)
class VerifiedDelivery:
    raw_bytes: bytes
    signature_verified: bool
    delivery_id: str | None
    event_type: str | None
    timestamp_ms: int | None
    payload: dict[str, Any] | None = None


class LinearVerifier:
    """Verifies Linear webhook requests using HMAC-SHA256 and constant-time comparison."""

    def __init__(self, signing_secret: str, *, max_skew_seconds: int = 60):
        if not signing_secret or not isinstance(signing_secret, str):
            raise ValueError("signing_secret must be a non-empty string")
        self._secret = signing_secret.encode("utf-8")
        self._max_skew_ms = max_skew_seconds * 1000

    def verify(
        self,
        raw_bytes: bytes,
        headers: Mapping[str, str],
        *,
        now_ms: int | None = None,
    ) -> VerifiedDelivery:
        if not isinstance(raw_bytes, bytes):
            raise TypeError("raw_bytes must be bytes")

        lower_headers = {str(k).lower(): str(v).strip() for k, v in headers.items()}

        sig_header = lower_headers.get("linear-signature")
        if not sig_header:
            raise SignatureVerificationError("MISSING_SIGNATURE: missing Linear-Signature header")

        # Linear uses bare 64-character lowercase hexadecimal HMAC-SHA256 string
        expected_sig = hmac.new(self._secret, raw_bytes, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig_header, expected_sig):
            raise SignatureVerificationError("INVALID_SIGNATURE: signature does not match expected digest")

        # Parse verified payload body
        try:
            payload = json.loads(raw_bytes.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload must be a JSON object")
        except Exception as exc:
            raise ValueError(f"MALFORMED_PAYLOAD: unable to parse JSON body: {exc}")

        # Authoritative freshness check: must use the HMAC-protected webhookTimestamp in signed body
        raw_ts = payload.get("webhookTimestamp")
        if raw_ts is None:
            raise TimestampSkewError("TIMESTAMP_SKEW: missing webhookTimestamp in signed payload")
        try:
            timestamp_ms = int(raw_ts)
        except (ValueError, TypeError):
            raise TimestampSkewError("TIMESTAMP_SKEW: invalid webhookTimestamp format in signed payload")

        now = now_ms if now_ms is not None else int(time.time() * 1000)
        diff = abs(now - timestamp_ms)
        if diff > self._max_skew_ms:
            raise TimestampSkewError(f"TIMESTAMP_SKEW: clock skew {diff}ms exceeds {self._max_skew_ms}ms limit")

        # Auxiliary header check: if Linear-Timestamp header is present, it must not contradict the signed body
        ts_header = lower_headers.get("linear-timestamp")
        if ts_header:
            try:
                header_ts = int(ts_header)
                if abs(header_ts - timestamp_ms) > 5000:
                    raise TimestampSkewError(
                        f"TIMESTAMP_SKEW: Linear-Timestamp header ({header_ts}) deviates from signed payload timestamp ({timestamp_ms})"
                    )
            except ValueError:
                raise TimestampSkewError("TIMESTAMP_SKEW: invalid Linear-Timestamp integer format")

        delivery_id = lower_headers.get("linear-delivery")
        payload_type = str(payload.get("type") or "").strip()
        header_event = lower_headers.get("linear-event")

        # Event type consistency check between header and body
        if header_event and payload_type and header_event.lower() != payload_type.lower():
            raise ValueError(
                f"EVENT_TYPE_MISMATCH: Linear-Event header '{header_event}' != payload type '{payload_type}'"
            )

        event_type = payload_type or header_event or None

        return VerifiedDelivery(
            raw_bytes=raw_bytes,
            signature_verified=True,
            delivery_id=delivery_id,
            event_type=event_type,
            timestamp_ms=timestamp_ms,
            payload=payload,
        )
