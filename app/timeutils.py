"""Timestamp and money normalisation.

All timestamps are stored as fixed-width UTC ISO-8601 strings:

    2026-01-08T12:11:58.085567+00:00

Fixed width matters. SQLite has no native datetime type, so range
filters and ORDER BY are lexicographic string comparisons - which are
only equivalent to chronological comparisons if every stored value has
the same shape and the same timezone. `datetime.isoformat()` drops the
microsecond component when it is zero, so it is not used directly.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%f+00:00"

# Sentinels used to make a bare calendar date behave as an inclusive range.
DAY_START_SUFFIX = "T00:00:00.000000+00:00"
DAY_END_SUFFIX = "T23:59:59.999999+00:00"


class TimestampError(ValueError):
    """Raised when a timestamp cannot be parsed."""


def to_utc(value: datetime) -> datetime:
    """Attach UTC to a naive datetime, or convert an aware one to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def format_ts(value: datetime) -> str:
    return to_utc(value).strftime(TS_FORMAT)


def parse_ts(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return to_utc(value)
    text = value.strip()
    if not text:
        raise TimestampError("timestamp must not be empty")
    # `fromisoformat` on 3.11+ accepts a trailing Z, but be explicit so the
    # behaviour does not depend on the interpreter version.
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return to_utc(datetime.fromisoformat(text))
    except ValueError as exc:
        raise TimestampError(f"invalid ISO-8601 timestamp: {value!r}") from exc


def normalise_ts(value: str | datetime) -> str:
    """Parse any accepted timestamp form into the canonical stored form."""
    return format_ts(parse_ts(value))


def now_ts() -> str:
    return format_ts(datetime.now(timezone.utc))


def range_lower_bound(value: str | datetime | date) -> str:
    """Normalise the inclusive start of a date/datetime range filter."""
    if isinstance(value, datetime):
        return format_ts(value)
    if isinstance(value, date):
        return f"{value.isoformat()}{DAY_START_SUFFIX}"
    text = str(value).strip()
    if len(text) == 10:  # bare YYYY-MM-DD
        _validate_date(text)
        return f"{text}{DAY_START_SUFFIX}"
    return normalise_ts(text)


def range_upper_bound(value: str | datetime | date) -> str:
    """Normalise the inclusive end of a date/datetime range filter.

    A bare date expands to the last microsecond of that day, so that
    `date_to=2026-01-08` includes everything that happened on the 8th
    rather than only the midnight boundary.
    """
    if isinstance(value, datetime):
        return format_ts(value)
    if isinstance(value, date):
        return f"{value.isoformat()}{DAY_END_SUFFIX}"
    text = str(value).strip()
    if len(text) == 10:
        _validate_date(text)
        return f"{text}{DAY_END_SUFFIX}"
    return normalise_ts(text)


def _validate_date(text: str) -> None:
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise TimestampError(f"invalid date: {text!r}") from exc


# ---------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------

def to_minor_units(amount: float | int | str | Decimal) -> int:
    """Convert a major-unit amount to integer minor units (e.g. paise).

    Decimal is used rather than `round(amount * 100)` because binary
    floats do not represent 2-decimal values exactly: `round(8.115 * 100)`
    is 811, not 812. Currency arithmetic should never be done in float.
    """
    try:
        dec = Decimal(str(amount))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid amount: {amount!r}") from exc
    if not dec.is_finite():
        raise ValueError(f"invalid amount: {amount!r}")
    return int((dec * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_major_units(amount_minor: int | None) -> float | None:
    if amount_minor is None:
        return None
    return float(Decimal(amount_minor) / 100)
