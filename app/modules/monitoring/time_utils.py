"""Canonical UTC times and half-open, minute-aligned monitoring windows."""

from datetime import datetime, timedelta, timezone


def now_utc():
    return datetime.now(timezone.utc)


def parse_time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("an explicit timezone is required")
    return result.astimezone(timezone.utc)


def stamp(value):
    return parse_time(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def window_bounds(value):
    start = parse_time(value).replace(second=0, microsecond=0)
    return stamp(start), stamp(start + timedelta(seconds=60))
