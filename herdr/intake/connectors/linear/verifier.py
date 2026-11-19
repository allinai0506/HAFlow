"""Linear webhook signature verification and clock-skew enforcement."""

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Mapping


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

        # Clock skew validation
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        ts_header = lower_headers.get("linear-timestamp")
        timestamp_ms = None
        if ts_header:
            try:
                timestamp_ms = int(ts_header)
            except ValueError:
                raise TimestampSkewError("TIMESTAMP_SKEW: invalid Linear-Timestamp integer format")
            if abs(now - timestamp_ms) > self._max_skew_ms:
                diff = abs(now - timestamp_ms)
                raise TimestampSkewError(f"TIMESTAMP_SKEW: clock skew {diff}ms exceeds {self._max_skew_ms}ms limit")

        delivery_id = lower_headers.get("linear-delivery")
        event_type = lower_headers.get("linear-event")

        return VerifiedDelivery(
            raw_bytes=raw_bytes,
            signature_verified=True,
            delivery_id=delivery_id,
            event_type=event_type,
            timestamp_ms=timestamp_ms,
        )
