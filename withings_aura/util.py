"""Small helpers for the Withings Aura server."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .const import UNSET_VALUE

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_DST_SCAN_STEP_SECONDS = 6 * 3600
_DST_SCAN_STEPS = 370 * 4


def normalize_mac(mac: str) -> str:
    """Normalize a MAC address to lowercase colon-separated form."""
    return mac.strip().lower().replace("-", ":")


def parse_hhmm(value: str) -> tuple[int, int]:
    """Parse HH:MM into hour/minute."""
    match = _HHMM.match(value.strip())
    if not match:
        raise ValueError("time must be HH:MM")
    return int(match.group(1)), int(match.group(2))


def resolve_timezone(name: str | None) -> tzinfo:
    """Return an IANA zone by name, or the system zone (from /etc/localtime) when unnamed.

    A fixed-offset local time carries no DST rules, so a real zone is needed to tell
    the Aura about the next transition. Falls back to the fixed local offset with no
    transition when the system zone cannot be identified.
    """
    if name is None:
        target = os.path.realpath("/etc/localtime")
        name = target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else None
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as err:
            raise ValueError(f"unknown timezone: {name}") from err
    return datetime.now().astimezone().tzinfo or UTC


def _offset_seconds(value: datetime) -> int:
    offset = value.utcoffset()
    return int(offset.total_seconds()) if offset is not None else 0


def parse_optional_int(value: str | None) -> int | None:
    """Parse an Aura string setting that may be unset (u64 max)."""
    if value in (None, "", UNSET_VALUE):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def format_offset(seconds: int) -> str:
    """Format a UTC offset in seconds as +HH:MM."""
    sign = "+" if seconds >= 0 else "-"
    seconds = abs(seconds)
    return f"{sign}{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}"


def next_dst_transition(now: datetime) -> datetime | None:
    """Return the instant of the next UTC offset change for an aware datetime, if any.

    Scans in elapsed (UTC) time: adding a timedelta to a zoneinfo-aware datetime is
    wall-clock arithmetic, which lands an hour off on fall-back transitions.
    """
    if now.tzinfo is None:
        return None
    zone = now.tzinfo

    def offset_at(timestamp: int) -> int:
        return _offset_seconds(datetime.fromtimestamp(timestamp, zone))

    current = _offset_seconds(now)
    cursor = int(now.timestamp())
    for _ in range(_DST_SCAN_STEPS):
        candidate = cursor + _DST_SCAN_STEP_SECONDS
        if offset_at(candidate) != current:
            low, high = cursor, candidate
            while high - low > 1:
                middle = (low + high) // 2
                if offset_at(middle) == current:
                    low = middle
                else:
                    high = middle
            return datetime.fromtimestamp(high, zone)
        cursor = candidate
    return None


def time_sync_info(now: datetime) -> dict[str, object]:
    """Describe the time/offset data sent to the Aura."""
    transition = next_dst_transition(now)
    current_offset = _offset_seconds(now)
    next_offset = _offset_seconds(transition) if transition is not None else current_offset
    return {
        "timezone": str(now.tzinfo),
        "utc_offset": current_offset,
        "utc_offset_text": format_offset(current_offset),
        "next_change": transition.astimezone(UTC).isoformat() if transition is not None else None,
        "next_offset": next_offset,
        "next_offset_text": format_offset(next_offset),
        "synced_at": now.isoformat(),
    }
