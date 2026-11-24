"""DST transition regression tests."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from withings_aura.util import next_dst_transition


@pytest.mark.parametrize(
    ("zone", "start", "expected_utc"),
    [
        ("Europe/Paris", datetime(2026, 11, 24, 12, 0), datetime(2027, 3, 28, 1, 0, tzinfo=UTC)),
        ("Europe/Paris", datetime(2026, 6, 24, 12, 0), datetime(2026, 10, 25, 1, 0, tzinfo=UTC)),
        ("America/New_York", datetime(2026, 6, 24, 12, 0), datetime(2026, 11, 1, 6, 0, tzinfo=UTC)),
        ("America/New_York", datetime(2026, 1, 24, 12, 0), datetime(2026, 3, 8, 7, 0, tzinfo=UTC)),
    ],
)
def test_transition_instant_is_exact_for_both_directions(zone, start, expected_utc) -> None:
    transition = next_dst_transition(start.replace(tzinfo=ZoneInfo(zone)))
    assert transition is not None
    assert transition.astimezone(UTC) == expected_utc


def test_zone_without_dst_has_no_transition() -> None:
    assert next_dst_transition(datetime(2026, 6, 1, tzinfo=ZoneInfo("Asia/Tokyo"))) is None
