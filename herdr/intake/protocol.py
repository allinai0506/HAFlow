"""Bounded CloudEvents 1.0 facts and RFC 8785 serialization."""

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
from urllib.parse import urlsplit

MAX_BODY_BYTES = 262_144
MAX_DEPTH = 32
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
EVENT_TYPE = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)+\.v[1-9][0-9]*")
RFC3339 = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utf8(value):
    if any(0xFDD0 <= ord(char) <= 0xFDEF or (ord(char) & 0xFFFF) >= 0xFFFE for char in value):
        raise ValueError("invalid I-JSON character")
    return value.encode("utf-8")


def text(value, limit=256):
    if (not isinstance(value, str) or not value.strip()
            or len(_utf8(value)) > limit
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("invalid intake string")
    return value


def token(value):
    if not isinstance(value, str) or not TOKEN.fullmatch(value):
        raise ValueError("invalid intake key")
    return value


def error_code(value):
    if not isinstance(value, str) or not ERROR_CODE.fullmatch(value):
        raise ValueError("invalid intake error code")
    return value


def integer(value, minimum=0, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid intake integer")
    return value


def _unique_pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON property")
        out[key] = value
    return out


def _parse_number(encoded):
    exact = Decimal(encoded)
    number = float(encoded)
    if (not math.isfinite(number) or (number == 0 and exact != 0)
            or (abs(number) >= 2**53 and Decimal(repr(number)) != exact)):
        raise ValueError("number cannot be safely represented")
    return number


def _parse_int(encoded):
    value = int(encoded)
    return value if abs(value) <= 2**53 - 1 else _parse_number(encoded)


def _reject_constant(_):
    raise ValueError("invalid JSON number")


def parse_json(raw: bytes):
    if not isinstance(raw, bytes) or len(raw) > MAX_BODY_BYTES:
        raise ValueError("invalid JSON size")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs,
                          parse_int=_parse_int, parse_float=_parse_number, parse_constant=_reject_constant)
    except (ValueError, RecursionError, DecimalException):
        raise ValueError("invalid intake JSON") from None


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("non-finite number")
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    mantissa, _, exponent = repr(abs(value)).partition("e")
    whole, _, fractional = mantissa.partition(".")
    digits = (whole + fractional).lstrip("0")
    leading_zeroes = len(whole + fractional) - len(digits)
    point = len(whole) + int(exponent or 0) - leading_zeroes
    digits = digits.rstrip("0")
    if -5 <= point <= 21:
        if point <= 0:
            return sign + "0." + "0" * -point + digits
        if point >= len(digits):
            return sign + digits + "0" * (point - len(digits))
        return sign + digits[:point] + "." + digits[point:]
    mantissa = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
    return sign + mantissa + f"e{point - 1:+d}"


def _serialize(value, depth):
    if depth > MAX_DEPTH:
        raise ValueError("JSON nesting limit")
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError("JSON keys must be strings")
        keys = sorted(value, key=lambda key: key.encode("utf-16-be"))
        return "{" + ",".join(_serialize(key, depth + 1) + ":"
                              + _serialize(value[key], depth + 1) for key in keys) + "}"
    if type(value) is list:
        return "[" + ",".join(_serialize(item, depth + 1) for item in value) + "]"
    if type(value) is float:
        return _number(value)
    if type(value) is int and abs(value) > 2**53 - 1:
        raise ValueError("integers outside the safe range must be strings")
    if type(value) not in (str, int, bool, type(None)):
        raise ValueError("unsupported JSON value")
    return json.dumps(value, ensure_ascii=False)


def canonical_json(value) -> str:
    result = _serialize(value, 0)
    if len(_utf8(result)) > MAX_BODY_BYTES:
        raise ValueError("canonical data exceeds limit")
    return result


def _uri(value):
    value = text(value)
    if (not urlsplit(value).scheme or not value.isascii()
            or re.search(r'[^A-Za-z0-9._~:/?#\[\]@!$&\'()*+,;=%-]|%(?![0-9a-fA-F]{2})', value)):
        raise ValueError("invalid absolute URI")
    return value


def _fact_time(value):
    if value is None:
        return None, None
    if not RFC3339.fullmatch(text(value, 64)):
        raise ValueError("invalid fact time")
    if not value.endswith("Z") and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
        raise ValueError("invalid fact time offset")
    offset_start = -1 if value.endswith("Z") else -6
    seconds, dot, fraction = value[:offset_start].partition(".")
    when = datetime.fromisoformat(seconds + value[offset_start:].replace("Z", "+00:00"))
    utc = when.astimezone(timezone.utc)
    fraction = fraction.rstrip("0") if dot else ""
    normalized = utc.isoformat(timespec="seconds")[:-6] + ("." + fraction if fraction else "") + "Z"
    delta = utc - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1000 + int(fraction[:3].ljust(3, "0")), normalized


@dataclass(frozen=True)
class Scope:
    """Constructed by trusted service assembly, independently of the envelope."""

    tenant_id: str
    connection_id: str

    def __post_init__(self):
        token(self.tenant_id)
        token(self.connection_id)

    @property
    def keys(self):
        return self.tenant_id, self.connection_id


@dataclass(frozen=True)
class Delivery:
    """A delivery candidate. Its fields carry no verification authority."""

    envelope: bytes
    raw: bytes
    capability: str
    verified_via: str
    scope: Scope
    config_revision: int
    projection_version: str = "1"
    transport_id: str | None = None
    provider_event_id: str | None = None
    batch_id: str | None = None
    duplicate_only: bool = False


@dataclass(frozen=True)
class Fact:
    source: str
    event_id: str
    type: str
    schema: str | None
    data: str | None
    subject: str | None
    occurred_at: int | None
    occurred_time: str | None
    digest: str


def prepare_fact(delivery: Delivery) -> Fact:
    body = parse_json(delivery.envelope)
    if type(body) is not dict or body.get("specversion") != "1.0":
        raise ValueError("invalid CloudEvents version")
    core = {"specversion", "source", "id", "type", "dataschema", "datacontenttype",
            "subject", "time", "data", "correlationid"}
    if body.keys() - core or body.get("datacontenttype", "application/json") != "application/json":
        raise ValueError("unsupported CloudEvents attributes")
    source, event_id, kind = _uri(body.get("source")), text(body.get("id")), text(body.get("type"))
    if not EVENT_TYPE.fullmatch(kind):
        raise ValueError("invalid Intake event type")
    for key in ("dataschema", "subject", "time", "correlationid"):
        if key in body:
            text(body[key])
    schema = _uri(body["dataschema"]) if "dataschema" in body else None
    occurred_at, occurred_time = _fact_time(body.get("time"))
    data = canonical_json(body["data"]) if "data" in body else None
    projection = {key: body[key] for key in ("type", "dataschema", "subject", "time", "data")
                  if key in body}
    if occurred_time is not None:
        projection["time"] = occurred_time
    checksum = digest(canonical_json(projection).encode("utf-8"))
    version = token(delivery.projection_version)
    return Fact(source, event_id, kind, schema, data, body.get("subject"), occurred_at,
                occurred_time, f"jcs-sha256:{version}:{checksum}")
