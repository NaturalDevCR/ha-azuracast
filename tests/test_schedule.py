"""Tests for schedule evaluation."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.azuracast.api.models import Playlist, ScheduleItem
from custom_components.azuracast.schedule import (
    playlist_playable_now,
    schedule_item_active,
    station_time,
)

CR = ZoneInfo("America/Costa_Rica")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """2026-11-16 is a Monday (ISO weekday 1)."""
    return datetime(2026, 11, day, hour, minute, tzinfo=CR)


def item(
    start: int,
    end: int,
    days: tuple[int, ...] = (),
    start_date: date | None = None,
    end_date: date | None = None,
) -> ScheduleItem:
    return ScheduleItem(
        start_time=start, end_time=end, days=days, start_date=start_date, end_date=end_date
    )


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (at(16, 11, 59), False),
        (at(16, 12, 0), True),
        (at(16, 17, 59), True),
        (at(16, 18, 0), False),
    ],
)
def test_same_day_window(moment: datetime, expected: bool) -> None:
    assert schedule_item_active(item(1200, 1800), moment) is expected


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (at(16, 17, 59), False),
        (at(16, 18, 0), True),
        (at(16, 23, 30), True),
        (at(17, 1, 59), True),
        (at(17, 2, 0), False),
    ],
)
def test_window_wrapping_midnight(moment: datetime, expected: bool) -> None:
    assert schedule_item_active(item(1800, 200), moment) is expected


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (at(16, 9, 59), False),
        (at(16, 10, 0), True),
        (at(16, 10, 14), True),
        (at(16, 10, 15), False),
    ],
)
def test_equal_start_and_end_is_a_play_once_window(moment: datetime, expected: bool) -> None:
    assert schedule_item_active(item(1000, 1000), moment) is expected


def test_play_once_window_wraps_midnight_on_its_start_day() -> None:
    monday_only = item(2355, 2355, days=(1,))
    assert schedule_item_active(monday_only, at(16, 23, 55)) is True
    assert schedule_item_active(monday_only, at(17, 0, 5)) is True
    assert schedule_item_active(monday_only, at(17, 0, 10)) is False
    assert schedule_item_active(monday_only, at(17, 23, 58)) is False


def test_days_use_the_day_the_window_started() -> None:
    monday_only = item(1800, 200, days=(1,))
    assert schedule_item_active(monday_only, at(16, 20)) is True
    assert schedule_item_active(monday_only, at(17, 1)) is True
    assert schedule_item_active(monday_only, at(17, 20)) is False


def test_date_range() -> None:
    ranged = item(1100, 1300, start_date=date(2026, 11, 17), end_date=date(2026, 11, 18))
    assert schedule_item_active(ranged, at(16, 12)) is False
    assert schedule_item_active(ranged, at(17, 12)) is True
    assert schedule_item_active(ranged, at(18, 12)) is True
    assert schedule_item_active(ranged, at(19, 12)) is False


def test_overnight_end_date_uses_the_start_day() -> None:
    ends_monday = item(1800, 200, end_date=date(2026, 11, 16))
    assert schedule_item_active(ends_monday, at(17, 1)) is True
    assert schedule_item_active(ends_monday, at(17, 20)) is False


def test_overnight_start_date_uses_the_start_day() -> None:
    starts_tuesday = item(1800, 200, start_date=date(2026, 11, 17))
    assert schedule_item_active(starts_tuesday, at(17, 1)) is False
    assert schedule_item_active(starts_tuesday, at(17, 20)) is True


def _playlist(*items: ScheduleItem) -> Playlist:
    return Playlist(
        id=1,
        name="p",
        description="",
        type="default",
        order="shuffle",
        weight=1,
        is_enabled=False,
        is_jingle=False,
        schedule_items=items,
    )


def test_unscheduled_playlist_is_always_playable() -> None:
    assert playlist_playable_now(_playlist(), at(16, 4)) is True


def test_playlist_with_any_active_item() -> None:
    playlist = _playlist(item(600, 900), item(1200, 1800))
    assert playlist_playable_now(playlist, at(16, 13)) is True
    assert playlist_playable_now(playlist, at(16, 10)) is False


def test_station_time_uses_station_zone() -> None:
    utc_now = datetime(2026, 11, 20, 20, 0, tzinfo=UTC)
    local = station_time("America/Costa_Rica", utc_now, UTC)
    assert (local.hour, local.tzinfo) == (14, ZoneInfo("America/Costa_Rica"))


@pytest.mark.parametrize(
    ("utc_now", "local_hour", "offset_hours"),
    [
        (datetime(2026, 10, 24, 12, 0, tzinfo=UTC), 14, 2),
        (datetime(2026, 10, 25, 2, 30, tzinfo=UTC), 3, 1),
    ],
)
def test_station_time_follows_dst(utc_now: datetime, local_hour: int, offset_hours: int) -> None:
    local = station_time("Europe/Madrid", utc_now, UTC)
    assert local.hour == local_hour
    assert local.utcoffset() == timedelta(hours=offset_hours)


@pytest.mark.parametrize(
    "zone", [None, "", "Not/AZone", pytest.param("a" * 300, id="name-too-long")]
)
def test_station_time_falls_back(zone: str | None) -> None:
    utc_now = datetime(2026, 11, 20, 20, 0, tzinfo=UTC)
    assert station_time(zone, utc_now, ZoneInfo("Europe/Madrid")).hour == 21
