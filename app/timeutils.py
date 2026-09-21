"""Time helpers: RFC 3339 UTC parsing/formatting with microsecond integer math."""

from __future__ import annotations

import calendar
from datetime import datetime, timezone
from decimal import Decimal

MICROS_PER_SECOND = 1_000_000


def require_aware(value: datetime) -> datetime:
    """Reject naive timestamps: an explicit offset (Z) is mandatory in RFC 3339."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include an explicit UTC offset, e.g. 'Z' or '+00:00'")
    return value


def to_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def to_micros(value: datetime) -> int:
    """Seconds since the Unix epoch, expressed in microseconds (integer)."""
    aware = require_aware(value)
    utc = to_utc(aware)
    # Exact integer math; avoids float rounding of timestamp() * 1e6.
    return calendar.timegm(utc.timetuple()) * MICROS_PER_SECOND + utc.microsecond


def from_micros(micros: int) -> datetime:
    seconds, remainder = divmod(micros, MICROS_PER_SECOND)
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=remainder)


def format_rfc3339(value: datetime) -> str:
    """Format as RFC 3339 UTC with a trailing 'Z', trimmed to microsecond precision."""
    utc = to_utc(value)
    # isoformat gives e.g. 2026-09-21T10:00:00+00:00 (or with .ffffff)
    text = utc.isoformat()
    if text.endswith("+00:00"):
        text = text[:-6] + "Z"
    return text


def format_micros(micros: int) -> str:
    return format_rfc3339(from_micros(micros))


def seconds_to_micros(value: Decimal) -> int:
    """Convert a non-negative seconds value to integer microseconds.

    Raises ValueError on sub-microsecond precision so that silently dropping
    information cannot turn an infeasible system into a feasible one.
    """
    scaled = value * MICROS_PER_SECOND
    if scaled != scaled.to_integral_value():
        raise ValueError("interval resolution is limited to whole microseconds (1e-6 s)")
    micros = int(scaled)
    if micros < 0:
        raise ValueError("interval must be non-negative")
    return micros


def micros_to_seconds(micros: int) -> Decimal:
    return Decimal(micros) / MICROS_PER_SECOND


def format_seconds(micros: int) -> str:
    """Render a microsecond duration as seconds: '30', '0.5', '0.000001'."""
    value = micros_to_seconds(micros)
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text
